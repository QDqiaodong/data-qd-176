import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path

from irrigation.balance import step
from irrigation.planner import plan
from irrigation.service import run

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC}


def worked_example():
    return json.loads(Path("fixtures/plan-input.json").read_text())


def field(fid, area=1, capacity=10, opening=0, threshold=0, target=0,
          infiltration=100, efficiency=1000):
    return {
        "id": fid, "area_m2": area, "capacity_mm": capacity,
        "opening_mm": opening, "threshold_mm": threshold, "target_mm": target,
        "infiltration_mm": infiltration, "efficiency_permille": efficiency,
    }


def day_row(date, rain=0, et=1):
    return {"date": date, "rain_mm": rain, "et_mm": et}


def supply(date, inflow=0, pump=1000, minimum=0):
    return {"date": date, "inflow_litres": inflow,
            "pump_limit_litres": pump, "minimum_closing_litres": minimum}


def rule(fid, weight=1, choices=(0, 1)):
    return {"field_id": fid, "stress_weight": weight,
            "gross_mm_choices": list(choices)}


def make_data(fields, weather, supply_days, rules,
              applications=(), capacity=1000, opening=1000):
    return {
        "fields": fields,
        "weather": weather,
        "applications": list(applications),
        "planning": {
            "reservoir": {"capacity_litres": capacity, "opening_litres": opening},
            "supply_days": supply_days,
            "field_rules": rules,
        },
    }


def brute_force_optimal(data):
    """独立穷举：枚举每个未锁定组合的全部离散选择，逐日模拟蓄水池与田间
    平衡，按四级目标元组取最小值。不调用 irrigation.planner.plan。
    返回 (目标元组, gross_mm 序列)，无可行计划时返回 None。"""
    fields = {f["id"]: f for f in data["fields"]}
    weather = data["weather"]
    apps = {(a["field_id"], a["date"]): a["gross_mm"]
            for a in data["applications"]}
    fids = sorted(fields)
    dates = [w["date"] for w in weather]
    rules = {r["field_id"]: r for r in data["planning"]["field_rules"]}
    weights = {fid: rules[fid]["stress_weight"] for fid in fids}
    cap = data["planning"]["reservoir"]["capacity_litres"]
    level0 = data["planning"]["reservoir"]["opening_litres"]
    sup = {s["date"]: s for s in data["planning"]["supply_days"]}

    options = []
    for d in dates:
        for fid in fids:
            if (fid, d) in apps:
                options.append([(apps[(fid, d)], True)])
            else:
                options.append([(g, False) for g in rules[fid]["gross_mm_choices"]])

    best = None
    for combo in product(*options):
        level = level0
        closings = {fid: Fraction(fields[fid]["opening_mm"]) for fid in fids}
        total_unmet = Fraction(0)
        total_litres = 0
        feasible = True
        for di, d in enumerate(dates):
            s = sup[d]
            filled = level + s["inflow_litres"]
            spilled = max(0, filled - cap)
            available = filled - spilled
            used = 0
            for fi, fid in enumerate(fids):
                used += combo[di * len(fids) + fi][0] * fields[fid]["area_m2"]
            if used > s["pump_limit_litres"]:
                feasible = False
                break
            if available - used < s["minimum_closing_litres"]:
                feasible = False
                break
            level = available - used
            for fi, fid in enumerate(fids):
                gross = combo[di * len(fids) + fi][0]
                r = step(fields[fid], closings[fid],
                         weather[di]["rain_mm"], gross, weather[di]["et_mm"])
                closings[fid] = r["closing_mm"]
                total_unmet += weights[fid] * r["unmet_et_mm"]
                total_litres += gross * fields[fid]["area_m2"]
        if not feasible:
            continue
        terminal = sum(
            weights[fid] * max(Fraction(0),
                               Fraction(fields[fid]["target_mm"]) - closings[fid])
            for fid in fids
        )
        seq = tuple(g for g, _ in combo)
        key = (total_unmet, terminal, total_litres, seq)
        if best is None or key < best[0]:
            best = (key, seq)
    return best


def decision_sequence(report):
    return tuple(row["gross_mm"] for row in report["decisions"])


class WorkedExampleTests(unittest.TestCase):
    def test_mandatory_cross_day_case(self):
        report = plan(worked_example())
        self.assertEqual(report["status"], "optimal")
        # 仅在第一天给 b 供水 5mm，其余全零
        self.assertEqual(
            [(r["field_id"], r["date"], r["gross_mm"]) for r in report["decisions"]],
            [("a", "2026-07-01", 0), ("b", "2026-07-01", 5),
             ("a", "2026-07-02", 0), ("b", "2026-07-02", 0)],
        )
        self.assertTrue(all(r["locked"] is False for r in report["decisions"]))
        self.assertEqual(report["objective"], {
            "weighted_unmet_et_mm": "8",
            "weighted_terminal_deficit_mm": "0",
            "gross_supply_litres": 5,
        })
        # 同样 5 升若给 a，加权未满足 ET 为 10
        alt = worked_example()
        alt["applications"] = [
            {"field_id": "a", "date": "2026-07-01", "gross_mm": 5},
            {"field_id": "b", "date": "2026-07-01", "gross_mm": 0},
            {"field_id": "a", "date": "2026-07-02", "gross_mm": 0},
            {"field_id": "b", "date": "2026-07-02", "gross_mm": 0},
        ]
        alt_report = plan(alt)
        self.assertEqual(alt_report["objective"]["weighted_unmet_et_mm"], "10")
        self.assertGreater(Fraction(alt_report["objective"]["weighted_unmet_et_mm"]),
                           Fraction(report["objective"]["weighted_unmet_et_mm"]))

    def test_decisions_cover_every_field_day_in_order(self):
        report = plan(worked_example())
        keys = [(r["date"], r["field_id"]) for r in report["decisions"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 2 * 2)


class GlobalOptimalityTests(unittest.TestCase):
    """用独立穷举器逐实例核对 plan() 返回的是全局最优及全部平局规则。"""

    def instances(self):
        yield make_data(
            [field("a"), field("b", capacity=5)],
            [day_row("2026-06-01", et=1), day_row("2026-06-02", et=4)],
            [supply("2026-06-01", pump=5), supply("2026-06-02", pump=0)],
            [rule("a", weight=2, choices=(0, 5)), rule("b", choices=(0, 5))],
            capacity=5, opening=5,
        )
        yield make_data(
            [field("a", efficiency=800), field("b", opening=3, target=4)],
            [day_row("2026-06-01", et=1), day_row("2026-06-02", rain=2, et=3),
             day_row("2026-06-03", et=2)],
            [supply("2026-06-01", inflow=4, pump=3, minimum=1),
             supply("2026-06-02", inflow=10, pump=6),
             supply("2026-06-03", pump=4, minimum=2)],
            [rule("a", weight=2, choices=(0, 1, 3)),
             rule("b", weight=3, choices=(0, 2, 4))],
            capacity=8, opening=2,
        )
        yield make_data(
            [field("a", area=2, capacity=4), field("b", efficiency=750),
             field("c", opening=2, target=3)],
            [day_row("2026-06-01"), day_row("2026-06-02", et=2),
             day_row("2026-06-03", rain=1), day_row("2026-06-04", et=5)],
            [supply("2026-06-01", inflow=6, pump=5),
             supply("2026-06-02", pump=7, minimum=2),
             supply("2026-06-03", inflow=3, pump=6),
             supply("2026-06-04", pump=9)],
            [rule("a", choices=(0, 2)), rule("b", weight=2, choices=(0, 1, 2)),
             rule("c", weight=4, choices=(0, 3))],
            capacity=12, opening=4,
        )
        # 含显式锁定（含锁定零）与锁定值不在 choices 内
        yield make_data(
            [field("a"), field("b")],
            [day_row("2026-06-01"), day_row("2026-06-02", et=2)],
            [supply("2026-06-01", pump=4), supply("2026-06-02", pump=4)],
            [rule("a", choices=(0, 1, 2)), rule("b", choices=(0, 1, 2))],
            applications=[
                {"field_id": "a", "date": "2026-06-01", "gross_mm": 0},
                {"field_id": "b", "date": "2026-06-02", "gross_mm": 2},
            ],
            capacity=6, opening=3,
        )
        # 含溢流与最低蓄水
        yield make_data(
            [field("a", capacity=3), field("b", opening=1)],
            [day_row("2026-06-01", et=2), day_row("2026-06-02", et=3),
             day_row("2026-06-03", et=1)],
            [supply("2026-06-01", inflow=20, pump=0),
             supply("2026-06-02", pump=6, minimum=2),
             supply("2026-06-03", pump=6)],
            [rule("a", weight=2, choices=(0, 1, 3)),
             rule("b", choices=(0, 2))],
            capacity=5, opening=0,
        )

    def test_plan_matches_independent_brute_force(self):
        for data in self.instances():
            with self.subTest(data=data):
                expected = brute_force_optimal(data)
                self.assertIsNotNone(expected)
                report = plan(data)
                self.assertEqual(report["status"], "optimal")
                self.assertEqual(decision_sequence(report), expected[0][3])
                obj = report["objective"]
                self.assertEqual(obj["weighted_unmet_et_mm"], str(expected[0][0]))
                self.assertEqual(obj["weighted_terminal_deficit_mm"], str(expected[0][1]))
                self.assertEqual(obj["gross_supply_litres"], expected[0][2])
                # 毛供水升数目标与水账汇总一致
                self.assertEqual(report["water_balance"]["summary"]["gross_supply_litres"],
                                 expected[0][2])

    def test_brute_force_agrees_on_infeasible_instances(self):
        data = make_data(
            [field("a")], [day_row("2026-06-01"), day_row("2026-06-02")],
            [supply("2026-06-01", minimum=3), supply("2026-06-02", inflow=100)],
            [rule("a")], capacity=10, opening=2,
        )
        self.assertIsNone(brute_force_optimal(data))
        report = plan(data)
        self.assertEqual(report["status"], "infeasible")
        self.assertTrue(report["reason"].strip())

    def test_random_small_instances_match_brute_force(self):
        import random
        rng = random.Random(20260720)
        for trial in range(12):
            n_fields = rng.randint(1, 3)
            n_days = rng.randint(1, 4)
            fids = [f"f{i}" for i in range(n_fields)]
            fields = [
                field(fids[i], area=rng.randint(1, 3),
                      capacity=rng.randint(2, 6),
                      opening=rng.randint(0, 2),
                      efficiency=rng.choice([1000, 800, 750, 600]),
                      target=rng.randint(0, 2))
                for i in range(n_fields)
            ]
            base = "2026-08-01"
            from datetime import date, timedelta
            d0 = date.fromisoformat(base)
            dates = [(d0 + timedelta(days=i)).isoformat() for i in range(n_days)]
            weather = [day_row(d, rain=rng.choice([0, 0, 1, 3]),
                               et=rng.randint(1, 5)) for d in dates]
            supply_days = [
                supply(d, inflow=rng.randint(0, 8), pump=rng.randint(0, 7),
                       minimum=rng.randint(0, 2))
                for d in dates
            ]
            rules = [
                rule(fids[i], weight=rng.randint(1, 4),
                     choices=tuple(sorted(rng.sample([0, 1, 2, 3, 5],
                                                     k=rng.randint(1, 3)))))
                for i in range(n_fields)
            ]
            for r in rules:
                if r["gross_mm_choices"][0] != 0:
                    r["gross_mm_choices"] = [0] + r["gross_mm_choices"][:2]
            data = make_data(fields, weather, supply_days, rules,
                             capacity=rng.randint(6, 12),
                             opening=rng.randint(0, 6))
            with self.subTest(trial=trial):
                expected = brute_force_optimal(data)
                report = plan(data)
                if expected is None:
                    self.assertEqual(report["status"], "infeasible")
                    self.assertNotIn("decisions", report)
                else:
                    self.assertEqual(report["status"], "optimal")
                    self.assertEqual(decision_sequence(report), expected[0][3])
                    self.assertEqual(
                        report["objective"]["weighted_unmet_et_mm"],
                        str(expected[0][0]),
                    )
                    self.assertEqual(
                        report["objective"]["weighted_terminal_deficit_mm"],
                        str(expected[0][1]),
                    )


class ReservoirMechanicsTests(unittest.TestCase):
    def test_inflow_spills_before_pumping_and_spill_is_unrecoverable(self):
        data = make_data(
            [field("a", capacity=6)],
            [day_row("2026-06-01", et=2), day_row("2026-06-02", et=6)],
            [supply("2026-06-01", inflow=10, pump=0),
             supply("2026-06-02", pump=10)],
            [rule("a", choices=(0, 5))],
            capacity=5, opening=0,
        )
        report = plan(data)
        d1, d2 = report["reservoir_daily"]
        # 10 升来水：蓄到 5，溢流 5；当日泵限 0，抽不走任何水
        self.assertEqual(d1["opening_litres"], 0)
        self.assertEqual(d1["spill_litres"], 5)
        self.assertEqual(d1["available_litres"], 5)
        self.assertEqual(d1["gross_used_litres"], 0)
        self.assertEqual(d1["closing_litres"], 5)
        # 溢流的 5 升不能在第二天使用，第二天最多只能抽 5 升
        self.assertEqual(d2["opening_litres"], 5)
        self.assertEqual(d2["available_litres"], 5)
        self.assertEqual(decision_sequence(report), (0, 5))
        for row in report["reservoir_daily"]:
            self.assertEqual(row["balance_residual_litres"], 0)
            self.assertEqual(
                row["opening_litres"] + row["inflow_litres"] - row["spill_litres"]
                - row["gross_used_litres"] - row["closing_litres"], 0)

    def test_pumping_cannot_reduce_spill_within_same_day(self):
        data = make_data(
            [field("a")], [day_row("2026-06-01")],
            [supply("2026-06-01", inflow=8, pump=2)],
            [rule("a", choices=(0, 1, 2))],
            capacity=5, opening=0,
        )
        report = plan(data)
        row = report["reservoir_daily"][0]
        # 即使当日抽水 2 升，溢流仍按抽水前的蓄水计算为 3 升
        self.assertEqual(row["spill_litres"], 3)
        self.assertEqual(row["available_litres"], 5)
        self.assertEqual(row["closing_litres"], 5 - row["gross_used_litres"])

    def test_minimum_closing_blocks_borrowing_future_inflow(self):
        data = make_data(
            [field("a")],
            [day_row("2026-06-01"), day_row("2026-06-02", et=4)],
            [supply("2026-06-01", inflow=0, pump=0, minimum=3),
             supply("2026-06-02", inflow=100, pump=100)],
            [rule("a", choices=(0, 5))],
            capacity=10, opening=2,
        )
        report = plan(data)
        self.assertEqual(report["status"], "infeasible")
        self.assertIn("最低期末蓄水", report["reason"])
        self.assertEqual(set(report), {"schema_version", "status", "reason"})

    def test_locked_supply_over_pump_limit_is_infeasible(self):
        data = make_data(
            [field("a", area=2)], [day_row("2026-06-01")],
            [supply("2026-06-01", pump=3)],
            [rule("a", choices=(0, 2))],
            applications=[{"field_id": "a", "date": "2026-06-01", "gross_mm": 2}],
            capacity=10, opening=10,
        )
        # 2mm × 2m² = 4 升 > 泵限 3 升
        report = plan(data)
        self.assertEqual(report["status"], "infeasible")
        self.assertIn("泵", report["reason"])

    def test_minimum_closing_violated_after_locked_use_is_infeasible(self):
        data = make_data(
            [field("a")], [day_row("2026-06-01")],
            [supply("2026-06-01", pump=5, minimum=4)],
            [rule("a", choices=(0, 3))],
            applications=[{"field_id": "a", "date": "2026-06-01", "gross_mm": 3}],
            capacity=10, opening=5,
        )
        report = plan(data)
        self.assertEqual(report["status"], "infeasible")
        self.assertIn("最低期末蓄水", report["reason"])


class LockedZeroTests(unittest.TestCase):
    def _base(self):
        return make_data(
            [field("a")], [day_row("2026-06-01", et=1)],
            [supply("2026-06-01", pump=5)],
            [rule("a", choices=(0, 1))],
            capacity=5, opening=2,
        )

    def test_missing_record_is_choice_but_explicit_zero_is_locked(self):
        # 缺记录：计划器自由选择，缺水时会选 1mm
        missing = self._base()
        report_missing = plan(missing)
        self.assertEqual(decision_sequence(report_missing), (1,))
        self.assertFalse(report_missing["decisions"][0]["locked"])

        # 显式锁定为零：必须执行 0，locked 为真，接受缺水
        locked = self._base()
        locked["applications"] = [
            {"field_id": "a", "date": "2026-06-01", "gross_mm": 0}]
        report_locked = plan(locked)
        self.assertEqual(decision_sequence(report_locked), (0,))
        self.assertTrue(report_locked["decisions"][0]["locked"])
        self.assertEqual(
            report_locked["water_balance"]["daily"][0]["unmet_et_mm"], "1")

    def test_locked_value_need_not_belong_to_choices(self):
        data = self._base()
        data["applications"] = [
            {"field_id": "a", "date": "2026-06-01", "gross_mm": 2}]
        report = plan(data)  # choices 只有 [0,1]，锁定 2 仍合法
        self.assertEqual(decision_sequence(report), (2,))
        self.assertTrue(report["decisions"][0]["locked"])


class TieBreakTests(unittest.TestCase):
    def test_lexicographic_tie_break_when_first_three_objectives_equal(self):
        # 两个完全相同的田块，权重相同，第一天只够浇一个：目标前三项相同，
        # 字典序要求先给字符串较小的 field_id。
        data = make_data(
            [field("a"), field("b")],
            [day_row("2026-06-01"), day_row("2026-06-02", et=4)],
            [supply("2026-06-01", pump=1), supply("2026-06-02", pump=0)],
            [rule("a"), rule("b")],
            capacity=5, opening=1,
        )
        report = plan(data)
        # 序列按 (日期, field_id) 排列为 (a日1, b日1, a日2, b日2)；
        # 字典序最小意味着更早的槽位优先取 0，因此浇 b
        self.assertEqual(decision_sequence(report), (0, 1, 0, 0))
        # 穷举器独立确认同样的平局选择
        self.assertEqual(brute_force_optimal(data)[0][3], (0, 1, 0, 0))

    def test_third_objective_prefers_less_water_on_equal_et_outcome(self):
        # 供水 1mm 与 2mm 当日结果相同（受容量限制多出的部分渗漏），
        # 第三目标应选毛供水更少的计划
        data = make_data(
            [field("a", capacity=1, opening=1)],
            [day_row("2026-06-01", et=1)],
            [supply("2026-06-01", pump=5)],
            [rule("a", choices=(0, 1, 2))],
            capacity=5, opening=5,
        )
        report = plan(data)
        self.assertEqual(decision_sequence(report), (0,))  # 零供水已无缺水
        data["fields"][0]["opening_mm"] = 0
        report = plan(data)
        self.assertEqual(decision_sequence(report), (1,))  # 1mm 足够，2mm 只会渗漏

    def test_fraction_objectives_are_exact_and_canonical(self):
        # efficiency=800：抽 1mm 仅到田 4/5mm，et=1 时缺水 1/5；加权 2 得 2/5
        data = make_data(
            [field("a", efficiency=800, opening=0)],
            [day_row("2026-06-01", et=1)],
            [supply("2026-06-01", pump=0)],
            [rule("a", weight=2, choices=(0, 1))],
            capacity=5, opening=5,
        )
        report = plan(data)
        self.assertEqual(report["objective"]["weighted_unmet_et_mm"], "2")
        data["planning"]["supply_days"][0]["pump_limit_litres"] = 1
        report = plan(data)
        # 抽 1mm：缺水 1/5，加权后 2/5，必须是规范分数字符串而非浮点
        self.assertEqual(report["objective"]["weighted_unmet_et_mm"], "2/5")
        self.assertNotIn(".", report["objective"]["weighted_unmet_et_mm"])
        self.assertEqual(
            report["water_balance"]["daily"][0]["unmet_et_mm"], "1/5")
        # 与穷举器的精确 Fraction 比较一致
        self.assertEqual(
            Fraction(report["objective"]["weighted_unmet_et_mm"]),
            brute_force_optimal(data)[0][0],
        )

    def test_terminal_deficit_breaks_ties_on_last_day(self):
        # 两天缺水总量相同的两种路径，期末 target 缺水不同
        data = make_data(
            [field("a", opening=1, target=2, capacity=5)],
            [day_row("2026-06-01", et=1), day_row("2026-06-02", et=1)],
            [supply("2026-06-01", pump=1), supply("2026-06-02", pump=1)],
            [rule("a", choices=(0, 1))],
            capacity=5, opening=5,
        )
        report = plan(data)
        self.assertEqual(brute_force_optimal(data)[0][3], decision_sequence(report))
        # 两天各供 1mm：末日 closing=1（供水补到 2 后被当日 ET 消耗 1），
        # target=2，期末缺水为 1；其他路径期末缺水为 2，故第二目标选中此路径
        self.assertEqual(decision_sequence(report), (1, 1))
        self.assertEqual(report["objective"]["weighted_terminal_deficit_mm"], "1")
        self.assertEqual(report["objective"]["weighted_unmet_et_mm"], "0")


class InputValidationTests(unittest.TestCase):
    def _good(self):
        return worked_example()

    def assert_input_error(self, mutate):
        data = self._good()
        mutate(data)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            plan(data)

    def test_bool_is_not_an_integer_everywhere(self):
        self.assert_input_error(
            lambda d: d["planning"]["reservoir"].__setitem__("capacity_litres", True))
        self.assert_input_error(
            lambda d: d["planning"]["supply_days"][0].__setitem__("inflow_litres", True))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__("stress_weight", True))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0]["gross_mm_choices"].__setitem__(1, True))
        data = self._good()
        data["applications"] = [
            {"field_id": "a", "date": "2026-07-01", "gross_mm": True}]
        with self.assertRaises(ValueError):
            plan(data)

    def test_negative_and_out_of_range_values(self):
        self.assert_input_error(
            lambda d: d["planning"]["reservoir"].__setitem__("capacity_litres", 0))
        self.assert_input_error(
            lambda d: d["planning"]["reservoir"].__setitem__("opening_litres", -1))
        self.assert_input_error(
            lambda d: d["planning"]["reservoir"].__setitem__(
                "opening_litres", d["planning"]["reservoir"]["capacity_litres"] + 1))
        self.assert_input_error(
            lambda d: d["planning"]["supply_days"][0].__setitem__("inflow_litres", -3))
        self.assert_input_error(
            lambda d: d["planning"]["supply_days"][0].__setitem__(
                "minimum_closing_litres",
                d["planning"]["reservoir"]["capacity_litres"] + 1))

    def test_supply_day_identity_errors(self):
        self.assert_input_error(lambda d: d["planning"]["supply_days"].pop())
        self.assert_input_error(
            lambda d: d["planning"]["supply_days"][0].__setitem__(
                "date", d["planning"]["supply_days"][1]["date"]))
        self.assert_input_error(
            lambda d: d["planning"]["supply_days"][0].__setitem__(
                "date", "2026-07-09"))

    def test_weather_gap_remains_an_input_error(self):
        data = self._good()
        data["weather"][1]["date"] = "2026-07-03"
        with self.assertRaisesRegex(ValueError, "consecutive"):
            plan(data)

    def test_field_rule_identity_and_choice_errors(self):
        self.assert_input_error(lambda d: d["planning"]["field_rules"].pop())
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__("field_id", "zzz"))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][1].__setitem__(
                "field_id", d["planning"]["field_rules"][0]["field_id"]))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__(
                "gross_mm_choices", [1, 5]))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__(
                "gross_mm_choices", [0, 0]))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__(
                "gross_mm_choices", [0, 5, 3]))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__(
                "gross_mm_choices", [0, 1, 2, 3]))
        self.assert_input_error(
            lambda d: d["planning"]["field_rules"][0].__setitem__("stress_weight", 0))

    def test_scale_limits_apply_only_to_planning_mode(self):
        data = self._good()
        data["fields"].extend([field("c"), field("d")])
        data["planning"]["field_rules"].extend([rule("c"), rule("d")])
        with self.assertRaisesRegex(ValueError, "planning mode"):
            plan(data)
        # 旧模式规模上限不变：4 个田块的普通核算仍然合法
        legacy = copy.deepcopy(data)
        del legacy["planning"]
        legacy["weather"] = legacy["weather"][:1]
        legacy["applications"] = []
        self.assertEqual(run(legacy)["summary"]["field_count"], 4)

        data = self._good()
        base = "2026-07-01"
        from datetime import date, timedelta
        d0 = date.fromisoformat(base)
        days = [(d0 + timedelta(days=i)).isoformat() for i in range(8)]
        data["weather"] = [day_row(d) for d in days]
        data["planning"]["supply_days"] = [supply(d) for d in days]
        with self.assertRaisesRegex(ValueError, "planning mode"):
            plan(data)

    def test_missing_planning_object_is_input_error(self):
        data = self._good()
        del data["planning"]
        with self.assertRaises(KeyError):
            plan(data)

    def test_infeasible_is_not_an_input_error(self):
        # 合法结构但物理上不可行：返回值而不是异常
        data = make_data(
            [field("a")], [day_row("2026-06-01")],
            [supply("2026-06-01", minimum=9)], [rule("a")],
            capacity=10, opening=2,
        )
        report = plan(data)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["status"], "infeasible")
        self.assertIsInstance(report["reason"], str)
        self.assertTrue(report["reason"])


class ImmutabilityAndOrderTests(unittest.TestCase):
    def test_input_object_is_not_modified(self):
        data = worked_example()
        snapshot = copy.deepcopy(data)
        plan(data)
        self.assertEqual(data, snapshot)

    def test_array_order_does_not_change_business_result(self):
        data = worked_example()
        shuffled = copy.deepcopy(data)
        shuffled["planning"]["supply_days"].reverse()
        shuffled["planning"]["field_rules"].reverse()
        shuffled["applications"] = []
        self.assertEqual(plan(data), plan(shuffled))

        data2 = make_data(
            [field("a"), field("b")],
            [day_row("2026-06-01"), day_row("2026-06-02", et=2)],
            [supply("2026-06-01", pump=3), supply("2026-06-02", pump=3)],
            [rule("a", choices=(0, 1, 2)), rule("b", choices=(0, 1, 2))],
            applications=[
                {"field_id": "b", "date": "2026-06-01", "gross_mm": 1},
                {"field_id": "a", "date": "2026-06-02", "gross_mm": 0},
                {"field_id": "a", "date": "2026-06-01", "gross_mm": 0},
            ],
            capacity=6, opening=4,
        )
        ordered = copy.deepcopy(data2)
        ordered["applications"].sort(key=lambda r: (r["date"], r["field_id"]))
        self.assertEqual(plan(data2), plan(ordered))


class WaterBalanceReportTests(unittest.TestCase):
    def test_report_reuses_original_balance_and_conservation_holds(self):
        data = worked_example()
        report = plan(data)
        explicit = run({
            "fields": data["fields"],
            "weather": data["weather"],
            "applications": [
                {"field_id": r["field_id"], "date": r["date"],
                 "gross_mm": r["gross_mm"]}
                for r in report["decisions"]
            ],
        })
        self.assertEqual(report["water_balance"], explicit)
        for row in report["water_balance"]["daily"]:
            self.assertEqual(row["balance_residual_mm"], "0")
            incoming = sum(Fraction(row[k]) for k in
                           ("opening_mm", "rain_mm", "gross_irrigation_mm"))
            outgoing = sum(Fraction(row[k]) for k in
                           ("closing_mm", "delivery_loss_mm", "runoff_mm",
                            "drainage_mm", "actual_et_mm"))
            self.assertEqual(incoming, outgoing)
        # 目标与水账口径吻合：全窗口毛供水升数
        self.assertEqual(
            report["objective"]["gross_supply_litres"],
            report["water_balance"]["summary"]["gross_supply_litres"],
        )

    def test_rain_enters_fields_only_not_reservoir(self):
        data = make_data(
            [field("a", opening=1)], [day_row("2026-06-01", rain=4, et=2)],
            [supply("2026-06-01", inflow=0, pump=0)], [rule("a")],
            capacity=5, opening=1,
        )
        report = plan(data)
        self.assertEqual(report["reservoir_daily"][0]["inflow_litres"], 0)
        self.assertEqual(report["reservoir_daily"][0]["closing_litres"], 1)
        # 雨水全部进田：opening 1 + rain 4 - et 2 = closing 3
        self.assertEqual(report["water_balance"]["daily"][0]["closing_mm"], "3")


class PlanCliTests(unittest.TestCase):
    def _write(self, folder, name, payload):
        path = Path(folder) / name
        if isinstance(payload, str):
            path.write_text(payload)
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False))
        return str(path)

    def _run(self, source, output):
        return subprocess.run(
            [sys.executable, "-m", "irrigation.plan_cli",
             "--source", source, "--output", output],
            capture_output=True, text=True, env=ENV,
        )

    def test_success_writes_report_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", worked_example())
            output = str(Path(folder) / "report.json")
            r = self._run(source, output)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(Path(output).read_text())
            self.assertEqual(report["status"], "optimal")
            self.assertEqual(report["objective"]["weighted_unmet_et_mm"], "8")
            self.assertEqual(plan(worked_example()), report)

    def test_infeasible_exits_zero_without_partial_payload(self):
        data = make_data(
            [field("a")], [day_row("2026-06-01")],
            [supply("2026-06-01", minimum=9)], [rule("a")],
            capacity=10, opening=2,
        )
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", data)
            output = str(Path(folder) / "report.json")
            r = self._run(source, output)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(Path(output).read_text())
            self.assertEqual(report["status"], "infeasible")
            self.assertNotIn("decisions", report)
            self.assertNotIn("objective", report)
            self.assertNotIn("reservoir_daily", report)
            self.assertNotIn("water_balance", report)

    def test_invalid_input_exits_2_and_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = self._write(folder, "bad.json", {"fields": [], "weather": [],
                                                   "applications": []})
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = self._run(bad, str(output))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(output.read_text(), "KEEP")

    def test_existing_target_exits_2_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", worked_example())
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = self._run(source, str(output))
            self.assertEqual(r.returncode, 2)
            self.assertIn("exist", (r.stderr + r.stdout).lower())
            self.assertEqual(output.read_text(), "KEEP")

    def test_strict_json_duplicate_keys_rejected(self):
        # 直接构造重复键文本，验证沿用 soil-water 的严格 JSON 读取
        duplicated = '{"a": 1, "a": 2}'
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "dup.json", duplicated)
            output = str(Path(folder) / "report.json")
            r = self._run(source, output)
            self.assertEqual(r.returncode, 2)
            self.assertIn("duplicate JSON key", r.stderr)
            self.assertFalse(Path(output).exists())


if __name__ == "__main__":
    unittest.main()
