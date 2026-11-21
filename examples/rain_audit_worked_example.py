"""可直接运行的雨量证据补核与供水建议签发示例（仅用标准库）。

运行：
    PYTHONPATH=src python3 examples/rain_audit_worked_example.py

契约强制算例：一个 1m² 田块，初始 0mm、容量 4mm、入渗上限 10mm、
效率 1000‰、阈值 3mm、目标 4mm，不灌溉。三天 ET 依次 2、0、0mm，
雨量候选依次 [0,4]、[0,4]、[0,2]，仅前两天的分段合计为 4mm。

  基础算例（无探头、三天都可追查）：4 条真实可能历史；第一天未满足 ET
    范围 0..2mm，最后一天结存 2..4mm、渗漏 0..2mm；建议集合 [0,2]，暂不
    签发；追查单指向第一天（最坏/最好待签发数分别 1/0）。
  探头变体：补入第二天结存恰为 4mm 的探头证据后只剩 2 条历史；第三天
    两条都只存 4mm 仍计 2 条，签发 0，且不再开追查单；第一天未满足 ET
    范围变为 2..2mm。

每一步打印关键字段并做独立手算断言；脚本不读写任何台账文件。
"""
import json
from pathlib import Path

from irrigation.rain_audit import run

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rain-audit-input.json"


def check(actual, expected, label):
    assert actual == expected, f"{label}: 期望 {expected}，实际 {actual}"
    print(f"  {label}: {actual}")


def field_daily(report, date):
    return {row["field_id"]: row for row in report["field_daily"]
            if row["date"] == date}


def main():
    data = json.loads(FIXTURE.read_text())

    print("=== 基础算例：三条候选中只有前两日合计为 4mm ===")
    report = run(data)
    check(report["status"], "consistent", "状态")
    check(report["history_count"], 4, "完整可能历史数")
    check([row["possible_rain_mm"] for row in report["rain_daily"]],
          [[0, 4], [0, 4], [0, 2]], "逐日可能雨量")

    day1 = field_daily(report, "2026-09-01")["f"]
    check(day1["unmet_et_mm"], {"min": "0", "max": "2"}, "第1日未满足ET范围")
    day3 = field_daily(report, "2026-09-03")["f"]
    check(day3["closing_mm"], {"min": "2", "max": "4"}, "第3日结存范围")
    check(day3["drainage_mm"], {"min": "0", "max": "2"}, "第3日渗漏范围")

    advice = report["next_advice"][0]
    check(advice["possible_gross_mm"], [0, 2], "次日建议可能值")
    check(advice["released_gross_mm"], None, "次日建议签发值")

    follow = report["follow_up"]
    check(follow["date"], "2026-09-01", "追查日期")
    check(follow["worst_withheld_count"], 1, "最坏待签发田块数")
    check(follow["best_withheld_count"], 0, "最好待签发田块数")
    branch = {row["rain_mm"]: row for row in follow["outcomes"]}
    check(branch[0]["history_count"], 2, "查到 0mm 后历史数")
    check(branch[0]["next_advice"][0]["released_gross_mm"], 0,
          "查到 0mm 后签发")
    check(branch[4]["history_count"], 2, "查到 4mm 后历史数")
    check(branch[4]["next_advice"][0]["possible_gross_mm"], [0, 2],
          "查到 4mm 后建议可能值")

    print("=== 探头变体：第二天结存恰为 4mm ===")
    data["probes"] = [{"field_id": "f", "date": "2026-09-02",
                       "min_closing_mm": "4", "max_closing_mm": "4"}]
    report = run(data)
    check(report["history_count"], 2, "加探头后的历史数")
    check(report["next_advice"][0]["released_gross_mm"], 0, "探头后签发值")
    check(report["follow_up"], None, "探头后追查单")
    check(field_daily(report, "2026-09-01")["f"]["unmet_et_mm"],
          {"min": "2", "max": "2"}, "探头后第1日未满足ET范围")
    check(field_daily(report, "2026-09-03")["f"]["closing_mm"],
          {"min": "4", "max": "4"}, "探头后第3日结存范围")

    print("\n全部独立断言通过。")


if __name__ == "__main__":
    main()
