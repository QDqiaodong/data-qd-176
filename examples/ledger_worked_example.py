"""可直接运行的读数录入 / 结算 / 更正示例（仅用标准库）。

运行：
    PYTHONPATH=src python3 examples/ledger_worked_example.py

它在临时目录建立一个 SQLite 台账，完整演示契约中必须可复核的更正算例：
  1. 首日、次日累计读数 10、20（revision=1，无换表）；
  2. 结算首日，原账单 30 微单位被冻结；
  3. 首日更正 revision=2 closing=12：两日变成 12、8 升，仅对已结算首日追加 +2/+6；
  4. 再更正 revision=3 closing=11：基于财务已确认值 12 追加 -1/-3；
  5. 精确重发第一次更正请求：返回当时回执、不再改账；
  6. 结算次日：按当前 9 升收 27；原结算合计 57 加差额 3 = 财务确认 60。

每一步都打印关键字段并做断言；脚本结束后临时数据库自动清理。
"""
import json
import tempfile
from pathlib import Path

from irrigation import meter_ledger as ml

CONFIG = {
    "fields": [{
        "id": "f1", "area_m2": 2, "capacity_mm": 100, "opening_mm": 0,
        "threshold_mm": 0, "target_mm": 0, "infiltration_mm": 100,
        "efficiency_permille": 1000,
    }],
    "weather": [
        {"date": "2026-09-01", "rain_mm": 0, "et_mm": 8},
        {"date": "2026-09-02", "rain_mm": 0, "et_mm": 0},
    ],
    "meters": [{
        "field_id": "f1", "opening_litres": 0,
        "price_microunits_per_litre": 3,
    }],
}


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main():
    with tempfile.TemporaryDirectory() as folder:
        db = str(Path(folder) / "ledger.db")
        show("create", ml.create(db, CONFIG))

        # 1) 首次录入：两日累计 10、20，均 revision=1、无换表
        first = ml.apply(db, {
            "request_id": "req-initial", "expected_generation": 0,
            "readings": [
                {"field_id": "f1", "date": "2026-09-01", "revision": 1,
                 "closing_litres": 10, "reset_opening_litres": None},
                {"field_id": "f1", "date": "2026-09-02", "revision": 1,
                 "closing_litres": 20, "reset_opening_litres": None},
            ],
        })
        show("首次录入回执", first)
        report = ml.report(db)
        day1, day2 = report["daily"]
        assert (day1["gross_litres"], day2["gross_litres"]) == (10, 10)
        assert day1["water_balance"]["unmet_et_mm"] == "3"      # 8 - 10/2
        assert day2["water_balance"]["closing_mm"] == "5"       # 5 + 10/2

        # 2) 只结算首日：10 升 × 3 = 30，账单从此冻结
        close1 = ml.close(db, {
            "request_id": "req-close-day1", "expected_generation": 1,
            "through_date": "2026-09-01",
        })
        show("结算首日（原账单冻结为 30）", close1["statement"])
        assert close1["statement"]["total_amount_microunits"] == 30

        # 3) 首日更正 revision=2 closing=12：两日 12、8 升
        correction1 = ml.apply(db, {
            "request_id": "req-correction-1", "expected_generation": 2,
            "readings": [
                {"field_id": "f1", "date": "2026-09-01", "revision": 2,
                 "closing_litres": 12, "reset_opening_litres": None},
            ],
        })
        show("第一次更正回执（仅已结算首日 +2 升 / +6 微单位）",
             correction1["adjustments"])
        report = ml.report(db)
        assert [r["gross_litres"] for r in report["daily"]] == [12, 8]
        assert report["daily"][0]["water_balance"]["unmet_et_mm"] == "2"
        assert report["daily"][1]["water_balance"]["closing_mm"] == "4"
        assert report["statements"][0]["total_amount_microunits"] == 30  # 原账单不变

        # 4) 再更正 revision=3 closing=11：财务已确认值是 12，故差额 -1 / -3
        correction2 = ml.apply(db, {
            "request_id": "req-correction-2", "expected_generation": 3,
            "readings": [
                {"field_id": "f1", "date": "2026-09-01", "revision": 3,
                 "closing_litres": 11, "reset_opening_litres": None},
            ],
        })
        show("第二次更正回执（基于已确认值 12 追加 -1 升 / -3 微单位）",
             correction2["adjustments"])
        report = ml.report(db)
        assert report["financial_summary"] == {
            "original_amount_microunits": 30,
            "adjustment_amount_microunits": 3,       # +6 - 3
            "recognized_amount_microunits": 33,
        }

        # 5) 精确重发第一次更正：原 expected_generation=2，返回当时回执且不改账
        replay = ml.apply(db, {
            "request_id": "req-correction-1", "expected_generation": 2,
            "readings": [
                {"field_id": "f1", "date": "2026-09-01", "revision": 2,
                 "closing_litres": 12, "reset_opening_litres": None},
            ],
        })
        show("重发第一次更正（幂等回执，generation 不前进）", replay)
        assert replay == correction1
        assert ml.report(db)["generation"] == 4

        # 6) 结算次日：当前次日用水 20 - 11 = 9 升，9 × 3 = 27
        close2 = ml.close(db, {
            "request_id": "req-close-day2", "expected_generation": 4,
            "through_date": "2026-09-02",
        })
        show("结算次日（按当前 9 升收 27）", close2["statement"])
        assert close2["statement"]["lines"][0]["gross_litres"] == 9
        report = ml.report(db)
        show("最终财务汇总", report["financial_summary"])
        assert report["financial_summary"] == {
            "original_amount_microunits": 57,       # 30 + 27
            "adjustment_amount_microunits": 3,
            "recognized_amount_microunits": 60,
        }
        print("\n全部断言通过：原账单 57 ＋ 净差额 3 ＝ 财务确认 60。")


if __name__ == "__main__":
    main()
