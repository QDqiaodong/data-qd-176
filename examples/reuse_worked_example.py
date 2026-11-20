"""可直接运行的回用水盐分核算与水质拦截示例（仅用标准库）。

运行：
    PYTHONPATH=src python3 examples/reuse_worked_example.py

它完整演示契约中必须可复核的两日算例（池 r、田块 f）：
  1. 首日池内 10L 淡水，抽 5L 获准入田；田间容量 2mm，5mm 灌溉水与期初
     2mm/20mg 土壤水盐混匀后渗漏 5mm，携盐 100/7 mg，土壤期末留盐 40/7 mg；
     渗漏全量（1000‰）排定为次日回流，池首日末存 5L 淡水。
  2. 次日早回流 5L/盐 100/7 mg 到账，池水 10L、浓度 10/7 mg/L，超过田块
     1 mg/L 的限值；1L 抽水被整笔拦截，携盐 10/7 mg 排出系统，田块零灌溉，
     ET 消耗 1mm 纯水，土壤盐仍为 40/7 mg。
  3. 全系统水恒等式 12 = 10（末存）+ 1（拒水）+ 1（ET），
     盐恒等式 20 = 130/7（末存）+ 10/7（弃盐），残差均精确为 0。

每一步打印关键字段并做独立断言；脚本不读写任何台账文件。
"""
import json
from fractions import Fraction

from irrigation.reuse_water import run

INPUT = {
    "fields": [{
        "id": "f", "area_m2": 1, "capacity_mm": 2, "opening_mm": 2,
        "threshold_mm": 0, "target_mm": 0, "infiltration_mm": 10,
        "efficiency_permille": 1000,
    }],
    "weather": [
        {"date": "2026-10-01", "rain_mm": 0, "et_mm": 0},
        {"date": "2026-10-02", "rain_mm": 0, "et_mm": 1},
    ],
    "tanks": [{
        "id": "r", "capacity_litres": 20, "opening_litres": 10,
        "opening_salt_mg": 0,
    }],
    "field_reuse": [{
        "field_id": "f", "opening_salt_mg": 20,
        "max_source_mg_per_litre": 1, "return_permille": 1000,
        "return_tank_id": "r",
    }],
    "operations": [
        {"date": "2026-10-01", "inflows": [], "evaporations": [],
         "transfers": [],
         "withdrawals": [{"field_id": "f", "tank_id": "r", "gross_litres": 5}]},
        {"date": "2026-10-02", "inflows": [], "evaporations": [],
         "transfers": [],
         "withdrawals": [{"field_id": "f", "tank_id": "r", "gross_litres": 1}]},
    ],
}


def check(actual, expected, label):
    assert actual == expected, f"{label}: 期望 {expected}，实际 {actual}"
    print(f"  {label}: {actual}")


def main():
    report = run(INPUT)
    print("=== deliveries（抽水裁决）===")
    print(json.dumps(report["deliveries"], ensure_ascii=False, indent=2))

    t1, t2 = report["tank_daily"]
    print("\n=== 池 r 日账关键值 ===")
    check(t1["closing_litres"], "5", "首日池末水量")
    check(t1["closing_salt_mg"], "0", "首日池末盐量")
    check(t2["return_litres"], "5", "次日晨回流水量")
    check(t2["return_salt_mg"], "100/7", "次日晨回流盐量")
    check(t2["concentration_mg_per_litre"], "10/7", "次日混合浓度 mg/L")
    check(t2["closing_litres"], "9", "次日池末水量（拒水已出池）")
    check(t2["closing_salt_mg"], "90/7", "次日池末盐量")

    f1, f2 = report["field_daily"]
    print("\n=== 田块 f 日账关键值 ===")
    check(f1["water_balance"]["drainage_mm"], "5", "首日渗漏水深")
    check(f1["drainage_salt_mg"], "100/7", "首日渗漏携盐")
    check(f1["closing_salt_mg"], "40/7", "首日期末土壤盐")
    check(f2["water_balance"]["gross_irrigation_mm"], "0", "次日零灌溉（被拦截）")
    check(f2["water_balance"]["actual_et_mm"], "1", "次日 ET 纯水")
    check(f2["closing_salt_mg"], "40/7", "次日期末土壤盐（ET 不带盐）")

    print("\n=== 全系统守恒 ===")
    w = report["system_balance"]["water_litres"]
    s = report["system_balance"]["salt_mg"]
    check(w["opening"], "12", "系统期初水")
    check(w["closing"], "10", "系统期末水")
    check(w["rejected"], "1", "拦截弃水")
    check(w["actual_et"], "1", "实际 ET")
    check(w["residual"], "0", "水残差")
    check(s["opening"], "20", "系统期初盐")
    check(s["closing"], "130/7", "系统期末盐")
    check(s["rejected"], "10/7", "拦截弃盐")
    check(s["residual"], "0", "盐残差")
    assert Fraction(s["closing"]) + Fraction(s["rejected"]) == Fraction(20)
    assert report["pending_returns"] == []
    print("\n全部断言通过：末日无窗口外回流，水盐残差精确为 0。")


if __name__ == "__main__":
    main()
