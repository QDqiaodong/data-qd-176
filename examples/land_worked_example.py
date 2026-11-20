"""可直接运行的田块拆并接续与用水来源审计示例（仅用标准库）。

运行：
    PYTHONPATH=src python3 examples/land_worked_example.py

完整演示契约规定的三个连续气象日（日期 2026-09-01..03）及其两个变体：

  基础算例：期初 a、b 各 2m²，a 期初 4mm、b 期初 0mm。
    第 1 日无雨、ET=0，只给 b 灌溉 4mm；两田效率 1000‰、容量与入渗上限
    足够，a、b 各结存 8L，分别来自初始储水与灌溉。
    第 2 日 a、b 退出，c、d 各接管 a 和 b 各 1m²；无雨无灌溉、ET=1mm，
    c 容量 3mm、d 容量充足。c、d 均接到 8L；c 渗漏 2L、ET 2L、结存 4L；
    d 无渗漏、ET 2L、结存 6L。
    第 3 日 c、d 把全部土地交给新田块 e，无雨无灌溉、ET=0、e 容量足够，
    末存 10L，其中 a 的初始水、b 的灌溉各 5L。
  变体 A（独占接续）：第 2 日改为 c 独占 a、d 独占 b 的原土地，末存来源
    变为 a 贡献 4L、b 贡献 6L。
  变体 B（去灌溉、ET=3）：再去掉首日 b 的灌溉并把次日 ET 改为 3mm，
    各接管一半时 c 期初深度 2mm、缺水 1mm；c 独占 a 时期初 4mm、渗漏 1mm、
    缺水 0。

每一步打印关键字段并做独立手算断言；脚本不读写任何台账文件。
"""
import copy
import json
from fractions import Fraction
from pathlib import Path

from irrigation.land_water import run

F = Fraction
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "land-input.json"


def check(actual, expected, label):
    assert actual == expected, f"{label}: 期望 {expected}，实际 {actual}"
    print(f"  {label}: {actual}")


def base_data():
    return json.loads(FIXTURE.read_text())


def scenario_half_takeover():
    print("=== 基础算例：c、d 各接管 a、b 一半土地 ===")
    report = run(base_data())

    # 第 1 日：a 结存 4mm×2m²=8L（初始水）；b 结存 4mm×2m²=8L（灌溉水）。
    day1 = {r["field_id"]: r for r in report["daily"] if r["date"] == "2026-09-01"}
    check(day1["a"]["closing_mm"], "4", "a 第1日结存(mm)")
    check(day1["b"]["closing_mm"], "4", "b 第1日结存(mm)")

    # 交接边：a/b 各 4mm，转出 1m² => 每边 4L，共 4 边 16L。
    transfers = [t for t in report["land_transfers"] if t["date"] == "2026-09-02"]
    check(len(transfers), 4, "第2日交接边数")
    check(sum(F(t["water_litres"]) for t in transfers), F(16), "第2日交接总水量(L)")

    # 第 2 日：c、d 期初均 4mm；c 容量 3mm => 渗漏 1mm(2L)，ET 1mm(2L)，结存 2mm(4L)。
    day2 = {r["field_id"]: r for r in report["daily"] if r["date"] == "2026-09-02"}
    check(day2["c"]["opening_mm"], "4", "c 期初(mm)")
    check(day2["c"]["drainage_mm"], "1", "c 渗漏(mm)")
    check(day2["c"]["actual_et_mm"], "1", "c 实际ET(mm)")
    check(day2["c"]["closing_mm"], "2", "c 结存(mm)")
    check(day2["d"]["drainage_mm"], "0", "d 渗漏(mm)")
    check(day2["d"]["closing_mm"], "3", "d 结存(mm)")

    # 第 3 日：c 交 4L、d 交 6L，e 期初 = 10/4 = 5/2 mm，末存 10L。
    e = next(r for r in report["daily"] if r["field_id"] == "e")
    check(e["opening_mm"], "5/2", "e 期初(mm)")
    check(e["closing_mm"], "5/2", "e 结存(mm)")
    check(report["system_balance"]["storage_litres"], "10", "系统末存(L)")
    check(report["system_balance"]["residual_litres"], "0", "系统残差(L)")

    # 来源审计：a 的初始水 8L 总去向 = c 渗漏 1 + c ET 1 + d ET 1 + e 末存 5。
    src_a = next(r for r in report["audit_sources"]
                 if r["identity"]["field_id"] == "a")
    check(src_a["water_litres"], "8", "a 初始来源水量(L)")
    check({(s["identity"]["field_id"], s["identity"]["kind"]): s["water_litres"]
           for s in src_a["sinks"]},
          {("c", "drainage"): "1", ("c", "et"): "1",
           ("d", "et"): "1", ("e", "storage"): "5"},
          "a 初始水的全部终点")

    # 终点审计：第 2 日 c 的渗漏 2L 由两笔来源各贡献 1L。
    sink_c = next(r for r in report["audit_sinks"]
                  if r["identity"]["field_id"] == "c"
                  and r["identity"]["kind"] == "drainage")
    check(sink_c["water_litres"], "2", "c 渗漏终点水量(L)")
    check({s["identity"]["field_id"]: s["water_litres"] for s in sink_c["sources"]},
          {"a": "1", "b": "1"}, "c 渗漏的来源分解")

    # e 末存 10L：a 初始水、b 灌溉水各 5L。
    sink_e = next(r for r in report["audit_sinks"]
                  if r["identity"]["field_id"] == "e")
    check({s["identity"]["field_id"]: s["water_litres"] for s in sink_e["sources"]},
          {"a": "5", "b": "5"}, "e 末存的来源分解")


def scenario_exclusive_takeover():
    print("=== 变体 A：c 独占 a、d 独占 b ===")
    data = base_data()
    data["repartitions"][0]["moves"] = [
        {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
        {"from_field_id": "b", "to_field_id": "d", "area_m2": 2},
    ]
    report = run(data)
    sink_e = next(r for r in report["audit_sinks"]
                  if r["identity"]["field_id"] == "e")
    # c 承接 a 全部 8L：渗漏 2 + ET 2 + 余 4 进 e；d 承接 b 全部 8L：ET 2 + 余 6。
    check({s["identity"]["field_id"]: s["water_litres"] for s in sink_e["sources"]},
          {"a": "4", "b": "6"}, "独占接续下 e 末存来源")
    day2 = {r["field_id"]: r for r in report["daily"] if r["date"] == "2026-09-02"}
    check(day2["c"]["drainage_mm"], "1", "c 渗漏仍为 1mm")
    check(day2["d"]["drainage_mm"], "0", "d 无渗漏")


def scenario_no_irrigation_higher_et():
    print("=== 变体 B：去掉首日灌溉，次日 ET=3mm ===")
    data = base_data()
    data["applications"] = []
    data["weather"][1]["et_mm"] = 3

    half = run(copy.deepcopy(data))
    day2 = {r["field_id"]: r for r in half["daily"] if r["date"] == "2026-09-02"}
    # 各接管一半：c 只承接 a 的 4L / 2m² = 2mm，无渗漏，ET 需求 3mm 缺水 1mm。
    check(day2["c"]["opening_mm"], "2", "半接管 c 期初(mm)")
    check(day2["c"]["drainage_mm"], "0", "半接管 c 渗漏(mm)")
    check(day2["c"]["unmet_et_mm"], "1", "半接管 c 缺水量(mm)")

    data["repartitions"][0]["moves"] = [
        {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
        {"from_field_id": "b", "to_field_id": "d", "area_m2": 2},
    ]
    exclusive = run(data)
    day2 = {r["field_id"]: r for r in exclusive["daily"]
            if r["date"] == "2026-09-02"}
    # c 独占 a：期初 4mm，超容量 3mm => 渗漏 1mm，可用 3mm 恰好满足 ET，缺水 0。
    check(day2["c"]["opening_mm"], "4", "独占 c 期初(mm)")
    check(day2["c"]["drainage_mm"], "1", "独占 c 渗漏(mm)")
    check(day2["c"]["unmet_et_mm"], "0", "独占 c 缺水量(mm)")


def main():
    scenario_half_takeover()
    scenario_exclusive_takeover()
    scenario_no_irrigation_higher_et()
    print("\n全部独立断言通过。")


if __name__ == "__main__":
    main()
