"""irrigation.reuse_water 的自动化测试。

期望值全部在本文件内独立手算（线性方程、水量平衡逐步推导）给出，不把生产
``run`` 的返回值再调用一次当期望；水盐守恒只作为附加核对，不能替代浓度、
拦截与携盐的独立数值期望。覆盖契约要求的全部区分点：

- 锚定底水的同日循环混合 vs 无底水伪循环；
- 输水数组置换不改变结果；
- 跨池精确分数浓度与“等于限值允许入田”；
- 回流延迟造成次日水质拦截，被拒水仍出池且不进田；
- 蒸发只移水、干盐再溶解（池与土壤两处）；
- 雨水稀释先于径流、土壤混合先于渗漏；
- 回收比例、未回收排水与末日 pending_returns；
- 严格 JSON、失败不发布、退出码与旧接口不变。
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

from irrigation.reuse_water import run

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC}
F = Fraction


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def field(fid="f", area=1, capacity=100, opening=0, threshold=0, target=0,
          infiltration=100, efficiency=1000):
    return {
        "id": fid, "area_m2": area, "capacity_mm": capacity,
        "opening_mm": opening, "threshold_mm": threshold, "target_mm": target,
        "infiltration_mm": infiltration, "efficiency_permille": efficiency,
    }


def weather(dates, rain=0, et=0):
    if isinstance(rain, int):
        rain = [rain] * len(dates)
    if isinstance(et, int):
        et = [et] * len(dates)
    return [{"date": d, "rain_mm": r, "et_mm": e}
            for d, r, e in zip(dates, rain, et)]


def tank(tid, capacity=1000, opening=0, opening_salt=0):
    return {"id": tid, "capacity_litres": capacity,
            "opening_litres": opening, "opening_salt_mg": opening_salt}


def reuse(fid="f", opening_salt=0, limit=10 ** 9, permille=0,
          return_tank="a"):
    return {"field_id": fid, "opening_salt_mg": opening_salt,
            "max_source_mg_per_litre": limit, "return_permille": permille,
            "return_tank_id": return_tank}


def op(date, inflows=(), evaporations=(), transfers=(), withdrawals=()):
    return {"date": date, "inflows": list(inflows),
            "evaporations": list(evaporations),
            "transfers": list(transfers), "withdrawals": list(withdrawals)}


def make_data(fields, weather_rows, tanks, reuse_rows, operations):
    return {"fields": fields, "weather": weather_rows, "tanks": tanks,
            "field_reuse": reuse_rows, "operations": operations}


# ---------------------------------------------------------------------------
# 契约规定的两日可复核算例
# ---------------------------------------------------------------------------

class MandatoryTwoDayTests(unittest.TestCase):
    def data(self):
        return json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "reuse-input.json").read_text()
        )

    def test_mandatory_two_day_worked_case(self):
        report = run(self.data())
        t1, t2 = report["tank_daily"]
        d1, d2 = report["deliveries"]
        f1, f2 = report["field_daily"]

        # 首日：淡水获准；田间渗漏 5L 携盐 100/7；土壤期末 2mm、盐 40/7。
        self.assertEqual(t1["opening_litres"], "10")
        self.assertEqual(t1["concentration_mg_per_litre"], "0")
        self.assertEqual(t1["withdrawn_litres"], "5")
        self.assertEqual(t1["closing_litres"], "5")
        self.assertEqual(t1["closing_salt_mg"], "0")
        self.assertTrue(d1["accepted"])
        self.assertEqual(d1["applied_litres"], "5")
        self.assertEqual(f1["water_balance"]["gross_irrigation_mm"], "5")
        self.assertEqual(f1["water_balance"]["drainage_mm"], "5")
        self.assertEqual(f1["water_balance"]["closing_mm"], "2")
        self.assertEqual(f1["drainage_salt_mg"], "100/7")
        self.assertEqual(f1["closing_salt_mg"], "40/7")
        self.assertEqual(f1["recovered_litres"], "5")
        self.assertEqual(f1["recovered_salt_mg"], "100/7")

        # 次日早回流到账：10L、盐 100/7，浓度 10/7；1L 抽水全部拦截。
        self.assertEqual(t2["opening_litres"], "5")
        self.assertEqual(t2["return_litres"], "5")
        self.assertEqual(t2["return_salt_mg"], "100/7")
        self.assertEqual(t2["mixed_litres"], "10")
        self.assertEqual(t2["concentration_mg_per_litre"], "10/7")
        self.assertFalse(d2["accepted"])
        self.assertEqual(d2["source_mg_per_litre"], "10/7")
        self.assertEqual(d2["applied_litres"], "0")
        self.assertEqual(d2["applied_salt_mg"], "0")
        self.assertEqual(d2["rejected_litres"], "1")
        self.assertEqual(d2["rejected_salt_mg"], "10/7")
        # 拒水仍出池：池末 9L、盐 90/7。
        self.assertEqual(t2["withdrawn_litres"], "1")
        self.assertEqual(t2["withdrawn_salt_mg"], "10/7")
        self.assertEqual(t2["closing_litres"], "9")
        self.assertEqual(t2["closing_salt_mg"], "90/7")
        # 田块当天零灌溉，ET 消耗 1mm 纯水，盐仍为 40/7。
        self.assertEqual(f2["water_balance"]["gross_irrigation_mm"], "0")
        self.assertEqual(f2["water_balance"]["actual_et_mm"], "1")
        self.assertEqual(f2["water_balance"]["closing_mm"], "1")
        self.assertEqual(f2["opening_salt_mg"], "40/7")
        self.assertEqual(f2["closing_salt_mg"], "40/7")

        # 末日没有回流。
        self.assertEqual(report["pending_returns"], [])

        # 全系统水守恒：期初 12 = 末存 10 + 拒水 1 + ET 1。
        wb = report["system_balance"]["water_litres"]
        self.assertEqual(wb["opening"], "12")
        self.assertEqual(wb["closing"], "10")
        self.assertEqual(wb["rejected"], "1")
        self.assertEqual(wb["actual_et"], "1")
        self.assertEqual(wb["residual"], "0")
        # 全系统盐守恒：期初 20 = 末存 130/7 + 弃盐 10/7。
        sb = report["system_balance"]["salt_mg"]
        self.assertEqual(sb["opening"], "20")
        self.assertEqual(sb["closing"], "130/7")
        self.assertEqual(sb["rejected"], "10/7")
        self.assertEqual(F(sb["closing"]) + F(sb["rejected"]), F(20))
        self.assertEqual(sb["residual"], "0")

        # 每行残差严格为字符串 "0"。
        for row in report["tank_daily"]:
            self.assertEqual(row["water_residual_litres"], "0")
            self.assertEqual(row["salt_residual_mg"], "0")
        for row in report["field_daily"]:
            self.assertEqual(row["salt_residual_mg"], "0")
            self.assertEqual(row["water_balance"]["balance_residual_mm"], "0")

    def test_sorting_is_business_order_independent_of_input(self):
        report = run(self.data())
        self.assertEqual(
            [(r["date"], r["tank_id"]) for r in report["tank_daily"]],
            [("2026-10-01", "r"), ("2026-10-02", "r")])
        self.assertEqual(
            [(r["date"], r["field_id"]) for r in report["deliveries"]],
            [("2026-10-01", "f"), ("2026-10-02", "f")])
        self.assertEqual(
            [(r["field_id"], r["date"]) for r in report["field_daily"]],
            [("f", "2026-10-01"), ("f", "2026-10-02")])


# ---------------------------------------------------------------------------
# 同日完全混合：环路、伪循环、置换不变、精确分数
# ---------------------------------------------------------------------------

class SameDayMixingTests(unittest.TestCase):
    def _two_tanks(self):
        dates = ["2026-11-01"]
        return make_data(
            [field()], weather(dates),
            [tank("a", opening=10, opening_salt=100),
             tank("b", opening=0, opening_salt=0)],
            [reuse()], [op(dates[0])])

    def test_anchored_cycle_equalizes_concentration(self):
        # a:10L/100mg；a->b 4L、b->a 3L。闭合环路无外加盐，两池浓度同为 10。
        # 独立方程：13 Ca - 3 Cb = 100；4 Cb - 4 Ca = 0 => Ca = Cb = 10。
        d = self._two_tanks()
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 4},
            {"from_tank_id": "b", "to_tank_id": "a", "water_litres": 3}]
        rows = {r["tank_id"]: r for r in run(d)["tank_daily"]}
        self.assertEqual(rows["a"]["mixed_litres"], "13")
        self.assertEqual(rows["b"]["mixed_litres"], "4")
        self.assertEqual(rows["a"]["concentration_mg_per_litre"], "10")
        self.assertEqual(rows["b"]["concentration_mg_per_litre"], "10")
        # 携盐按来源混合浓度：a 出 4L 携 40mg，b 出 3L 携 30mg。
        self.assertEqual(rows["a"]["transfer_out_salt_mg"], "40")
        self.assertEqual(rows["a"]["transfer_in_salt_mg"], "30")
        self.assertEqual(rows["b"]["transfer_in_salt_mg"], "40")
        self.assertEqual(rows["b"]["transfer_out_salt_mg"], "30")

    def test_fractional_cross_tank_concentrations(self):
        # a:10L/100mg（10），b:10L/0mg（0）；a->b 4、b->a 6。
        # 16 Ca - 6 Cb = 100；14 Cb - 4 Ca = 0 => Ca = 7、Cb = 2。
        d = self._two_tanks()
        d["tanks"][1] = tank("b", opening=10, opening_salt=0)
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 4},
            {"from_tank_id": "b", "to_tank_id": "a", "water_litres": 6}]
        rows = {r["tank_id"]: r for r in run(d)["tank_daily"]}
        self.assertEqual(rows["a"]["concentration_mg_per_litre"], "7")
        self.assertEqual(rows["b"]["concentration_mg_per_litre"], "2")
        # a 出 4L 携 28mg；b 出 6L 携 12mg。
        self.assertEqual(rows["a"]["transfer_out_salt_mg"], "28")
        self.assertEqual(rows["b"]["transfer_out_salt_mg"], "12")
        # 期末盐量：a = 100 + 12 - 28 = 84；b = 0 + 28 - 12 = 16。
        self.assertEqual(rows["a"]["closing_salt_mg"], "84")
        self.assertEqual(rows["b"]["closing_salt_mg"], "16")

    def test_fractional_concentration_with_external_salty_water(self):
        # a:4L/8mg（2）；外来入 a 2L/10mg => 底水 6L/18mg（3）；a->b 3L，
        # b 原本空。闭合系统无外加盐于混合期，混匀后两池浓度同为 18/6 = 3。
        # 独立方程：6 Ca - 3 Cb = 18；3 Cb - 3 Ca = 0 => Ca = Cb = 3。
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=4, opening_salt=8), tank("b")],
            [reuse()],
            [op(dates[0],
                inflows=[{"tank_id": "a", "water_litres": 2, "salt_mg": 10}],
                transfers=[{"from_tank_id": "a", "to_tank_id": "b",
                            "water_litres": 3}])])
        rows = {r["tank_id"]: r for r in run(d)["tank_daily"]}
        self.assertEqual(rows["a"]["external_litres"], "2")
        self.assertEqual(rows["a"]["external_salt_mg"], "10")
        self.assertEqual(rows["a"]["concentration_mg_per_litre"], "3")
        self.assertEqual(rows["b"]["concentration_mg_per_litre"], "3")
        # b 收到 3L × 3 = 9mg 盐。
        self.assertEqual(rows["b"]["transfer_in_salt_mg"], "9")

    def test_pseudo_cycle_without_base_water_is_rejected(self):
        # 两池均无底水，仅靠互相输水不能造水，即使进出表面平衡也整批失败。
        d = self._two_tanks()
        d["tanks"][0] = tank("a", opening=0, opening_salt=0)
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 2},
            {"from_tank_id": "b", "to_tank_id": "a", "water_litres": 2}]
        with self.assertRaisesRegex(ValueError, "底水"):
            run(d)

    def test_cycle_reachable_from_anchor_is_accepted(self):
        # a 有底水，a->b、b->c、c->b 成环：b/c 沿正向路径可从 a 到达。
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=10, opening_salt=0), tank("b"), tank("c")],
            [reuse()],
            [op(dates[0], transfers=[
                {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 4},
                {"from_tank_id": "b", "to_tank_id": "c", "water_litres": 2},
                {"from_tank_id": "c", "to_tank_id": "b", "water_litres": 2}])])
        report = run(d)  # 不抛异常即通过；全淡水浓度为 0
        for row in report["tank_daily"]:
            self.assertEqual(row["concentration_mg_per_litre"], "0")

    def test_transfer_array_permutation_does_not_change_report(self):
        d = self._two_tanks()
        d["tanks"][1] = tank("b", opening=10, opening_salt=0)
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 4},
            {"from_tank_id": "b", "to_tank_id": "a", "water_litres": 6}]
        shuffled = copy.deepcopy(d)
        shuffled["operations"][0]["transfers"].reverse()
        # 同时打乱四个操作数组与 tanks/field_reuse 排列。
        shuffled["tanks"].reverse()
        shuffled["field_reuse"].reverse()
        self.assertEqual(run(d), run(shuffled))

    def test_reverse_edges_may_coexist_but_duplicate_pair_rejected(self):
        d = self._two_tanks()
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 1},
            {"from_tank_id": "b", "to_tank_id": "a", "water_litres": 1}]
        run(d)  # 反向记录允许同时存在
        d["operations"][0]["transfers"].append(
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 1})
        with self.assertRaisesRegex(ValueError, "有向池对"):
            run(d)

    def test_overdraft_beyond_mixed_water_rejected(self):
        d = self._two_tanks()
        d["operations"][0]["transfers"] = [
            {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 11}]
        with self.assertRaisesRegex(ValueError, "混合水量"):
            run(d)

    def test_dry_salt_in_receiving_tank_dissolves_via_transfer(self):
        # b 空池但留有 9mg 干盐；a 有 3L 淡水，a->b 3L 后 b 重新有水，
        # 9mg 干盐全部参与溶解：b 浓度 3 mg/L，a 浓度 0。
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=3, opening_salt=0),
             tank("b", opening=0, opening_salt=9)],
            [reuse(return_tank="b")],
            [op(dates[0], transfers=[
                {"from_tank_id": "a", "to_tank_id": "b", "water_litres": 3}])])
        rows = {r["tank_id"]: r for r in run(d)["tank_daily"]}
        self.assertEqual(rows["a"]["concentration_mg_per_litre"], "0")
        self.assertEqual(rows["b"]["concentration_mg_per_litre"], "3")
        self.assertEqual(rows["b"]["opening_salt_mg"], "9")
        self.assertEqual(rows["b"]["closing_salt_mg"], "9")

    def test_one_tank_feeds_two_fields_with_same_source_concentration(self):
        dates = ["2026-11-01"]
        d = make_data(
            [field("f"), field("g", area=2)], weather(dates),
            [tank("a", opening=10, opening_salt=50)],  # 浓度 5
            [reuse("f", limit=4), reuse("g", limit=6)],
            [op(dates[0], withdrawals=[
                {"field_id": "g", "tank_id": "a", "gross_litres": 2},
                {"field_id": "f", "tank_id": "a", "gross_litres": 3}])])
        report = run(d)
        by_field = {r["field_id"]: r for r in report["deliveries"]}
        # 同一来源浓度 5：f 限值 4 被拒，g 限值 6 获准。
        self.assertEqual(by_field["f"]["source_mg_per_litre"], "5")
        self.assertFalse(by_field["f"]["accepted"])
        self.assertEqual(by_field["f"]["rejected_salt_mg"], "15")
        self.assertTrue(by_field["g"]["accepted"])
        self.assertEqual(by_field["g"]["applied_salt_mg"], "10")
        # 池合计出 5L、携盐 25mg，余 5L/25mg。
        row = report["tank_daily"][0]
        self.assertEqual(row["withdrawn_litres"], "5")
        self.assertEqual(row["withdrawn_salt_mg"], "25")
        self.assertEqual(row["closing_litres"], "5")
        self.assertEqual(row["closing_salt_mg"], "25")


# ---------------------------------------------------------------------------
# 水质拦截
# ---------------------------------------------------------------------------

class InterceptionTests(unittest.TestCase):
    def _salty_tank(self, limit):
        dates = ["2026-11-01"]
        return make_data(
            [field()], weather(dates),
            [tank("a", opening=10, opening_salt=100)],  # 浓度 10 mg/L
            [reuse(limit=limit)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 2}])])

    def test_limit_equal_to_concentration_is_accepted(self):
        report = run(self._salty_tank(limit=10))
        delivery = report["deliveries"][0]
        self.assertTrue(delivery["accepted"])
        self.assertEqual(delivery["applied_litres"], "2")
        self.assertEqual(delivery["applied_salt_mg"], "20")
        self.assertEqual(delivery["rejected_litres"], "0")

    def test_strictly_over_limit_is_rejected_and_leaves_system(self):
        report = run(self._salty_tank(limit=9))
        delivery = report["deliveries"][0]
        self.assertFalse(delivery["accepted"])
        self.assertEqual(delivery["rejected_litres"], "2")
        self.assertEqual(delivery["rejected_salt_mg"], "20")
        self.assertEqual(delivery["applied_litres"], "0")
        # 水盐确实出池，不退回到池中。
        row = report["tank_daily"][0]
        self.assertEqual(row["withdrawn_litres"], "2")
        self.assertEqual(row["withdrawn_salt_mg"], "20")
        self.assertEqual(row["closing_litres"], "8")
        self.assertEqual(row["closing_salt_mg"], "80")
        # 田块当天按零灌溉计算。
        self.assertEqual(
            report["field_daily"][0]["water_balance"]["gross_irrigation_mm"],
            "0")
        self.assertEqual(report["field_daily"][0]["irrigation_salt_mg"], "0")
        # 系统账把拒水记为外流。
        self.assertEqual(
            report["system_balance"]["water_litres"]["rejected"], "2")
        self.assertEqual(
            report["system_balance"]["salt_mg"]["rejected"], "20")

    def test_zero_litre_withdrawal_from_dry_tank_has_null_concentration(self):
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=0, opening_salt=5)],  # 干盐
            [reuse(limit=0)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 0}])])
        delivery = run(d)["deliveries"][0]
        self.assertIsNone(delivery["source_mg_per_litre"])
        self.assertTrue(delivery["accepted"])
        self.assertEqual(delivery["requested_litres"], "0")
        row = run(d)["tank_daily"][0]
        self.assertIsNone(row["concentration_mg_per_litre"])
        self.assertEqual(row["closing_salt_mg"], "5")  # 干盐保留

class DelayedReturnInterceptionTests(unittest.TestCase):
    def test_return_arrives_next_morning_and_then_blocks_pump(self):
        # 池 a：首日 4L、浓度 1mg/L，抽 4L 到田；田块容量 2mm，期初 2mm 水，
        # 4mm 到田后储至 6mm，渗漏 4mm=4L 并全量回流。次日池浓度 = 回流盐/4L。
        dates = ["2026-11-01", "2026-11-02"]
        d = make_data(
            [field(opening=2, capacity=2, infiltration=100)],
            weather(dates),
            [tank("a", opening=4, opening_salt=4)],  # 浓度 1 mg/L
            [reuse(opening_salt=20, limit=1, permille=1000)],
            [
                op(dates[0], withdrawals=[
                    {"field_id": "f", "tank_id": "a", "gross_litres": 4}]),
                op(dates[1], withdrawals=[
                    {"field_id": "f", "tank_id": "a", "gross_litres": 1}]),
            ])
        report = run(d)
        f1 = report["field_daily"][0]
        t1, t2 = report["tank_daily"]
        # 首日：土壤 2mm/20mg + 4L 含 4mg 盐 -> 6mm/24mg，浓度 4；
        # 容量 2mm -> 渗漏 4mm 携盐 24*4/6 = 16mg，留 2mm/8mg；4L 全回流。
        self.assertEqual(f1["water_balance"]["drainage_mm"], "4")
        self.assertEqual(f1["drainage_salt_mg"], "16")
        self.assertEqual(f1["closing_salt_mg"], "8")
        # 首日池被抽 4L 抽干，回流当日不可用。
        self.assertEqual(t1["closing_litres"], "0")
        self.assertEqual(t1["closing_salt_mg"], "0")
        # 次日早：池期初 0L + 回流 4L/16mg，浓度 4 mg/L > 限值 1。
        self.assertEqual(t2["opening_litres"], "0")
        self.assertEqual(t2["return_litres"], "4")
        self.assertEqual(t2["return_salt_mg"], "16")
        self.assertEqual(t2["concentration_mg_per_litre"], "4")
        d2 = report["deliveries"][1]
        self.assertFalse(d2["accepted"])
        self.assertEqual(d2["rejected_litres"], "1")
        self.assertEqual(d2["rejected_salt_mg"], "4")
        # 拒水出池：池末 3L/12mg。
        self.assertEqual(t2["closing_litres"], "3")
        self.assertEqual(t2["closing_salt_mg"], "12")


# ---------------------------------------------------------------------------
# 蒸发、干盐与溢流
# ---------------------------------------------------------------------------

class EvaporationDrySaltSpillTests(unittest.TestCase):
    def test_evaporation_removes_only_water_and_dry_salt_redissolves(self):
        dates = ["2026-11-01", "2026-11-02"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=10, opening_salt=100)],
            [reuse()],
            [op(dates[0], evaporations=[{"tank_id": "a", "water_litres": 10}]),
             op(dates[1], inflows=[
                 {"tank_id": "a", "water_litres": 10, "salt_mg": 0}])])
        rows = {r["date"]: r for r in run(d)["tank_daily"]}
        day1, day2 = rows["2026-11-01"], rows["2026-11-02"]
        self.assertEqual(day1["evaporation_litres"], "10")
        self.assertEqual(day1["closing_litres"], "0")
        self.assertEqual(day1["closing_salt_mg"], "100")  # 盐全部留下
        self.assertIsNone(day1["concentration_mg_per_litre"])
        # 次日 10L 淡水使 100mg 干盐全部重新溶解，浓度 10。
        self.assertEqual(day2["opening_litres"], "0")
        self.assertEqual(day2["opening_salt_mg"], "100")
        self.assertEqual(day2["concentration_mg_per_litre"], "10")

    def test_evaporation_cannot_be_covered_by_same_day_inflow(self):
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", opening=3, opening_salt=0)],
            [reuse()],
            [op(dates[0],
                inflows=[{"tank_id": "a", "water_litres": 10, "salt_mg": 0}],
                evaporations=[{"tank_id": "a", "water_litres": 4}])])
        with self.assertRaisesRegex(ValueError, "蒸发"):
            run(d)

    def test_spill_happens_after_outflow_and_carries_concentration_salt(self):
        # 容量 8、期初 8L/80mg（浓度10）；来淡水 3L、抽 1L 后 10L > 8，
        # 溢流 2L，当日浓度 80/11，溢盐 160/11。
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", capacity=8, opening=8, opening_salt=80)],
            [reuse()],
            [op(dates[0],
                inflows=[{"tank_id": "a", "water_litres": 3, "salt_mg": 0}],
                withdrawals=[{"field_id": "f", "tank_id": "a",
                              "gross_litres": 1}])])
        row = run(d)["tank_daily"][0]
        self.assertEqual(row["mixed_litres"], "11")
        self.assertEqual(row["concentration_mg_per_litre"], "80/11")
        self.assertEqual(row["spill_litres"], "2")
        self.assertEqual(row["spill_salt_mg"], "160/11")
        self.assertEqual(row["closing_litres"], "8")
        # 期末盐 = 80 - 抽水携盐 80/11 - 溢盐 160/11 = 640/11。
        self.assertEqual(row["closing_salt_mg"], "640/11")

    def test_pumping_first_means_enough_pump_avoids_spill(self):
        # 同样 8L 起：先抽 3L 则余 5L + 来水 3L = 8，恰好不溢流。
        dates = ["2026-11-01"]
        d = make_data(
            [field()], weather(dates),
            [tank("a", capacity=8, opening=8, opening_salt=80)],
            [reuse()],
            [op(dates[0],
                inflows=[{"tank_id": "a", "water_litres": 3, "salt_mg": 0}],
                withdrawals=[{"field_id": "f", "tank_id": "a",
                              "gross_litres": 3}])])
        row = run(d)["tank_daily"][0]
        self.assertEqual(row["spill_litres"], "0")
        self.assertEqual(row["closing_litres"], "8")


# ---------------------------------------------------------------------------
# 田间盐迁移：损失、雨水稀释、土壤混合、干涸保留
# ---------------------------------------------------------------------------

class FieldSaltTransportTests(unittest.TestCase):
    def test_delivery_loss_carries_proportional_salt(self):
        # 效率 800‰：毛 10L（浓度1，盐10mg），损失 2L 携盐 2mg，到田 8L/8mg。
        dates = ["2026-11-01"]
        d = make_data(
            [field(efficiency=800)], weather(dates),
            [tank("a", opening=100, opening_salt=100)],
            [reuse()],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 10}])])
        frow = run(d)["field_daily"][0]
        self.assertEqual(frow["water_balance"]["delivery_loss_mm"], "2")
        self.assertEqual(frow["irrigation_salt_mg"], "10")
        self.assertEqual(frow["delivery_loss_salt_mg"], "2")
        self.assertEqual(frow["closing_salt_mg"], "8")
        sb = run(d)["system_balance"]
        self.assertEqual(sb["water_litres"]["delivery_loss"], "2")
        self.assertEqual(sb["salt_mg"]["delivery_loss"], "2")

    def test_rain_dilutes_before_runoff_takes_salt(self):
        # 6L 含 6mg 盐的到田水 + 4L 无盐雨 => 10L 浓度 3/5；
        # 入渗上限 5mm -> 径流 5mm 携盐 6*5/10 = 3mg。
        dates = ["2026-11-01"]
        d = make_data(
            [field(infiltration=5)], weather(dates, rain=4),
            [tank("a", opening=100, opening_salt=100)],
            [reuse(permille=500)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 6}])])
        frow = run(d)["field_daily"][0]
        self.assertEqual(frow["water_balance"]["rain_mm"], "4")
        self.assertEqual(frow["water_balance"]["runoff_mm"], "5")
        self.assertEqual(frow["runoff_salt_mg"], "3")
        # 入渗 5mm 携盐 3mg，全部留土（无容量渗漏）。
        self.assertEqual(frow["closing_salt_mg"], "3")
        # 回收 500‰：回流 2.5L/1.5mg，未回收排水等量离开系统。
        self.assertEqual(frow["recovered_litres"], "5/2")
        self.assertEqual(frow["recovered_salt_mg"], "3/2")
        self.assertEqual(frow["discharged_litres"], "5/2")
        self.assertEqual(frow["discharged_salt_mg"], "3/2")

    def test_soil_mixes_before_capacity_drainage(self):
        # 期初 2mm/20mg + 6L 淡水 => 8mm/20mg（浓度 5/2）；
        # 容量 2mm -> 渗漏 6mm 携盐 20*6/8 = 15mg，留 2mm/5mg。
        dates = ["2026-11-01"]
        d = make_data(
            [field(opening=2, capacity=2, infiltration=100)], weather(dates),
            [tank("a", opening=100, opening_salt=0)],
            [reuse(opening_salt=20, permille=1000)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 6}])])
        frow = run(d)["field_daily"][0]
        self.assertEqual(frow["water_balance"]["drainage_mm"], "6")
        self.assertEqual(frow["drainage_salt_mg"], "15")
        self.assertEqual(frow["closing_salt_mg"], "5")

    def test_dry_soil_keeps_salt_and_redissolves_next_day(self):
        dates = ["2026-11-01", "2026-11-02"]
        d = make_data(
            [field(opening=1, capacity=100, infiltration=100)],
            weather(dates, et=[1, 0]),
            [tank("a", opening=100, opening_salt=0)],
            [reuse(opening_salt=7)],
            [op(dates[0]),
             op(dates[1], withdrawals=[
                 {"field_id": "f", "tank_id": "a", "gross_litres": 7}])])
        report = run(d)
        f1, f2 = report["field_daily"]
        self.assertEqual(f1["water_balance"]["closing_mm"], "0")
        self.assertEqual(f1["closing_salt_mg"], "7")  # 干涸不丢盐
        # 次日 7L 淡水与 7mg 干盐全混匀，无渗漏，留 7mm/7mg。
        self.assertEqual(f2["water_balance"]["closing_mm"], "7")
        self.assertEqual(f2["closing_salt_mg"], "7")

    def test_et_removes_only_water(self):
        # 期初 5mm/10mg（浓度2），ET 3mm 后 2mm 水、盐仍 10mg。
        dates = ["2026-11-01"]
        d = make_data(
            [field(opening=5, capacity=100)], weather(dates, et=3),
            [tank("a")], [reuse(opening_salt=10)], [op(dates[0])])
        frow = run(d)["field_daily"][0]
        self.assertEqual(frow["water_balance"]["actual_et_mm"], "3")
        self.assertEqual(frow["water_balance"]["closing_mm"], "2")
        self.assertEqual(frow["closing_salt_mg"], "10")


# ---------------------------------------------------------------------------
# 回流、未回收排水与末日 pending
# ---------------------------------------------------------------------------

class ReturnFlowTests(unittest.TestCase):
    def test_last_day_pending_returns_are_not_used_early(self):
        dates = ["2026-11-01", "2026-11-02"]
        d = make_data(
            [field(capacity=2, infiltration=5)], weather(dates),
            [tank("a", opening=100, opening_salt=100)],
            [reuse(permille=500)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 6}]),
             op(dates[1], withdrawals=[
                 {"field_id": "f", "tank_id": "a", "gross_litres": 6}])])
        report = run(d)
        f1, f2 = report["field_daily"]
        t1, t2 = report["tank_daily"]
        # 首日径流 1 + 渗漏 3 = 4L 排水、4mg 盐；500‰ 回流 2L/2mg。
        self.assertEqual(f1["recovered_litres"], "2")
        self.assertEqual(f1["recovered_salt_mg"], "2")
        self.assertEqual(f1["discharged_litres"], "2")
        # 回流次日一早才到账，首日池账不含它。
        self.assertEqual(t1["return_litres"], "0")
        self.assertEqual(t2["return_litres"], "2")
        self.assertEqual(t2["return_salt_mg"], "2")
        # 末日排水 6L 的 500‰ = 3L 留在窗口外 pending，日期为末日次日。
        self.assertEqual(f2["recovered_litres"], "3")
        self.assertEqual(report["pending_returns"], [
            {"date": "2026-11-03", "tank_id": "a",
             "water_litres": "3", "salt_mg": f2["recovered_salt_mg"]}])
        # 末日池账不提前计入 pending。
        self.assertEqual(t2["closing_litres"],
                         str(F(t2["mixed_litres"]) - F(6)))
        wb = report["system_balance"]["water_litres"]
        self.assertEqual(wb["pending_returns"], "3")
        self.assertEqual(wb["discharged"], "5")  # 首日 2 + 末日 3

    def test_zero_permille_means_no_recovery(self):
        dates = ["2026-11-01"]
        d = make_data(
            [field(capacity=2, infiltration=5)], weather(dates),
            [tank("a", opening=100, opening_salt=100)],
            [reuse(permille=0, return_tank="a")],  # 回流池仍必须存在
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 6}])])
        report = run(d)
        frow = report["field_daily"][0]
        self.assertEqual(frow["recovered_litres"], "0")
        self.assertEqual(frow["recovered_salt_mg"], "0")
        self.assertGreater(F(frow["discharged_litres"]), 0)
        self.assertEqual(report["pending_returns"], [])

    def test_pending_aggregates_by_tank_and_skips_zero(self):
        # 两个田块末日都回流到同一池，应汇总成一行；permille=0 的田块不出行。
        dates = ["2026-11-01"]
        d = make_data(
            [field("f", capacity=2, infiltration=5),
             field("g", capacity=2, infiltration=5),
             field("h", capacity=2, infiltration=5)],
            weather(dates),
            [tank("a", opening=1000, opening_salt=0)],
            [reuse("f", permille=500), reuse("g", permille=1000),
             reuse("h", permille=0)],
            [op(dates[0], withdrawals=[
                {"field_id": "f", "tank_id": "a", "gross_litres": 6},
                {"field_id": "g", "tank_id": "a", "gross_litres": 6},
                {"field_id": "h", "tank_id": "a", "gross_litres": 6}])])
        report = run(d)
        self.assertEqual(len(report["pending_returns"]), 1)
        row = report["pending_returns"][0]
        self.assertEqual(row["date"], "2026-11-02")
        self.assertEqual(row["tank_id"], "a")
        f_row = next(r for r in report["field_daily"] if r["field_id"] == "f")
        g_row = next(r for r in report["field_daily"] if r["field_id"] == "g")
        self.assertEqual(
            F(row["water_litres"]),
            F(f_row["recovered_litres"]) + F(g_row["recovered_litres"]))


# ---------------------------------------------------------------------------
# 报告结构、排序、格式与系统守恒
# ---------------------------------------------------------------------------

class ReportShapeTests(unittest.TestCase):
    def test_field_daily_covers_all_field_days_even_without_withdrawal(self):
        dates = ["2026-11-01", "2026-11-02"]
        d = make_data(
            [field("f"), field("g", area=2)], weather(dates),
            [tank("a", opening=10)],
            [reuse("f"), reuse("g")],
            [op(dates[0]), op(dates[1])])
        report = run(d)
        self.assertEqual(len(report["field_daily"]), 4)
        self.assertEqual(len(report["tank_daily"]), 2)
        self.assertEqual(report["deliveries"], [])
        self.assertEqual(
            [(r["field_id"], r["date"]) for r in report["field_daily"]],
            [("f", dates[0]), ("f", dates[1]),
             ("g", dates[0]), ("g", dates[1])])
        # water_balance 保留现有 daily 的全部字段与身份。
        wb = report["field_daily"][0]["water_balance"]
        self.assertEqual(set(wb), {
            "field_id", "date", "opening_mm", "rain_mm",
            "gross_irrigation_mm", "delivery_loss_mm", "runoff_mm",
            "drainage_mm", "actual_et_mm", "unmet_et_mm", "closing_mm",
            "balance_residual_mm", "advised_next_gross_mm"})
        self.assertIsInstance(wb["advised_next_gross_mm"], int)

    def test_all_quantities_are_canonical_fraction_strings(self):
        report = run(json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "reuse-input.json").read_text()))
        for row in report["tank_daily"]:
            for key, value in row.items():
                if key in ("date", "tank_id", "concentration_mg_per_litre"):
                    continue
                self.assertIsInstance(value, str, (key, value))
                self.assertNotIn(".", value)
        for row in report["deliveries"]:
            self.assertIsInstance(row["accepted"], bool)
            for key, value in row.items():
                if key in ("date", "field_id", "tank_id", "accepted",
                           "source_mg_per_litre"):
                    continue
                self.assertIsInstance(value, str)
        # 分数必须是规范形式（Fraction 解析后再转字符串不变）。
        for section in ("tank_daily", "pending_returns"):
            for row in report[section]:
                for key, value in row.items():
                    if isinstance(value, str) and key not in ("date", "tank_id"):
                        self.assertEqual(str(F(value)), value)

    def test_system_balance_water_and_salt_close(self):
        report = run(json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "reuse-input.json").read_text()))
        w = report["system_balance"]["water_litres"]
        s = report["system_balance"]["salt_mg"]
        self.assertEqual(
            set(w), {"opening", "external", "rain", "closing",
                     "pending_returns", "spill", "rejected", "delivery_loss",
                     "discharged", "evaporation", "actual_et", "residual"})
        self.assertEqual(
            set(s), {"opening", "external", "closing", "pending_returns",
                     "spill", "rejected", "delivery_loss", "discharged",
                     "residual"})
        # 独立逐项相加复核水恒等式。
        self.assertEqual(
            F(w["opening"]) + F(w["external"]) + F(w["rain"]),
            F(w["closing"]) + F(w["pending_returns"]) + F(w["spill"])
            + F(w["rejected"]) + F(w["delivery_loss"]) + F(w["discharged"])
            + F(w["evaporation"]) + F(w["actual_et"]))
        self.assertEqual(
            F(s["opening"]) + F(s["external"]),
            F(s["closing"]) + F(s["pending_returns"]) + F(s["spill"])
            + F(s["rejected"]) + F(s["delivery_loss"]) + F(s["discharged"]))


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.good = json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "reuse-input.json").read_text())

    def assert_input_error(self, mutate):
        data = copy.deepcopy(self.good)
        mutate(data)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            run(data)

    def test_root_and_row_extra_keys_rejected(self):
        self.assert_input_error(lambda d: d.update(applications=[]))
        self.assert_input_error(lambda d: d.update(planning={}))
        self.assert_input_error(lambda d: d.pop("tanks"))
        self.assert_input_error(lambda d: d["tanks"][0].update(extra=1))
        self.assert_input_error(lambda d: d["operations"][0].update(extra=1))

    def test_bool_is_not_integer(self):
        self.assert_input_error(
            lambda d: d["tanks"][0].__setitem__("capacity_litres", True))
        self.assert_input_error(
            lambda d: d["field_reuse"][0].__setitem__("return_permille", True))
        self.assert_input_error(
            lambda d: d["operations"][0]["withdrawals"][0]
            .__setitem__("gross_litres", False))

    def test_ranges_and_references(self):
        self.assert_input_error(
            lambda d: d["tanks"][0].__setitem__("capacity_litres", 0))
        self.assert_input_error(
            lambda d: d["tanks"][0].__setitem__("opening_litres", 21))
        self.assert_input_error(
            lambda d: d["tanks"][0].__setitem__("opening_salt_mg", -1))
        self.assert_input_error(
            lambda d: d["field_reuse"][0].__setitem__("return_permille", 1001))
        self.assert_input_error(
            lambda d: d["field_reuse"][0].__setitem__("return_tank_id", "x"))
        self.assert_input_error(
            lambda d: d["operations"][0]["withdrawals"][0]
            .__setitem__("field_id", "x"))

    def test_operation_identity_rules(self):
        self.assert_input_error(lambda d: d["operations"].pop())
        self.assert_input_error(
            lambda d: d["operations"][1].__setitem__(
                "date", d["operations"][0]["date"]))
        self.assert_input_error(
            lambda d: d["operations"][0].__setitem__("date", "2026-09-30"))
        self.assert_input_error(lambda d: d["field_reuse"].pop())
        self.assert_input_error(lambda d: d.__setitem__("tanks", []))
        self.assert_input_error(
            lambda d: d.__setitem__("tanks", d["tanks"] * 13))
        self.assert_input_error(
            lambda d: d["tanks"][0].__setitem__("id", ""))

    def test_duplicate_and_self_loop_rules(self):
        def add_tank(d):
            d["tanks"].append(
                {"id": "s", "capacity_litres": 10,
                 "opening_litres": 10, "opening_salt_mg": 0})

        def dup_pair(d):
            add_tank(d)
            d["operations"][0]["transfers"] = [
                {"from_tank_id": "r", "to_tank_id": "s", "water_litres": 1},
                {"from_tank_id": "r", "to_tank_id": "s", "water_litres": 1}]

        def self_loop(d):
            d["operations"][0]["transfers"] = [
                {"from_tank_id": "r", "to_tank_id": "r", "water_litres": 1}]

        def dup_inflow(d):
            d["operations"][0]["inflows"] = [
                {"tank_id": "r", "water_litres": 1, "salt_mg": 0},
                {"tank_id": "r", "water_litres": 2, "salt_mg": 0}]

        def zero_water_salt(d):
            d["operations"][0]["inflows"] = [
                {"tank_id": "r", "water_litres": 0, "salt_mg": 1}]

        self.assert_input_error(dup_pair)
        self.assert_input_error(self_loop)
        self.assert_input_error(dup_inflow)
        self.assert_input_error(zero_water_salt)

    def test_weather_gap_still_uses_legacy_validation(self):
        self.assert_input_error(
            lambda d: d["weather"][1].__setitem__("date", "2026-10-03"))

    def test_identity_strings_are_not_stripped(self):
        # 带空格的身份与不带空格是两个不同身份；引用必须原样匹配。
        data = copy.deepcopy(self.good)
        data["tanks"][0]["id"] = " r "
        data["field_reuse"][0]["return_tank_id"] = " r "
        data["operations"][0]["withdrawals"][0]["tank_id"] = " r "
        data["operations"][1]["withdrawals"][0]["tank_id"] = " r "
        report = run(data)
        self.assertEqual(report["tank_daily"][0]["tank_id"], " r ")

    def test_input_object_is_not_modified(self):
        data = self.good
        snapshot = copy.deepcopy(data)
        run(data)
        self.assertEqual(data, snapshot)


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
            [sys.executable, "-m", "irrigation.reuse_cli",
             "--source", source, "--output", output],
            capture_output=True, text=True, env=ENV)

    def test_success_writes_report_exits_zero_and_matches_api(self):
        data = json.loads(Path(__file__).resolve().parents[1]
                          .joinpath("fixtures", "reuse-input.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            source = self._write(folder, "in.json", data)
            output = str(Path(folder) / "report.json")
            r = self._run(source, output)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(Path(output).read_text()), run(data))

    def test_invalid_input_exits_2_and_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = self._write(folder, "bad.json", {"fields": []})
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = self._run(bad, str(output))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(output.read_text(), "KEEP")

    def test_existing_target_exits_2_and_is_preserved(self):
        data = json.loads(Path(__file__).resolve().parents[1]
                          .joinpath("fixtures", "reuse-input.json").read_text())
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
