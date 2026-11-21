"""irrigation.rain_audit 的自动化测试。

期望值全部在本文件内独立手算或独立枚举给出（逐日平衡用 ``balance.step`` 手推
或显式列出全部序列），不把生产 ``run`` 的返回值再调用一次当期望。覆盖：

- 契约强制算例的 4 条历史、范围端点、建议集合与追查单，及探头变体；
- 重叠分段合计与多田块探头分别有解但共同无解；
- 同一份以不同可能雨量为条件的证据对不同田块签发影响不同；
- 最坏情况优先于最好情况的选择顺序；
- 追查日仍有不同候选值但不能改善任何签发；
- 分数供水效率、探头闭区间等号边界；
- 置换输入、非法输入、证据不一致的不同出口，CLI 失败不覆盖文件；
- 全部雨量只有一个候选且证据一致时与原水账口径相符。
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from irrigation.balance import step
from irrigation.rain_audit import run
from irrigation.service import run as legacy_run

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC}
F = Fraction
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rain-audit-input.json"
DATES = ["2026-09-01", "2026-09-02", "2026-09-03"]


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def field(fid, area=1, capacity=4, opening=0, threshold=3, target=4,
          infiltration=10, efficiency=1000):
    return {
        "id": fid, "area_m2": area, "capacity_mm": capacity,
        "opening_mm": opening, "threshold_mm": threshold, "target_mm": target,
        "infiltration_mm": infiltration, "efficiency_permille": efficiency,
    }


def weather(rows):
    """rows: [(date, et_mm, rain_mm_choices)]"""
    return [{"date": d, "et_mm": e, "rain_mm_choices": list(r)}
            for d, e, r in rows]


def make_data(fields, weather_rows, applications=(), windows=(),
              probes=(), checkable=()):
    return {
        "fields": list(fields), "applications": list(applications),
        "weather": list(weather_rows), "rain_windows": list(windows),
        "probes": list(probes), "checkable_rain_dates": list(checkable),
    }


def fixture_data():
    return json.loads(FIXTURE.read_text())


def advice_for(fields, rain_sequence, ets, applications=()):
    """独立手推：从原期初按 balance.step 逐日推进，返回 (末日建议, 各日结果)。"""
    apps = {(row["field_id"], row["date"]): F(row["gross_mm"])
            for row in applications}
    out = {}
    for f in fields:
        opening = F(f["opening_mm"])
        results = []
        for date, rain, et in zip(DATES, rain_sequence, ets):
            r = step(f, opening, F(rain), apps.get((f["id"], date), F(0)), F(et))
            opening = r["closing_mm"]
            results.append(r)
        out[f["id"]] = (results[-1]["advised_next_gross_mm"], results)
    return out


def field_daily(report, date=None, fid=None):
    rows = report["field_daily"]
    if date is not None:
        rows = [r for r in rows if r["date"] == date]
    if fid is not None:
        rows = [r for r in rows if r["field_id"] == fid]
    return rows


def advice_map(report):
    return {r["field_id"]: r for r in report["next_advice"]}


def range_pair(obj):
    return (F(obj["min"]), F(obj["max"]))


# ---------------------------------------------------------------------------
# 契约强制算例与探头变体
# ---------------------------------------------------------------------------

class MandatoryWorkedExampleTests(unittest.TestCase):
    def setUp(self):
        self.report = run(fixture_data())

    def test_four_histories_and_rain_sets(self):
        self.assertEqual(self.report["status"], "consistent")
        self.assertEqual(self.report["history_count"], 4)
        self.assertEqual(
            [(r["date"], r["possible_rain_mm"]) for r in self.report["rain_daily"]],
            [(DATES[0], [0, 4]), (DATES[1], [0, 4]), (DATES[2], [0, 2])])

    def test_day1_unmet_range(self):
        row = field_daily(self.report, DATES[0], "f")[0]
        self.assertEqual(range_pair(row["unmet_et_mm"]), (F(0), F(2)))

    def test_last_day_closing_and_drainage_ranges(self):
        row = field_daily(self.report, DATES[2], "f")[0]
        self.assertEqual(range_pair(row["closing_mm"]), (F(2), F(4)))
        self.assertEqual(range_pair(row["drainage_mm"]), (F(0), F(2)))
        self.assertEqual(range_pair(row["runoff_mm"]), (F(0), F(0)))

    def test_advice_withheld(self):
        row = advice_map(self.report)["f"]
        self.assertEqual(row["possible_gross_mm"], [0, 2])
        self.assertIsNone(row["released_gross_mm"])

    def test_follow_up_chooses_first_day_with_exact_branches(self):
        follow = self.report["follow_up"]
        self.assertEqual(follow["date"], DATES[0])
        self.assertEqual(follow["worst_withheld_count"], 1)
        self.assertEqual(follow["best_withheld_count"], 0)
        branches = {o["rain_mm"]: o for o in follow["outcomes"]}
        self.assertEqual(sorted(branches), [0, 4])
        self.assertEqual(branches[0]["history_count"], 2)
        self.assertEqual(branches[0]["next_advice"][0]["released_gross_mm"], 0)
        self.assertEqual(branches[4]["history_count"], 2)
        self.assertEqual(branches[4]["next_advice"][0]["possible_gross_mm"], [0, 2])
        self.assertIsNone(branches[4]["next_advice"][0]["released_gross_mm"])

    def test_branch_counts_sum_to_total(self):
        follow = self.report["follow_up"]
        self.assertEqual(
            sum(o["history_count"] for o in follow["outcomes"]),
            self.report["history_count"])


class ProbeVariantTests(unittest.TestCase):
    def setUp(self):
        data = fixture_data()
        data["probes"] = [{"field_id": "f", "date": DATES[1],
                           "min_closing_mm": "4", "max_closing_mm": "4"}]
        self.report = run(data)

    def test_two_histories_with_same_storage_still_distinct(self):
        # 第三天雨量仍可为 0 或 2，但两条历史末日都只存 4mm，仍计两条。
        self.assertEqual(self.report["history_count"], 2)
        self.assertEqual(
            [r["possible_rain_mm"] for r in self.report["rain_daily"]],
            [[0], [4], [0, 2]])

    def test_advice_released_zero_and_no_follow_up(self):
        row = advice_map(self.report)["f"]
        self.assertEqual(row["possible_gross_mm"], [0])
        self.assertEqual(row["released_gross_mm"], 0)
        self.assertIsNone(self.report["follow_up"])

    def test_day1_unmet_becomes_point_range(self):
        row = field_daily(self.report, DATES[0], "f")[0]
        self.assertEqual(range_pair(row["unmet_et_mm"]), (F(2), F(2)))

    def test_later_evidence_excludes_earlier_value(self):
        # 探头在第二天，却把第一天的可能雨量从 [0,4] 收窄到 [0]。
        self.assertEqual(
            self.report["rain_daily"][0]["possible_rain_mm"], [0])


# ---------------------------------------------------------------------------
# 共同无解：分别有解、合并不成立
# ---------------------------------------------------------------------------

class JointInconsistencyTests(unittest.TestCase):
    def test_overlapping_windows_each_ok_but_jointly_contradictory(self):
        base = fixture_data()
        first = {"from_date": DATES[0], "through_date": DATES[1],
                 "total_rain_mm": 0}
        second = {"from_date": DATES[1], "through_date": DATES[2],
                  "total_rain_mm": 6}
        # 分别单独加上都仍有历史。
        for single in (first, second):
            data = copy.deepcopy(base)
            data["rain_windows"] = [single]
            self.assertEqual(run(data)["status"], "consistent")
        # 第一条力迫 d1=d2=0；第二条要求 d2=4、d3=2。两者对 d2 矛盾。
        data = copy.deepcopy(base)
        data["rain_windows"] = [first, second]
        report = run(data)
        self.assertEqual(report["status"], "inconsistent")
        self.assertEqual(report["history_count"], 0)
        self.assertTrue(report["reason"])
        self.assertNotIn("rain_daily", report)
        self.assertNotIn("next_advice", report)
        self.assertNotIn("follow_up", report)

    def test_two_field_probes_each_ok_but_jointly_contradictory(self):
        data = make_data(
            [field("g", capacity=2, threshold=0, target=0),
             field("h", capacity=4, threshold=0, target=0)],
            weather([(DATES[0], 0, [0, 4])]),
            checkable=[DATES[0]])
        probe_g = {"field_id": "g", "date": DATES[0],
                   "min_closing_mm": "2", "max_closing_mm": "2"}
        probe_h = {"field_id": "h", "date": DATES[0],
                   "min_closing_mm": "0", "max_closing_mm": "0"}
        # g 单独要求雨量 4（容量 2 后结存 2），h 单独要求雨量 0。
        alone_g = copy.deepcopy(data)
        alone_g["probes"] = [probe_g]
        self.assertEqual(run(alone_g)["history_count"], 1)
        alone_h = copy.deepcopy(data)
        alone_h["probes"] = [probe_h]
        self.assertEqual(run(alone_h)["history_count"], 1)
        # 同一条历史须同时满足两者，不可能。
        joint = copy.deepcopy(data)
        joint["probes"] = [probe_g, probe_h]
        self.assertEqual(run(joint)["status"], "inconsistent")


# ---------------------------------------------------------------------------
# 同一雨量条件对不同田块签发影响不同
# ---------------------------------------------------------------------------

class PerFieldAdviceTests(unittest.TestCase):
    def setUp(self):
        self.data = make_data(
            [field("A", capacity=5, opening=1, threshold=2, target=2,
                   infiltration=5, efficiency=500),
             field("B", capacity=5, opening=3, threshold=4, target=5,
                   infiltration=5, efficiency=750)],
            weather([(DATES[0], 2, [0, 2]), (DATES[1], 2, [1, 2]),
                     (DATES[2], 2, [3])]),
            checkable=DATES)
        self.report = run(self.data)

    def test_overall_both_withheld(self):
        self.assertEqual(self.report["history_count"], 4)
        advice = advice_map(self.report)
        self.assertIsNone(advice["A"]["released_gross_mm"])
        self.assertIsNone(advice["B"]["released_gross_mm"])

    def test_rain_zero_releases_a_but_not_b(self):
        branch = {o["rain_mm"]: o for o in self.report["follow_up"]["outcomes"]}
        advice_a = {r["field_id"]: r for r in branch[0]["next_advice"]}
        self.assertEqual(advice_a["A"]["released_gross_mm"], 2)
        self.assertIsNone(advice_a["B"]["released_gross_mm"])

    def test_rain_two_releases_b_but_not_a(self):
        branch = {o["rain_mm"]: o for o in self.report["follow_up"]["outcomes"]}
        advice_a = {r["field_id"]: r for r in branch[2]["next_advice"]}
        self.assertIsNone(advice_a["A"]["released_gross_mm"])
        self.assertEqual(advice_a["B"]["released_gross_mm"], 3)

    def test_expected_advice_values_independently_recomputed(self):
        # 独立手推：首日 0/2，次日 1/2，第三日 3。
        expected_zero = advice_for(
            self.data["fields"], [0, 1, 3], [2, 2, 2],
            self.data["applications"])["A"][0]
        self.assertEqual(expected_zero, 2)
        expected_two = advice_for(
            self.data["fields"], [2, 2, 3], [2, 2, 2])["B"][0]
        self.assertEqual(expected_two, 3)


# ---------------------------------------------------------------------------
# 最坏情况优先于最好情况
# ---------------------------------------------------------------------------

class RankingTests(unittest.TestCase):
    def test_worst_case_beats_better_best_case(self):
        data = make_data(
            [field("A", capacity=4, opening=3, threshold=4, target=4,
                   infiltration=10, efficiency=750),
             field("B", capacity=4, opening=2, threshold=1, target=4,
                   infiltration=1, efficiency=750),
             field("C", capacity=2, opening=0, threshold=1, target=1,
                   infiltration=10, efficiency=500)],
            weather([(DATES[0], 2, [0, 4]), (DATES[1], 1, [1, 4]),
                     (DATES[2], 1, [1, 4, 5])]),
            checkable=DATES)
        follow = run(data)["follow_up"]
        # 第 1 日（最坏 2 / 最好 2）优于第 2 日（最坏 3 / 最好 1）。
        self.assertEqual(follow["date"], DATES[0])
        self.assertEqual(follow["worst_withheld_count"], 2)
        self.assertEqual(follow["best_withheld_count"], 2)

    def test_best_case_breaks_tie_when_worst_equal(self):
        # 强制算例：第 1、2、3 日最坏都是 1，最好都是 0，取最早第 1 日。
        report = run(fixture_data())
        self.assertEqual(report["follow_up"]["date"], DATES[0])


# ---------------------------------------------------------------------------
# 无法改善任何签发的追查日被排除
# ---------------------------------------------------------------------------

class NonImprovingDateTests(unittest.TestCase):
    def test_only_improving_dates_are_eligible(self):
        data = make_data(
            [field("f", capacity=4, opening=2, threshold=3, target=3,
                   infiltration=10, efficiency=1000)],
            weather([(DATES[0], 1, [0, 3]), (DATES[1], 1, [0, 1]),
                     (DATES[2], 2, [0, 4])]),
            checkable=DATES)
        report = run(data)
        self.assertEqual(report["history_count"], 8)
        advice = advice_map(report)["f"]
        self.assertEqual(advice["possible_gross_mm"], [1, 2, 3])
        follow = report["follow_up"]
        # 第 1、2 日查到任何值都无法把建议收窄到单值；只有第 3 日可以。
        self.assertEqual(follow["date"], DATES[2])
        self.assertEqual([o["rain_mm"] for o in follow["outcomes"]], [0, 4])
        self.assertEqual(follow["worst_withheld_count"], 1)
        self.assertEqual(follow["best_withheld_count"], 0)
        branch = {o["rain_mm"]: o["next_advice"][0] for o in follow["outcomes"]}
        # 查到 0 时仍有 2、3 两种建议；查到 4 才唯一签发 1。
        self.assertEqual(branch[0]["possible_gross_mm"], [2, 3])
        self.assertIsNone(branch[0]["released_gross_mm"])
        self.assertEqual(branch[4]["possible_gross_mm"], [1])
        self.assertEqual(branch[4]["released_gross_mm"], 1)

    def test_no_checkable_dates_means_null_follow_up(self):
        data = fixture_data()
        data["checkable_rain_dates"] = []
        self.assertIsNone(run(data)["follow_up"])

    def test_no_withheld_fields_means_null_follow_up(self):
        data = fixture_data()
        data["probes"] = [{"field_id": "f", "date": DATES[1],
                           "min_closing_mm": "4", "max_closing_mm": "4"}]
        self.assertIsNone(run(data)["follow_up"])


# ---------------------------------------------------------------------------
# 精确分数与边界
# ---------------------------------------------------------------------------

class ExactBoundaryTests(unittest.TestCase):
    def test_fractional_efficiency_advice(self):
        # 效率 700‰、target 3、阈值 1、结存 0：毛供水 ceil(3 / (7/10))=ceil(30/7)=5。
        data = make_data(
            [field("f", capacity=3, opening=0, threshold=1, target=3,
                   infiltration=10, efficiency=700)],
            weather([(DATES[0], 1, [0])]),
            checkable=[DATES[0]])
        report = run(data)
        row = field_daily(report, DATES[0], "f")[0]
        self.assertEqual(range_pair(row["closing_mm"]), (F(0), F(0)))
        self.assertEqual(advice_map(report)["f"]["released_gross_mm"], 5)

    def test_probe_inclusive_equality_boundaries(self):
        # 探头闭区间恰好等于可能结存：等于上界和下界都应被接受。
        data = make_data(
            [field("f", capacity=4, opening=0, threshold=3, target=4)],
            weather([(DATES[0], 2, [0, 4])]),
            probes=[{"field_id": "f", "date": DATES[0],
                     "min_closing_mm": "0", "max_closing_mm": "2"}],
            checkable=[DATES[0]])
        report = run(data)
        self.assertEqual(report["history_count"], 2)  # 0 和 2 两端都在界内
        # 下界收到 1 就排除了 0，只剩 2。
        data["probes"][0]["min_closing_mm"] = "1"
        self.assertEqual(run(data)["history_count"], 1)

    def test_fractional_probe_bounds_parse_exactly(self):
        data = make_data(
            [field("f", capacity=4, opening=0, threshold=3, target=4,
                   efficiency=500)],
            weather([(DATES[0], 0, [0, 1])]),
            applications=[{"field_id": "f", "date": DATES[0],
                           "gross_mm": 1}],
            probes=[{"field_id": "f", "date": DATES[0],
                     "min_closing_mm": "1/2", "max_closing_mm": "1/2"}])
        # 1mm 毛供水、效率 500‰ => 到田 1/2mm；只有候选雨量 0 使结存恰为 1/2。
        report = run(data)
        self.assertEqual(report["history_count"], 1)
        self.assertEqual(
            report["rain_daily"][0]["possible_rain_mm"], [0])


# ---------------------------------------------------------------------------
# 与原水账口径一致
# ---------------------------------------------------------------------------

class LegacyParityTests(unittest.TestCase):
    def test_single_candidate_matches_legacy_service(self):
        legacy = json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "input.json").read_text())
        data = make_data(
            legacy["fields"],
            [{"date": row["date"], "et_mm": row["et_mm"],
              "rain_mm_choices": [row["rain_mm"]]}
             for row in legacy["weather"]],
            legacy["applications"])
        report = run(data)
        expected = legacy_run(legacy)
        self.assertEqual(report["history_count"], 1)
        by_key = {(r["date"], r["field_id"]): r for r in report["field_daily"]}
        for row in expected["daily"]:
            got = by_key[(row["date"], row["field_id"])]
            for key in ("closing_mm", "unmet_et_mm", "runoff_mm",
                        "drainage_mm"):
                lo, hi = range_pair(got[key])
                self.assertEqual(lo, F(row[key]))
                self.assertEqual(hi, F(row[key]))
        advice = advice_map(report)
        for row in expected["daily"][-1:]:
            self.assertEqual(advice[row["field_id"]]["released_gross_mm"],
                             row["advised_next_gross_mm"])

    def test_continuity_uses_real_previous_closing_not_probe_bounds(self):
        # 若把探头下界当新期初，第 2 日会凭空多出水；这里断言真实连续。
        data = make_data(
            [field("f", capacity=4, opening=2, threshold=0, target=0)],
            weather([(DATES[0], 0, [0]), (DATES[1], 0, [0])]),
            probes=[{"field_id": "f", "date": DATES[1],
                     "min_closing_mm": "2", "max_closing_mm": "2"}])
        report = run(data)
        day2 = field_daily(report, DATES[1], "f")[0]
        # 真实期初 2mm、ET 0：结存仍是 2mm，而不是把下界 2mm 当“新期初”。
        self.assertEqual(range_pair(day2["closing_mm"]), (F(2), F(2)))
        # ET 1mm 时真实结存 1mm，却低于探头下界 2mm，应无解。
        data["weather"][1]["et_mm"] = 1
        self.assertEqual(run(data)["status"], "inconsistent")


# ---------------------------------------------------------------------------
# 置换、输入不被修改
# ---------------------------------------------------------------------------

class PermutationTests(unittest.TestCase):
    def test_reordering_does_not_change_report(self):
        base = fixture_data()
        shuffled = copy.deepcopy(base)
        shuffled["fields"].reverse()
        shuffled["rain_windows"].reverse()
        shuffled["checkable_rain_dates"].reverse()
        for row in shuffled["weather"]:
            row["rain_mm_choices"].reverse()
        shuffled["applications"] = [
            {"field_id": "f", "date": DATES[0], "gross_mm": 1},
            {"field_id": "f", "date": DATES[2], "gross_mm": 0}]
        base["applications"] = list(reversed(shuffled["applications"]))
        self.assertEqual(run(base), run(shuffled))

    def test_input_object_is_not_modified(self):
        data = fixture_data()
        data["applications"] = [{"field_id": "f", "date": DATES[0],
                                 "gross_mm": 1}]
        snapshot = copy.deepcopy(data)
        run(data)
        self.assertEqual(data, snapshot)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.good = fixture_data()

    def assert_input_error(self, mutate):
        data = copy.deepcopy(self.good)
        mutate(data)
        with self.assertRaises(ValueError):
            run(data)

    def test_root_keys_exact(self):
        self.assert_input_error(lambda d: d.update(extra=1))
        self.assert_input_error(lambda d: d.pop("probes"))
        self.assert_input_error(lambda d: d.pop("checkable_rain_dates"))

    def test_weather_object_keys_exact(self):
        self.assert_input_error(
            lambda d: d["weather"][0].update(rain_mm=0))
        self.assert_input_error(
            lambda d: d["weather"][0].pop("et_mm"))

    def test_choices_validation(self):
        self.assert_input_error(
            lambda d: d["weather"][0].__setitem__("rain_mm_choices", []))
        self.assert_input_error(
            lambda d: d["weather"][0].__setitem__(
                "rain_mm_choices", [0, 0]))
        self.assert_input_error(
            lambda d: d["weather"][0].__setitem__(
                "rain_mm_choices", [0, 1, 2, 3]))
        self.assert_input_error(
            lambda d: d["weather"][0].__setitem__(
                "rain_mm_choices", [-1]))
        self.assert_input_error(
            lambda d: d["weather"][0].__setitem__(
                "rain_mm_choices", [True]))

    def test_more_than_nine_multi_choice_days_rejected(self):
        from datetime import date, timedelta
        start = date(2026, 1, 1)
        rows = []
        for i in range(10):
            rows.append({"date": (start + timedelta(days=i)).isoformat(),
                         "et_mm": 0, "rain_mm_choices": [0, 1]})
        data = make_data([field("f")], rows)
        with self.assertRaisesRegex(ValueError, "9"):
            run(data)
        # 九天为合法上界。
        data["weather"] = rows[:9]
        run(data)

    def test_field_scale_limits(self):
        self.assert_input_error(lambda d: d.__setitem__("fields", []))
        data = copy.deepcopy(self.good)
        data["fields"] = [field(f"f{i}") for i in range(5)]
        with self.assertRaisesRegex(ValueError, "4"):
            run(data)

    def test_window_structure(self):
        self.assert_input_error(
            lambda d: d["rain_windows"].append(
                {"from_date": DATES[1], "through_date": DATES[0],
                 "total_rain_mm": 1}))
        self.assert_input_error(
            lambda d: d["rain_windows"].append(
                {"from_date": DATES[0], "through_date": "2026-10-01",
                 "total_rain_mm": 1}))
        self.assert_input_error(
            lambda d: d["rain_windows"].append(dict(d["rain_windows"][0])))
        self.assert_input_error(
            lambda d: d["rain_windows"][0].update(extra=1))

    def test_probe_structure_and_fraction_strings(self):
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "nope", "date": DATES[0],
                 "min_closing_mm": "0", "max_closing_mm": "0"}))
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "2", "max_closing_mm": "1"}))
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "0", "max_closing_mm": "5"}))  # 超容量
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": 0.5, "max_closing_mm": "1"}))
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "2/4", "max_closing_mm": "1"}))
        self.assert_input_error(
            lambda d: d["probes"].append(
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "01", "max_closing_mm": "1"}))
        # 同一田块日重复。
        self.assert_input_error(
            lambda d: d["probes"].extend([
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "0", "max_closing_mm": "0"},
                {"field_id": "f", "date": DATES[0],
                 "min_closing_mm": "0", "max_closing_mm": "0"}]))

    def test_checkable_dates(self):
        self.assert_input_error(
            lambda d: d["checkable_rain_dates"].append("2026-10-01"))
        self.assert_input_error(
            lambda d: d["checkable_rain_dates"].append(DATES[0]))

    def test_application_validation(self):
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "x", "date": DATES[0], "gross_mm": 1}))
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "f", "date": "2026-10-01", "gross_mm": 1}))
        self.assert_input_error(
            lambda d: d["applications"].extend([
                {"field_id": "f", "date": DATES[0], "gross_mm": 1},
                {"field_id": "f", "date": DATES[0], "gross_mm": 2}]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def _write(self, folder, name, payload):
        path = Path(folder) / name
        if isinstance(payload, str):
            path.write_text(payload)
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False))
        return str(path)

    def _run(self, source, output):
        return subprocess.run(
            [sys.executable, "-m", "irrigation.rain_audit_cli",
             "--source", source, "--output", output],
            capture_output=True, text=True, env=ENV)

    def test_success_writes_report_exits_zero_and_matches_api(self):
        data = fixture_data()
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", data)
            output = str(Path(folder) / "report.json")
            r = self._run(source, output)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(Path(output).read_text()), run(data))

    def test_inconsistent_still_exits_zero(self):
        data = fixture_data()
        data["rain_windows"].append(
            {"from_date": DATES[2], "through_date": DATES[2],
             "total_rain_mm": 1})
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", data)
            output = Path(folder) / "report.json"
            r = self._run(source, str(output))
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "inconsistent")

    def test_invalid_input_exits_2_and_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = self._write(folder, "bad.json", {"fields": []})
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = self._run(bad, str(output))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(output.read_text(), "KEEP")

    def test_existing_target_exits_2_and_is_preserved(self):
        data = fixture_data()
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", data)
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = self._run(source, str(output))
            self.assertEqual(r.returncode, 2)
            self.assertIn("exist", (r.stderr + r.stdout).lower())
            self.assertEqual(output.read_text(), "KEEP")

    def test_strict_json_duplicate_keys_and_nan_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            dup = self._write(folder, "dup.json", '{"a": 1, "a": 2}')
            r = self._run(dup, str(Path(folder) / "o1.json"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("duplicate JSON key", r.stderr)
            nan = self._write(folder, "nan.json", '{"x": NaN}')
            r2 = self._run(nan, str(Path(folder) / "o2.json"))
            self.assertEqual(r2.returncode, 2)

    def test_missing_source_file_is_os_error_exit_2(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self._run(str(Path(folder) / "nope.json"),
                          str(Path(folder) / "o.json"))
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
