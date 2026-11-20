"""irrigation.land_water 的自动化测试。

期望值全部在本文件内独立手算（逐 mm×m² 推升、按混合比例拆分来源）给出，不把
生产 ``run`` 的返回值再调用一次当期望；系统残差为 0 只作为附加核对，不能替代
每条来源/终点分解的独立数值预期。覆盖契约要求的全部区分点：

- 三日强制算例的交接水量、c 渗漏双来源各 1L、e 末存双来源各 5L；
- 独占接续变体（末存 a=4/b=6）与去灌溉、ET=3 的缺水/渗漏变体；
- 无拆并时与原 ``irrigation.service.run`` 水账逐字段语义一致；
- 降雨/到田水混合先于径流、期初水混合先于渗漏的先后顺序；
- 面积缺口/超额、旧日供水绑错新旧 ID、ID 复用、规模上限等校验；
- 精确分数、零水量边与零水量查询、数组置换不变、输入不被修改；
- 严格 JSON、CLI 失败不覆盖输出、退出码与旧接口不变。
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

from irrigation.land_water import run
from irrigation.service import run as legacy_run

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC}
F = Fraction
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "land-input.json"
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def field(fid, area=2, capacity=10, opening=0, threshold=0, target=0,
          infiltration=100, efficiency=1000):
    return {
        "id": fid, "area_m2": area, "capacity_mm": capacity,
        "opening_mm": opening, "threshold_mm": threshold, "target_mm": target,
        "infiltration_mm": infiltration, "efficiency_permille": efficiency,
    }


def new_field(fid, **kwargs):
    row = field(fid, **kwargs)
    row.pop("opening_mm")
    return row


def weather(dates, rain=0, et=0):
    if isinstance(rain, int):
        rain = [rain] * len(dates)
    if isinstance(et, int):
        et = [et] * len(dates)
    return [{"date": d, "rain_mm": r, "et_mm": e}
            for d, r, e in zip(dates, rain, et)]


def make_data(fields, weather_rows, applications=(), repartitions=(),
              audit_sources=(), audit_sinks=()):
    return {
        "fields": fields, "weather": weather_rows,
        "applications": list(applications), "repartitions": list(repartitions),
        "audit_sources": list(audit_sources), "audit_sinks": list(audit_sinks),
    }


def ident(date, fid, kind):
    return {"date": date, "field_id": fid, "kind": kind}


def audit_map(report, key):
    return {tuple(row["identity"][k] for k in ("date", "field_id", "kind")):
            row for row in report[key]}


def fixture_data():
    return json.loads(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# 契约强制的三日算例（fixtures/land-input.json）
# ---------------------------------------------------------------------------

class MandatoryThreeDayTests(unittest.TestCase):
    def setUp(self):
        self.report = run(fixture_data())

    def test_daily_covers_only_active_fields_per_day(self):
        active = {}
        for row in self.report["daily"]:
            active.setdefault(row["date"], []).append(row["field_id"])
        self.assertEqual(active, {
            "2026-09-01": ["a", "b"],
            "2026-09-02": ["c", "d"],
            "2026-09-03": ["e"],
        })
        # 排序按 (date, field_id)。
        keys = [(r["date"], r["field_id"]) for r in self.report["daily"]]
        self.assertEqual(keys, sorted(keys))

    def test_day1_closing_is_eight_litres_each(self):
        day1 = {r["field_id"]: r for r in self.report["daily"]
                if r["date"] == DAYS[0]}
        # a：4mm 期初无进出，结存 4mm × 2m² = 8L。
        self.assertEqual(day1["a"]["opening_mm"], "4")
        self.assertEqual(day1["a"]["closing_mm"], "4")
        # b：毛灌 4mm、效率 1000‰，结存 4mm × 2m² = 8L。
        self.assertEqual(day1["b"]["gross_irrigation_mm"], "4")
        self.assertEqual(day1["b"]["delivery_loss_mm"], "0")
        self.assertEqual(day1["b"]["closing_mm"], "4")
        self.assertIsInstance(day1["a"]["advised_next_gross_mm"], int)

    def test_day2_capacity_drainage_and_et(self):
        day2 = {r["field_id"]: r for r in self.report["daily"]
                if r["date"] == DAYS[1]}
        # c、d 各接 8L / 2m² = 期初 4mm。
        self.assertEqual(day2["c"]["opening_mm"], "4")
        self.assertEqual(day2["d"]["opening_mm"], "4")
        # c 容量 3mm：渗漏 1mm = 2L，ET 1mm = 2L，结存 2mm = 4L。
        self.assertEqual(day2["c"]["drainage_mm"], "1")
        self.assertEqual(day2["c"]["actual_et_mm"], "1")
        self.assertEqual(day2["c"]["unmet_et_mm"], "0")
        self.assertEqual(day2["c"]["closing_mm"], "2")
        # d 容量充足：无渗漏，ET 1mm = 2L，结存 3mm = 6L。
        self.assertEqual(day2["d"]["drainage_mm"], "0")
        self.assertEqual(day2["d"]["actual_et_mm"], "1")
        self.assertEqual(day2["d"]["closing_mm"], "3")

    def test_land_transfers_list_every_edge_including_zero_later(self):
        edges = [(t["date"], t["from_field_id"], t["to_field_id"],
                  t["area_m2"], t["water_litres"])
                 for t in self.report["land_transfers"]]
        # 第 2 日四条边各 1m²，旧田期末 4mm => 每边 4L。
        self.assertEqual(edges[:4], [
            (DAYS[1], "a", "c", 1, "4"),
            (DAYS[1], "a", "d", 1, "4"),
            (DAYS[1], "b", "c", 1, "4"),
            (DAYS[1], "b", "d", 1, "4"),
        ])
        # 第 3 日：c 结存 4L、d 结存 6L，整田 2m² 交给 e。
        self.assertEqual(edges[4:], [
            (DAYS[2], "c", "e", 2, "4"),
            (DAYS[2], "d", "e", 2, "6"),
        ])
        keys = [(t["date"], t["from_field_id"], t["to_field_id"])
                for t in self.report["land_transfers"]]
        self.assertEqual(keys, sorted(keys))

    def test_day3_e_opening_and_final_storage(self):
        e = next(r for r in self.report["daily"] if r["field_id"] == "e")
        # 接到 10L / 4m² = 5/2 mm；无雨无灌 ET=0，结存不变。
        self.assertEqual(e["opening_mm"], "5/2")
        self.assertEqual(e["closing_mm"], "5/2")

    def test_source_audit_a_initial_full_path(self):
        rows = audit_map(self.report, "audit_sources")
        a = rows[(DAYS[0], "a", "initial")]
        self.assertEqual(a["water_litres"], "8")
        # 独立手算：c 渗漏 1、c ET 1、d ET 1、e 末存 5，合计恰为 8L。
        self.assertEqual(
            {(s["identity"]["date"], s["identity"]["field_id"],
              s["identity"]["kind"]): s["water_litres"] for s in a["sinks"]},
            {(DAYS[1], "c", "drainage"): "1",
             (DAYS[1], "c", "et"): "1",
             (DAYS[1], "d", "et"): "1",
             (DAYS[2], "e", "storage"): "5"})
        self.assertEqual(
            sum(F(s["water_litres"]) for s in a["sinks"]), F(8))

    def test_sink_audit_c_drainage_two_sources_one_litre_each(self):
        rows = audit_map(self.report, "audit_sinks")
        c = rows[(DAYS[1], "c", "drainage")]
        self.assertEqual(c["water_litres"], "2")
        self.assertEqual(
            {s["identity"]["field_id"]: s["water_litres"]
             for s in c["sources"]},
            {"a": "1", "b": "1"})

    def test_sink_audit_e_storage_split_five_five(self):
        rows = audit_map(self.report, "audit_sinks")
        e = rows[(DAYS[2], "e", "storage")]
        self.assertEqual(e["water_litres"], "10")
        self.assertEqual(
            {s["identity"]["field_id"]: s["water_litres"]
             for s in e["sources"]},
            {"a": "5", "b": "5"})

    def test_system_balance_closes_exactly(self):
        b = self.report["system_balance"]
        self.assertEqual(b, {
            "initial_litres": "8", "rain_litres": "0",
            "irrigation_litres": "8", "delivery_loss_litres": "0",
            "runoff_litres": "0", "drainage_litres": "2",
            "et_litres": "4", "storage_litres": "10",
            "residual_litres": "0"})
        # 独立相加复核，不依赖 residual 字段。
        self.assertEqual(
            F(b["initial_litres"]) + F(b["rain_litres"])
            + F(b["irrigation_litres"]),
            F(b["delivery_loss_litres"]) + F(b["runoff_litres"])
            + F(b["drainage_litres"]) + F(b["et_litres"])
            + F(b["storage_litres"]))

    def test_all_litres_are_canonical_fraction_strings(self):
        for section in ("land_transfers",):
            for row in self.report[section]:
                self.assertEqual(
                    str(F(row["water_litres"])), row["water_litres"])
        for section, detail in (("audit_sources", "sinks"),
                                ("audit_sinks", "sources")):
            for row in self.report[section]:
                self.assertEqual(
                    str(F(row["water_litres"])), row["water_litres"])
                for sub in row[detail]:
                    self.assertEqual(
                        str(F(sub["water_litres"])), sub["water_litres"])
        for value in self.report["system_balance"].values():
            self.assertNotIn(".", value)


# ---------------------------------------------------------------------------
# 契约规定的两个变体
# ---------------------------------------------------------------------------

class TakeoverVariantTests(unittest.TestCase):
    def test_exclusive_takeover_changes_final_provenance(self):
        data = fixture_data()
        data["repartitions"][0]["moves"] = [
            {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
            {"from_field_id": "b", "to_field_id": "d", "area_m2": 2},
        ]
        report = run(data)
        e = audit_map(report, "audit_sinks")[
            (DAYS[2], "e", "storage")]
        # c 独占 a 的 8L：渗漏 2、ET 2 后余 4；d 独占 b 的 8L：ET 2 后余 6。
        self.assertEqual(
            {s["identity"]["field_id"]: s["water_litres"]
             for s in e["sources"]}, {"a": "4", "b": "6"})

    def test_half_takeover_without_irrigation_et3_has_deficit(self):
        data = fixture_data()
        data["applications"] = []
        data["weather"][1]["et_mm"] = 3
        report = run(data)
        day2 = {r["field_id"]: r for r in report["daily"]
                if r["date"] == DAYS[1]}
        # c 只接 a 的一半：4L / 2m² = 2mm；无渗漏；ET 需 3mm，缺水 1mm。
        self.assertEqual(day2["c"]["opening_mm"], "2")
        self.assertEqual(day2["c"]["drainage_mm"], "0")
        self.assertEqual(day2["c"]["actual_et_mm"], "2")
        self.assertEqual(day2["c"]["unmet_et_mm"], "1")

    def test_exclusive_takeover_without_irrigation_et3_drains(self):
        data = fixture_data()
        data["applications"] = []
        data["weather"][1]["et_mm"] = 3
        data["repartitions"][0]["moves"] = [
            {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
            {"from_field_id": "b", "to_field_id": "d", "area_m2": 2},
        ]
        report = run(data)
        day2 = {r["field_id"]: r for r in report["daily"]
                if r["date"] == DAYS[1]}
        # c 独占 a：期初 4mm，超容量 1mm 渗漏，剩 3mm 恰好满足 ET，缺水 0。
        self.assertEqual(day2["c"]["opening_mm"], "4")
        self.assertEqual(day2["c"]["drainage_mm"], "1")
        self.assertEqual(day2["c"]["actual_et_mm"], "3")
        self.assertEqual(day2["c"]["unmet_et_mm"], "0")


# ---------------------------------------------------------------------------
# 无拆并时与原水账一致
# ---------------------------------------------------------------------------

class LegacyParityTests(unittest.TestCase):
    def test_daily_matches_legacy_service_semantically(self):
        legacy = json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "input.json").read_text())
        data = copy.deepcopy(legacy)
        data.update(repartitions=[], audit_sources=[], audit_sinks=[])
        report = run(data)
        expected = legacy_run(legacy)
        self.assertEqual(len(report["daily"]), len(expected["daily"]))
        mm_keys = [k for k in report["daily"][0] if k.endswith("_mm")]
        for got, want in zip(report["daily"], expected["daily"]):
            self.assertEqual(got["field_id"], want["field_id"])
            self.assertEqual(got["date"], want["date"])
            for key in mm_keys:
                self.assertEqual(F(got[key]), F(want[key]))
            self.assertEqual(got["advised_next_gross_mm"],
                             want["advised_next_gross_mm"])
        # 无拆并即无交接边；系统余额仍为 0。
        self.assertEqual(report["land_transfers"], [])
        self.assertEqual(report["system_balance"]["residual_litres"], "0")

    def test_legacy_extra_application_key_keeps_legacy_tolerance(self):
        # 共有字段按已有核算口径处理：applications 行的额外键不新增拒绝规则。
        legacy = json.loads(
            Path(__file__).resolve().parents[1]
            .joinpath("fixtures", "input.json").read_text())
        data = copy.deepcopy(legacy)
        data.update(repartitions=[], audit_sources=[], audit_sinks=[])
        data["applications"][0]["note"] = "legacy-tolerated"
        run(data)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 混合先后顺序与来源归因
# ---------------------------------------------------------------------------

class MixingOrderTests(unittest.TestCase):
    def _data(self, **field_overrides):
        params = dict(area=1, capacity=10, opening=5, infiltration=3)
        params.update(field_overrides)
        f = field("f", **params)
        return make_data(
            [f], weather(["2026-07-01"], rain=4),
            applications=[{"field_id": "f", "date": "2026-07-01",
                           "gross_mm": 4}],
            audit_sources=[
                ident("2026-07-01", "f", "initial"),
                ident("2026-07-01", "f", "rain"),
                ident("2026-07-01", "f", "irrigation")],
            audit_sinks=[
                ident("2026-07-01", "f", "runoff"),
                ident("2026-07-01", "f", "storage")])

    def test_runoff_takes_only_rain_and_delivered_not_opening(self):
        # incoming = 4 雨 + 4 到田 = 8mm；入渗上限 3 => 径流 5mm。
        # 径流按 1:1 带走：雨 5/2、灌溉 5/2；期初 5L 完全不进径流。
        report = run(self._data())
        sinks = audit_map(report, "audit_sinks")
        runoff = sinks[("2026-07-01", "f", "runoff")]
        self.assertEqual(runoff["water_litres"], "5")
        self.assertEqual(
            {s["identity"]["kind"]: s["water_litres"]
             for s in runoff["sources"]},
            {"rain": "5/2", "irrigation": "5/2"})
        # 入渗 3L（雨 3/2、灌 3/2）+ 期初 5L 混匀结存 8L。
        storage = sinks[("2026-07-01", "f", "storage")]
        self.assertEqual(
            {s["identity"]["kind"]: s["water_litres"]
             for s in storage["sources"]},
            {"initial": "5", "rain": "3/2", "irrigation": "3/2"})

    def test_drainage_and_et_follow_soil_mix_proportions(self):
        # 期初 5mm + 入渗 3mm = 8mm；容量 6 => 渗漏 2mm，可用 6mm；ET 3mm。
        data = self._data(capacity=6)
        data["weather"] = [{"date": "2026-07-01", "rain_mm": 4, "et_mm": 3}]
        data["audit_sinks"] += [
            ident("2026-07-01", "f", "drainage"),
            ident("2026-07-01", "f", "et")]
        report = run(data)
        sinks = audit_map(report, "audit_sinks")
        # 混合占比 initial:rain:irr = 5 : 3/2 : 3/2 = 10:3:3（共 16 份）。
        drainage = sinks[("2026-07-01", "f", "drainage")]
        self.assertEqual(drainage["water_litres"], "2")
        self.assertEqual(
            {s["identity"]["kind"]: s["water_litres"]
             for s in drainage["sources"]},
            {"initial": "5/4", "rain": "3/8", "irrigation": "3/8"})
        et = sinks[("2026-07-01", "f", "et")]
        self.assertEqual(et["water_litres"], "3")
        self.assertEqual(
            {s["identity"]["kind"]: s["water_litres"]
             for s in et["sources"]},
            {"initial": "15/8", "rain": "9/16", "irrigation": "9/16"})
        # 未满足 ET 为 0 时不产生额外水量；每个来源出流总和等于进水量。
        for row in report["audit_sources"]:
            self.assertEqual(
                sum(F(s["water_litres"]) for s in row["sinks"]),
                F(row["water_litres"]))

    def test_delivery_loss_comes_only_from_irrigation(self):
        # 效率 800‰、毛 10mm（1m²）：损耗 2mm 整笔来自灌溉。
        data = make_data(
            [field("f", area=1, capacity=100, efficiency=800)],
            weather(["2026-07-01"]),
            applications=[{"field_id": "f", "date": "2026-07-01",
                           "gross_mm": 10}],
            audit_sinks=[ident("2026-07-01", "f", "delivery_loss")])
        report = run(data)
        loss = report["audit_sinks"][0]
        self.assertEqual(loss["water_litres"], "2")
        self.assertEqual(
            [s["identity"]["kind"] for s in loss["sources"]], ["irrigation"])
        self.assertEqual(loss["sources"][0]["water_litres"], "2")

    def test_unmet_et_is_not_water_and_dry_mix_makes_no_contribution(self):
        # 干土、无雨无灌、ET=2：所有终点水量为 0，明细为空，缺水 2mm。
        data = make_data(
            [field("f", opening=0, capacity=10)],
            weather(["2026-07-01"], et=2),
            audit_sinks=[ident("2026-07-01", "f", "et"),
                         ident("2026-07-01", "f", "storage")])
        report = run(data)
        row = report["daily"][0]
        self.assertEqual(row["actual_et_mm"], "0")
        self.assertEqual(row["unmet_et_mm"], "2")
        for sink in report["audit_sinks"]:
            self.assertEqual(sink["water_litres"], "0")
            self.assertEqual(sink["sources"], [])


class TransferMechanicsTests(unittest.TestCase):
    def test_area_proportional_split_and_zero_water_edge(self):
        # a: 3m²、期初 2mm（6L）；拆给 c 2m²（4L）、d 1m²（2L），另设零水
        # 旧田块 z 同日退出，其交接边水量为 0 但必须列出。
        data = make_data(
            [field("a", area=3, opening=2), field("z", area=1, opening=0)],
            weather(["2026-08-01", "2026-08-02"]),
            repartitions=[{
                "date": "2026-08-02",
                "new_fields": [
                    new_field("c", area=2), new_field("d", area=1),
                    new_field("g", area=1)],
                "moves": [
                    {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
                    {"from_field_id": "a", "to_field_id": "d", "area_m2": 1},
                    {"from_field_id": "z", "to_field_id": "g", "area_m2": 1}],
            }],
            audit_sources=[ident("2026-08-01", "a", "initial")],
            audit_sinks=[
                ident("2026-08-02", "c", "storage"),
                ident("2026-08-02", "d", "storage")])
        report = run(data)
        edges = {(t["from_field_id"], t["to_field_id"]): t["water_litres"]
                 for t in report["land_transfers"]}
        self.assertEqual(edges, {("a", "c"): "4", ("a", "d"): "2",
                                 ("z", "g"): "0"})
        day2 = {r["field_id"]: r for r in report["daily"]
                if r["date"] == "2026-08-02"}
        self.assertEqual(day2["c"]["opening_mm"], "2")
        self.assertEqual(day2["d"]["opening_mm"], "2")
        src = report["audit_sources"][0]
        self.assertEqual(
            {s["identity"]["field_id"]: s["water_litres"]
             for s in src["sinks"]}, {"c": "4", "d": "2"})

    def test_inherited_opening_above_capacity_is_not_pre_trimmed(self):
        # 新田块 c 容量 1mm，却承接 4mm 期初：不预裁，当日渗漏 3mm。
        data = make_data(
            [field("a", area=1, opening=4, capacity=10)],
            weather(["2026-08-01", "2026-08-02"]),
            repartitions=[{
                "date": "2026-08-02",
                "new_fields": [new_field("c", area=1, capacity=1)],
                "moves": [{"from_field_id": "a", "to_field_id": "c",
                           "area_m2": 1}]}],
            audit_sinks=[ident("2026-08-02", "c", "drainage")])
        report = run(data)
        c = next(r for r in report["daily"] if r["field_id"] == "c")
        self.assertEqual(c["opening_mm"], "4")
        self.assertEqual(c["drainage_mm"], "3")
        self.assertEqual(c["closing_mm"], "1")

    def test_same_source_rejoins_one_sink_from_two_predecessors(self):
        # 契约三日算例已覆盖多前身汇入；这里再核对单来源合计口径：
        # a 的初始水经 c、d 两边重新汇入 e 的同一 storage 终点。
        report = run(fixture_data())
        e = next(r for r in report["audit_sinks"]
                 if r["identity"]["kind"] == "storage")
        a_part = next(s for s in e["sources"]
                      if s["identity"]["field_id"] == "a")
        self.assertEqual(a_part["water_litres"], "5")


class ExactFractionTests(unittest.TestCase):
    def test_fractional_efficiency_litres_and_provenance(self):
        # 面积 3、效率 800‰、毛 1mm：损耗 1/5 mm = 3/5 L；
        # 到田 4/5 mm = 12/5 L 全部结存。
        data = make_data(
            [field("f", area=3, capacity=100, efficiency=800)],
            weather(["2026-07-01"]),
            applications=[{"field_id": "f", "date": "2026-07-01",
                           "gross_mm": 1}],
            audit_sources=[ident("2026-07-01", "f", "irrigation")],
            audit_sinks=[
                ident("2026-07-01", "f", "delivery_loss"),
                ident("2026-07-01", "f", "storage")])
        report = run(data)
        row = report["daily"][0]
        self.assertEqual(row["delivery_loss_mm"], "1/5")
        self.assertEqual(row["closing_mm"], "4/5")
        self.assertEqual(report["system_balance"]["delivery_loss_litres"],
                         "3/5")
        self.assertEqual(report["system_balance"]["storage_litres"], "12/5")
        source = report["audit_sources"][0]
        self.assertEqual(source["water_litres"], "3")
        self.assertEqual(
            {s["identity"]["kind"]: s["water_litres"]
             for s in source["sinks"]},
            {"delivery_loss": "3/5", "storage": "12/5"})

    def test_fractional_area_ratio_transfer(self):
        # 旧田 3m² 期末 2mm（6L），2m² 边交接 4L、1m² 边交接 2L；
        # 份额按 Fraction 面积比 2/3、1/3 拆分，无浮点误差。
        data = make_data(
            [field("a", area=3, opening=2)],
            weather(["2026-08-01", "2026-08-02"]),
            repartitions=[{
                "date": "2026-08-02",
                "new_fields": [new_field("c", area=2),
                               new_field("d", area=1)],
                "moves": [
                    {"from_field_id": "a", "to_field_id": "c", "area_m2": 2},
                    {"from_field_id": "a", "to_field_id": "d",
                     "area_m2": 1}]}],
            audit_sources=[ident("2026-08-01", "a", "initial")])
        report = run(data)
        sinks = {s["identity"]["field_id"]: s["water_litres"]
                 for s in report["audit_sources"][0]["sinks"]}
        self.assertEqual(sinks, {"c": "4", "d": "2"})


# ---------------------------------------------------------------------------
# 审计查询语义
# ---------------------------------------------------------------------------

class AuditQueryTests(unittest.TestCase):
    def test_zero_water_identity_returns_zero_and_empty_details(self):
        data = make_data(
            [field("a", opening=0), field("b", opening=0)],
            weather(["2026-08-01"]),
            audit_sources=[
                ident("2026-08-01", "a", "initial"),
                ident("2026-08-01", "a", "rain"),
                ident("2026-08-01", "a", "irrigation")],
            audit_sinks=[ident("2026-08-01", "a", "et")])
        report = run(data)
        for row in report["audit_sources"]:
            self.assertEqual(row["water_litres"], "0")
            self.assertEqual(row["sinks"], [])
        self.assertEqual(report["audit_sinks"][0]["sources"], [])

    def test_single_sided_query_still_traces_full_other_side(self):
        data = fixture_data()
        data["audit_sinks"] = []
        report = run(data)
        a = next(r for r in report["audit_sources"]
                 if r["identity"]["field_id"] == "a")
        self.assertEqual(len(a["sinks"]), 4)  # 不受另一组查询过滤
        data = fixture_data()
        data["audit_sources"] = []
        report = run(data)
        c = next(r for r in report["audit_sinks"]
                 if r["identity"]["kind"] == "drainage")
        self.assertEqual(len(c["sources"]), 2)

    def test_rows_and_nested_details_are_identity_sorted(self):
        data = make_data(
            [field("a"), field("b")],
            weather(["2026-08-01", "2026-08-02"]),
            audit_sources=[
                ident("2026-08-02", "b", "rain"),
                ident("2026-08-01", "a", "rain"),
                ident("2026-08-01", "a", "initial")])
        report = run(data)
        keys = [tuple(r["identity"][k]
                      for k in ("date", "field_id", "kind"))
                for r in report["audit_sources"]]
        self.assertEqual(keys, sorted(keys))

    def test_balance_is_independent_of_chosen_queries(self):
        base = fixture_data()
        full = run(base)
        stripped = run(make_data(
            base["fields"], base["weather"], base["applications"],
            base["repartitions"], [], []))
        self.assertEqual(full["system_balance"], stripped["system_balance"])
        self.assertEqual(full["daily"], stripped["daily"])
        self.assertEqual(full["land_transfers"], stripped["land_transfers"])


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.good = fixture_data()

    def assert_input_error(self, mutate):
        data = copy.deepcopy(self.good)
        mutate(data)
        with self.assertRaises((ValueError, KeyError, TypeError)):
            run(data)

    def test_root_and_new_object_extra_keys_rejected(self):
        self.assert_input_error(lambda d: d.update(planning={}))
        self.assert_input_error(lambda d: d.pop("repartitions"))
        self.assert_input_error(
            lambda d: d["repartitions"][0].update(extra=1))
        self.assert_input_error(
            lambda d: d["repartitions"][0]["moves"][0].update(extra=1))
        self.assert_input_error(
            lambda d: d["audit_sources"][0].update(extra=1))

    def test_new_field_rejects_opening_mm_and_bad_limits(self):
        self.assert_input_error(
            lambda d: d["repartitions"][0]["new_fields"][0]
            .__setitem__("opening_mm", 0))
        self.assert_input_error(
            lambda d: d["repartitions"][0]["new_fields"][0]
            .__setitem__("capacity_mm", 0))
        self.assert_input_error(
            lambda d: d["repartitions"][0]["new_fields"][0]
            .__setitem__("efficiency_permille", 1001))

    def test_bool_is_not_integer(self):
        self.assert_input_error(
            lambda d: d["repartitions"][0]["moves"][0]
            .__setitem__("area_m2", True))
        self.assert_input_error(
            lambda d: d["applications"][0].__setitem__("gross_mm", True))
        self.assert_input_error(
            lambda d: d["fields"][0].__setitem__("area_m2", False))

    def test_repartition_date_rules(self):
        self.assert_input_error(
            lambda d: d["repartitions"][0]
            .__setitem__("date", "2026-09-01"))
        self.assert_input_error(
            lambda d: d["repartitions"][0]
            .__setitem__("date", "2026-09-04"))
        self.assert_input_error(
            lambda d: d["repartitions"].append(dict(d["repartitions"][0])))

    def test_empty_new_fields_or_moves_rejected(self):
        self.assert_input_error(
            lambda d: d["repartitions"][0].__setitem__("moves", []))
        self.assert_input_error(
            lambda d: d["repartitions"][0].__setitem__("new_fields", []))

    def test_area_gap_and_excess_rejected(self):
        # 缺口：删掉一条边后 a 只转出 1m²，不足原面积 2m²。
        self.assert_input_error(
            lambda d: d["repartitions"][0]["moves"].pop())
        # 超额：同方向再加一条边（同时触发有向对重复）。
        self.assert_input_error(
            lambda d: d["repartitions"][0]["moves"].append(
                {"from_field_id": "a", "to_field_id": "c", "area_m2": 1}))
        # 新田块自身面积与转入面积不符。
        self.assert_input_error(
            lambda d: d["repartitions"][0]["new_fields"][0]
            .__setitem__("area_m2", 3))

    def test_move_target_and_source_rules(self):
        def point_to_old(d):
            d["repartitions"][0]["new_fields"] = [
                d["repartitions"][0]["new_fields"][0]]
            d["repartitions"][0]["moves"] = [
                {"from_field_id": "b", "to_field_id": "c", "area_m2": 1},
                {"from_field_id": "b", "to_field_id": "a", "area_m2": 1}]
        self.assert_input_error(point_to_old)
        # 本批新田块不能当批再转出。
        self.assert_input_error(
            lambda d: d["repartitions"][0]["moves"].append(
                {"from_field_id": "c", "to_field_id": "d", "area_m2": 1}))

    def test_id_reuse_including_retired_ids_rejected(self):
        self.assert_input_error(
            lambda d: d["repartitions"][0]["new_fields"][0]
            .__setitem__("id", "a"))

    def test_applications_must_bind_to_active_field_day(self):
        # 绑到已退出的旧 ID。
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "a", "date": "2026-09-02", "gross_mm": 1}))
        # 绑到尚未生效的新 ID（c 9-02 才在册）。
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "c", "date": "2026-09-01", "gross_mm": 1}))
        # 未知 ID。
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "x", "date": "2026-09-01", "gross_mm": 1}))
        # 同一田块日重复。
        self.assert_input_error(
            lambda d: d["applications"].append(
                {"field_id": "b", "date": "2026-09-01", "gross_mm": 1}))

    def test_audit_identity_lifecycle_and_kinds(self):
        self.assert_input_error(
            lambda d: d["audit_sources"].append(dict(d["audit_sources"][0])))
        self.assert_input_error(
            lambda d: d["audit_sources"].append(
                ident("2026-09-02", "a", "rain")))  # 失效田块日
        self.assert_input_error(
            lambda d: d["audit_sources"].append(
                ident("2026-09-02", "c", "initial")))  # initial 非首日
        self.assert_input_error(
            lambda d: d["audit_sinks"].append(
                ident("2026-09-02", "c", "storage")))  # storage 非末日
        self.assert_input_error(
            lambda d: d["audit_sinks"].append(
                ident("2026-09-02", "c", "bogus")))  # 非法 kind
        self.assert_input_error(
            lambda d: d["audit_sources"].append(
                ident("2026-09-03", "e", "storage")))  # 来源用了终点 kind

    def test_audit_cap_enforced_with_distinct_identities(self):
        data = make_data(
            [field("a"), field("b")],
            weather(["2026-08-01", "2026-08-02", "2026-08-03"]))
        identities = [("2026-08-01", "a", "initial"),
                      ("2026-08-01", "b", "initial")]
        for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
            for fid in ("a", "b"):
                identities.append((day, fid, "rain"))
        data["audit_sources"] = [
            dict(zip(("date", "field_id", "kind"), t)) for t in identities]
        self.assertEqual(len(data["audit_sources"]), 8)  # 先确认构造本身合法
        run(copy.deepcopy(data))
        data["audit_sources"] += [
            ident("2026-08-01", "a", "irrigation"),
            ident("2026-08-01", "b", "irrigation"),
            ident("2026-08-02", "a", "irrigation"),
            ident("2026-08-02", "b", "irrigation"),
            ident("2026-08-03", "a", "irrigation")]  # 13 条互不相同
        with self.assertRaisesRegex(ValueError, "12"):
            run(data)

    def test_weather_window_capped_at_sixty_days(self):
        dates = [f"2026-01-{i:02d}" for i in range(1, 32)]
        dates += [f"2026-02-{i:02d}" for i in range(1, 29)]  # 31+28=59 天
        dates.append("2026-03-01")                            # 共 60 天
        data = make_data([field("a")], weather(dates))
        run(data)  # 60 天为合法上界
        data["weather"].append({"date": "2026-03-02", "rain_mm": 0,
                                "et_mm": 0})                  # 第 61 天
        with self.assertRaisesRegex(ValueError, "60"):
            run(data)

    def test_active_field_cap_enforced(self):
        # 1 个 2m² 旧田拆成两个 1m² 新田：在册数 1 -> 2，逐层拆到超过 200。
        fields = [field("root", area=256, capacity=1000)]
        repartitions = []
        current = "root"
        current_area = 256
        day_index = 2
        while current_area >= 2:
            n = current_area  # 每个新田 1m²
            batch = {
                "date": f"2026-08-{day_index:02d}",
                "new_fields": [new_field(f"{current}-{i}", area=1,
                                         capacity=1000)
                               for i in range(n)],
                "moves": [
                    {"from_field_id": current,
                     "to_field_id": f"{current}-{i}", "area_m2": 1}
                    for i in range(n)],
            }
            repartitions.append(batch)
            current = f"{current}-0"
            current_area = 1
            break  # 一批即把 256m² 拆成 256 个新田，超过 200 在上界外
        dates = ["2026-08-01", "2026-08-02"]
        data = make_data(fields, weather(dates), repartitions=repartitions)
        with self.assertRaisesRegex(ValueError, "200"):
            run(data)

    def test_total_field_and_move_caps_enforced(self):
        # 总面积口径：4 个旧田各 996m²，996 个新田各 4m²，全连接 3984 条边。
        old_fields = [field(f"o{i}", area=996, capacity=1000)
                      for i in range(4)]
        new_fields = [new_field(f"n{i}", area=4, capacity=1000)
                      for i in range(996)]
        moves = [
            {"from_field_id": f"o{i}", "to_field_id": f"n{j}", "area_m2": 1}
            for i in range(4) for j in range(996)]
        data = make_data(
            old_fields, weather(["2026-08-01", "2026-08-02"]),
            repartitions=[{"date": "2026-08-02",
                           "new_fields": new_fields, "moves": moves}])
        # 4 + 996 = 1000 个田块恰好在上界，但 3984 条交接边超过 3000。
        with self.assertRaisesRegex(ValueError, "3000"):
            run(data)

    def test_total_field_cap_enforced_even_with_few_moves(self):
        # 5 个旧田共 997m² 拆成 997 个 1m² 新田：合计 1002 个田块，
        # moves 只有 997 条（未超 3000），应先触发 1000 田块上限。
        areas = [199, 199, 199, 199, 201]
        old_fields = [field(f"o{i}", area=a, capacity=10)
                      for i, a in enumerate(areas)]
        new_fields = [new_field(f"n{j}", area=1, capacity=10)
                      for j in range(997)]
        moves = []
        j = 0
        for i, area in enumerate(areas):
            for _ in range(area):
                moves.append({"from_field_id": f"o{i}",
                              "to_field_id": f"n{j}", "area_m2": 1})
                j += 1
        data = make_data(
            old_fields, weather(["2026-08-01", "2026-08-02"]),
            repartitions=[{"date": "2026-08-02",
                           "new_fields": new_fields, "moves": moves}])
        with self.assertRaisesRegex(ValueError, "1000"):
            run(data)

    def test_input_object_is_not_modified(self):
        data = self.good
        snapshot = copy.deepcopy(data)
        run(data)
        self.assertEqual(data, snapshot)


class PermutationTests(unittest.TestCase):
    def test_array_reordering_does_not_change_report(self):
        data = fixture_data()
        shuffled = copy.deepcopy(data)
        shuffled["applications"].reverse()
        shuffled["repartitions"].reverse()
        for batch in shuffled["repartitions"]:
            batch["moves"].reverse()
            batch["new_fields"].reverse()
        shuffled["audit_sources"].reverse()
        shuffled["audit_sinks"].reverse()
        shuffled["fields"].reverse()
        self.assertEqual(run(data), run(shuffled))


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
            [sys.executable, "-m", "irrigation.land_water_cli",
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
