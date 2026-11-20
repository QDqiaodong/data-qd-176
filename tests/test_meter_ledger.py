"""irrigation.meter_ledger 的自动化测试。

期望数值由本文件内的独立计算（手算口径 + 直接调用 irrigation.balance.step）给出，
不拿生产 report 的返回值充当独立期望。覆盖：持久化重开、乱序与低版本迟到、
同版本冲突整批回滚、整批合法但逐条中间为负、换表与缺口、精确分数水深、
结算冻结与连续正负差额、跨已结算/未结算边界、请求重试优先级、并发冲突，
以及在真实提交中断点杀死写进程的原子性测试。
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

from irrigation.balance import step
from irrigation import meter_ledger as ml

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENV = {**os.environ, "PYTHONPATH": SRC}


# ---------------------------------------------------------------------------
# 构造工具与独立期望计算（不调用 ml 的核算函数）
# ---------------------------------------------------------------------------

def field(fid="a", area=1, capacity=100, opening=0, threshold=0, target=0,
          infiltration=100, efficiency=1000):
    return {
        "id": fid, "area_m2": area, "capacity_mm": capacity,
        "opening_mm": opening, "threshold_mm": threshold, "target_mm": target,
        "infiltration_mm": infiltration, "efficiency_permille": efficiency,
    }


def weather(dates, rain=0, et=1):
    if isinstance(rain, int):
        rain = [rain] * len(dates)
    if isinstance(et, int):
        et = [et] * len(dates)
    return [{"date": d, "rain_mm": r, "et_mm": e}
            for d, r, e in zip(dates, rain, et)]


def meter(fid="a", opening_litres=0, price=1):
    return {"field_id": fid, "opening_litres": opening_litres,
            "price_microunits_per_litre": price}


def config(fields, weather_rows, meters):
    return {"fields": fields, "weather": weather_rows, "meters": meters}


def reading(fid, d, revision, closing, reset=None):
    return {"field_id": fid, "date": d, "revision": revision,
            "closing_litres": closing, "reset_opening_litres": reset}


def dates(prefix="2026-09-01", n=4):
    d0 = date.fromisoformat(prefix)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def oracle_gross(day_rows, opening_litres):
    """独立口径：按当前版本序列计算毛供水升数，未知返回 None。

    day_rows: [(iso, closing, reset_or_None) 或 None]，按气象日顺序。
    """
    result = []
    prev = opening_litres
    for row in day_rows:
        if row is None:
            result.append(None)
            prev = None
            continue
        _iso, closing, reset = row
        if reset is not None:
            litres = closing - reset
        elif prev is None:
            litres = None
        else:
            litres = closing - prev
        result.append(litres)
        prev = closing
    return result


def oracle_balance(field_obj, weather_rows, gross_litres):
    """独立口径：用 balance.step 逐日重算，缺口起全部 None。"""
    out = []
    opening = Fraction(field_obj["opening_mm"])
    pending = False
    for w, litres in zip(weather_rows, gross_litres):
        if litres is None:
            pending = True
        if pending:
            out.append(None)
            continue
        gross_mm = Fraction(litres, field_obj["area_m2"])
        r = step(field_obj, opening, Fraction(w["rain_mm"]), gross_mm,
                 Fraction(w["et_mm"]))
        out.append(r)
        opening = r["closing_mm"]
    return out


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ledger.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cfg):
        self.assertEqual(ml.create(self.db, cfg),
                         {"schema_version": 1, "generation": 0})
        return cfg


# ---------------------------------------------------------------------------
# 1. 必须可复核的更正算例（契约给定数值）
# ---------------------------------------------------------------------------

class MandatoryWorkedExample(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates("2026-09-01", 2)
        self.cfg = self.make(config(
            [field("f1", area=2)],
            weather(self.ds, rain=0, et=[8, 0]),
            [meter("f1", 0, 3)],
        ))

    def test_full_correction_and_settlement_story(self):
        db = self.db
        r1 = ml.apply(db, {"request_id": "req-1", "expected_generation": 0,
                           "readings": [reading("f1", self.ds[0], 1, 10),
                                        reading("f1", self.ds[1], 1, 20)]})
        self.assertEqual(r1["generation"], 1)
        rep = ml.report(db)
        # 独立期望：10 升 / 2 m2 = 5 mm；首日 ET 8 未满足 3；次日 closing 5
        self.assertEqual(oracle_gross(
            [(self.ds[0], 10, None), (self.ds[1], 20, None)], 0), [10, 10])
        d1, d2 = rep["daily"]
        self.assertEqual((d1["gross_litres"], d1["gross_mm"]), (10, "5"))
        self.assertEqual(d1["water_balance"]["unmet_et_mm"], "3")
        self.assertEqual(d1["water_balance"]["closing_mm"], "0")
        self.assertEqual(d2["water_balance"]["closing_mm"], "5")
        self.assertEqual(d2["water_balance"]["balance_residual_mm"], "0")

        c1 = ml.close(db, {"request_id": "close-1", "expected_generation": 1,
                           "through_date": self.ds[0]})
        self.assertEqual(c1["statement"]["total_amount_microunits"], 30)
        self.assertEqual(c1["statement"]["lines"], [{
            "field_id": "f1", "date": self.ds[0], "gross_litres": 10,
            "price_microunits_per_litre": 3, "amount_microunits": 30,
        }])

        r2 = ml.apply(db, {"request_id": "req-2", "expected_generation": 2,
                           "readings": [reading("f1", self.ds[0], 2, 12)]})
        self.assertEqual(r2["generation"], 3)
        self.assertEqual(len(r2["adjustments"]), 1)
        adj = r2["adjustments"][0]
        self.assertEqual((adj["delta_litres"], adj["delta_amount_microunits"]),
                         (2, 6))
        rep = ml.report(db)
        d1, d2 = rep["daily"]
        self.assertEqual([row["gross_litres"] for row in rep["daily"]], [12, 8])
        self.assertEqual(d1["water_balance"]["unmet_et_mm"], "2")
        self.assertEqual(d2["water_balance"]["closing_mm"], "4")
        # 原账单冻结不变
        self.assertEqual(rep["statements"][0]["total_amount_microunits"], 30)
        self.assertEqual(rep["statements"][0]["lines"][0]["gross_litres"], 10)

        r3 = ml.apply(db, {"request_id": "req-3", "expected_generation": 3,
                           "readings": [reading("f1", self.ds[0], 3, 11)]})
        adj3 = r3["adjustments"][0]
        # 财务已确认值是 12（原 10 + 前次 +2），故本次为 11-12 = -1
        self.assertEqual((adj3["previous_gross_litres"],
                          adj3["current_gross_litres"],
                          adj3["delta_litres"],
                          adj3["delta_amount_microunits"]), (12, 11, -1, -3))
        rep = ml.report(db)
        self.assertEqual(rep["financial_summary"], {
            "original_amount_microunits": 30,
            "adjustment_amount_microunits": 3,
            "recognized_amount_microunits": 33,
        })

        # 重发第一次更正：返回当时回执，不再改账
        replay = ml.apply(db, {"request_id": "req-2", "expected_generation": 2,
                               "readings": [reading("f1", self.ds[0], 2, 12)]})
        self.assertEqual(replay, r2)
        self.assertEqual(ml.report(db)["generation"], 4)

        c2 = ml.close(db, {"request_id": "close-2", "expected_generation": 4,
                           "through_date": self.ds[1]})
        self.assertEqual(c2["statement"]["from_date"], self.ds[1])
        self.assertEqual(c2["statement"]["lines"][0]["gross_litres"], 9)
        self.assertEqual(c2["statement"]["total_amount_microunits"], 27)
        rep = ml.report(db)
        self.assertEqual(rep["financial_summary"], {
            "original_amount_microunits": 57,
            "adjustment_amount_microunits": 3,
            "recognized_amount_microunits": 60,
        })
        # 恒等式：原结算升数 + 历次差额 = 当前实际升数
        self.assertEqual(10 + 2 - 1, rep["daily"][0]["gross_litres"])


# ---------------------------------------------------------------------------
# 2. 持久化重开
# ---------------------------------------------------------------------------

class PersistenceReopen(LedgerCase):
    def test_reopen_same_file_keeps_everything(self):
        ds = dates()
        cfg = self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5),
                                        reading("a", ds[1], 1, 9)]})
        ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                           "through_date": ds[1]})
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 2,
                           "readings": [reading("a", ds[0], 2, 7)]})
        first = ml.report(self.db)
        # 全新连接、全新进程视角重新打开
        second = ml.report(self.db)
        self.assertEqual(first, second)
        self.assertEqual(second["generation"], 3)
        # 物理文件中确实落盘
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM reading_versions").fetchone()[0], 3)
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key='generation'").fetchone()[0], "3")
        conn.close()

    def test_create_never_overwrites_and_cleans_failed_init(self):
        ds = dates(n=1)
        cfg = config([field("a")], weather(ds), [meter("a")])
        self.make(cfg)
        with self.assertRaises(OSError):
            ml.create(self.db, cfg)
        # 指向目录等不可用路径报 OSError
        with self.assertRaises(OSError):
            ml.create(str(Path(self.tmp.name) / "subdir" / "x.db"), cfg)
        # 无效配置失败且不留文件
        bad_db = str(Path(self.tmp.name) / "bad.db")
        bad_cfg = copy.deepcopy(cfg)
        bad_cfg["meters"][0]["opening_litres"] = -1
        with self.assertRaises(ValueError):
            ml.create(bad_db, bad_cfg)
        self.assertFalse(Path(bad_db).exists())

    def test_foreign_db_and_bad_version_rejected(self):
        other = str(Path(self.tmp.name) / "other.db")
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            ml.report(other)
        # 篡改 application_id
        ds = dates(n=1)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA application_id=1")
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            ml.report(self.db)


# ---------------------------------------------------------------------------
# 3. 乱序、低版本迟到、重放与同版本冲突
# ---------------------------------------------------------------------------

class RevisionOrdering(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates()
        self.make(config([field("a")], weather(self.ds), [meter("a")]))

    def test_out_of_order_and_late_low_revision(self):
        ds = self.ds
        # 先到 rev3，再在两个请求里迟到 rev1、rev2（同请求内同田块日只允许一行）
        ml.apply(self.db, {"request_id": "q-hi", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 3, 30)]})
        ml.apply(self.db, {"request_id": "q-low1", "expected_generation": 1,
                           "readings": [reading("a", ds[0], 1, 10)]})
        r_low = ml.apply(self.db, {"request_id": "q-low2", "expected_generation": 2,
                                   "readings": [reading("a", ds[0], 2, 20)]})
        # 首次归档 rev2，但当前版本仍是 rev3
        self.assertEqual([v["revision"] for v in r_low["accepted_versions"]], [2])
        self.assertEqual(r_low["current_changes"], [])
        self.assertEqual(r_low["adjustments"], [])
        rep = ml.report(self.db)
        self.assertEqual(rep["daily"][0]["current_revision"], 3)
        self.assertEqual(rep["daily"][0]["gross_litres"], 30)
        self.assertEqual(rep["generation"], 3)  # 即使只归档也加一

    def test_identical_replay_keeps_generation(self):
        ds = self.ds
        req = {"request_id": "q1", "expected_generation": 0,
               "readings": [reading("a", ds[0], 1, 5)]}
        first = ml.apply(self.db, req)
        # 同 request_id 同内容，数组顺序与键顺序不同仍视为重试
        again = ml.apply(self.db, {
            "request_id": "q1", "expected_generation": 0,
            "readings": [{"reset_opening_litres": None, "date": ds[0],
                          "closing_litres": 5, "revision": 1, "field_id": "a"}],
        })
        self.assertEqual(again, first)
        self.assertEqual(ml.report(self.db)["generation"], 1)

    def test_same_revision_different_content_rolls_back_whole_batch(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 10),
                                        reading("a", ds[1], 1, 20)]})
        # 一批两行：一行全新合法，一行与已存 rev1 内容不同 -> 整批回滚
        with self.assertRaisesRegex(ValueError, "读数版本冲突"):
            ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                               "readings": [reading("a", ds[2], 1, 30),
                                            reading("a", ds[0], 1, 11)]})
        rep = ml.report(self.db)
        self.assertEqual(rep["generation"], 1)
        self.assertIsNone(rep["daily"][2]["gross_litres"])  # 合法那行也未写入
        self.assertEqual(rep["daily"][0]["gross_litres"], 10)

    def test_null_reset_is_full_version_not_inherited_flag(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 10, reset=0)]})
        # rev2 的 reset=null 是完整版本：次日按普通差分，不能沿用换表标志
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                           "readings": [reading("a", ds[0], 2, 12, reset=None)]})
        ml.apply(self.db, {"request_id": "q3", "expected_generation": 2,
                           "readings": [reading("a", ds[1], 1, 15)]})
        rep = ml.report(self.db)
        self.assertEqual(rep["daily"][0]["gross_litres"], 12)  # 12 - 配置 opening 0
        self.assertEqual(rep["daily"][1]["gross_litres"], 3)   # 15 - 12

    def test_duplicate_field_date_in_one_request_rejected(self):
        ds = self.ds
        with self.assertRaisesRegex(ValueError, "重复"):
            ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                               "readings": [reading("a", ds[0], 1, 10),
                                            reading("a", ds[0], 2, 11)]})


# ---------------------------------------------------------------------------
# 4. 整批合法但逐条中间状态为负
# ---------------------------------------------------------------------------

class WholeBatchCandidacy(LedgerCase):
    def test_batch_legal_but_naive_per_row_order_goes_negative(self):
        ds = dates(n=2)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 100),
                                        reading("a", ds[1], 1, 110)]})
        # 若按数组顺序逐条生效，day1 改 120 后旧 day2=110 会临时为 -10；
        # 整体候选下 day2=125，最终差分 120 与 5 均非负，必须接受。
        receipt = ml.apply(self.db, {
            "request_id": "q2", "expected_generation": 1,
            "readings": [reading("a", ds[0], 2, 120),
                         reading("a", ds[1], 2, 125)],
        })
        self.assertEqual(receipt["generation"], 2)
        rep = ml.report(self.db)
        self.assertEqual([r["gross_litres"] for r in rep["daily"][:2]], [120, 5])

    def test_negative_in_final_candidate_fails_and_bumps_nothing(self):
        ds = dates(n=2)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 100),
                                        reading("a", ds[1], 1, 110)]})
        with self.assertRaisesRegex(ValueError, "负"):
            ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                               "readings": [reading("a", ds[1], 2, 99)]})
        self.assertEqual(ml.report(self.db)["generation"], 1)


# ---------------------------------------------------------------------------
# 5. 换表与缺口对计量和土壤水账的影响
# ---------------------------------------------------------------------------

class ResetAndGap(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates()
        self.make(config([field("a")], weather(self.ds),
                         [meter("a", opening_litres=100)]))

    def test_reset_day_meters_independently_and_gap_holds_balance_pending(self):
        ds = self.ds
        # 缺 day1；day2 换表（新表 0 起，期末 7）；day3 普通日 15
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[1], 1, 7, reset=0),
                                        reading("a", ds[2], 1, 15)]})
        rep = ml.report(self.db)
        expected_litres = oracle_gross(
            [None, (ds[1], 7, 0), (ds[2], 15, None), None], 100)
        self.assertEqual(expected_litres, [None, 7, 8, None])
        self.assertEqual([r["gross_litres"] for r in rep["daily"]], expected_litres)
        # 换表日计量已知，但土壤水账因 day1 缺口仍待补；换表不重置储水
        self.assertEqual(rep["daily"][1]["meter_status"], "ready")
        self.assertEqual(rep["daily"][1]["balance_status"], "pending")
        self.assertIsNone(rep["daily"][1]["water_balance"])
        # 补齐 day1（105-100=5）后，全部已计量日水账就绪并与独立口径一致
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                           "readings": [reading("a", ds[0], 1, 105)]})
        rep = ml.report(self.db)
        gross = [5, 7, 8, None]
        balances = oracle_balance(field("a"), weather(ds), gross)
        for row, litres, bal in zip(rep["daily"], gross, balances):
            self.assertEqual(row["gross_litres"], litres)
            if bal is None:
                self.assertEqual(row["balance_status"], "pending")
            else:
                self.assertEqual(row["balance_status"], "ready")
                self.assertEqual(row["water_balance"]["closing_mm"],
                                 str(bal["closing_mm"]))
                self.assertEqual(row["water_balance"]["balance_residual_mm"], "0")

    def test_day_after_reset_uses_reset_day_closing(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 105),
                                        reading("a", ds[1], 1, 7, reset=0),
                                        reading("a", ds[2], 1, 7)]})
        rep = ml.report(self.db)
        # day3: 7 - 7（换表日 closing）= 0
        self.assertEqual([r["gross_litres"] for r in rep["daily"][:3]], [5, 7, 0])
        # 0 升也是有效计量，meter ready
        self.assertEqual(rep["daily"][2]["meter_status"], "ready")


# ---------------------------------------------------------------------------
# 6. 精确分数水深
# ---------------------------------------------------------------------------

class FractionalDepth(LedgerCase):
    def test_fraction_gross_mm_and_later_day_changes(self):
        ds = dates(n=3)
        # area_m2=2：7 升 = 7/2 mm；efficiency 1000、et 0，opening 0
        self.make(config([field("a", area=2)],
                         weather(ds, rain=0, et=0), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 7),
                                        reading("a", ds[1], 1, 8)]})
        rep = ml.report(self.db)
        self.assertEqual(rep["daily"][0]["gross_mm"], "7/2")
        self.assertEqual(rep["daily"][0]["water_balance"]["gross_irrigation_mm"],
                         "7/2")
        self.assertEqual(rep["daily"][0]["water_balance"]["rain_mm"], "0")
        self.assertIsInstance(
            rep["daily"][0]["water_balance"]["advised_next_gross_mm"], int)
        # 次日 1 升 = 1/2 mm，储水累计 7/2 + 1/2 = 4
        self.assertEqual(rep["daily"][1]["gross_mm"], "1/2")
        self.assertEqual(rep["daily"][1]["water_balance"]["closing_mm"], "4")
        # 独立口径核对
        balances = oracle_balance(field("a", area=2), weather(ds, rain=0, et=0),
                                  [7, 1, None])
        self.assertEqual(rep["daily"][1]["water_balance"]["closing_mm"],
                         str(balances[1]["closing_mm"]))
        # 更正 day1 为 6 升：day1=6/2=3，day2=(8-6)=2 升=1mm，closing 仍 4
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                           "readings": [reading("a", ds[0], 2, 6)]})
        rep = ml.report(self.db)
        self.assertEqual(rep["daily"][0]["gross_mm"], "3")
        self.assertEqual(rep["daily"][1]["gross_mm"], "1")
        self.assertEqual(rep["daily"][1]["water_balance"]["opening_mm"], "3")
        self.assertEqual(rep["daily"][1]["water_balance"]["closing_mm"], "4")


# ---------------------------------------------------------------------------
# 7. 结算冻结、连续正负差额与跨边界
# ---------------------------------------------------------------------------

class SettlementAndAdjustments(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates()
        # 两个田块，验证多田块行排序与各自单价
        self.make(config(
            [field("a", area=1), field("b", area=2)],
            weather(self.ds), [meter("a", 0, 3), meter("b", 0, 1)],
        ))

    def _seed(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [
                               reading("a", ds[0], 1, 10), reading("a", ds[1], 1, 20),
                               reading("a", ds[2], 1, 30), reading("a", ds[3], 1, 40),
                               reading("b", ds[0], 1, 4), reading("b", ds[1], 1, 8),
                               reading("b", ds[2], 1, 10), reading("b", ds[3], 1, 12),
                           ]})

    def test_statement_freeze_and_signed_adjustment_chain(self):
        ds = self.ds
        self._seed()
        c1 = ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                                "through_date": ds[1]})
        # 行按 date、field_id 排序
        self.assertEqual([(l["date"], l["field_id"]) for l in c1["statement"]["lines"]],
                         [(ds[0], "a"), (ds[0], "b"), (ds[1], "a"), (ds[1], "b")])
        frozen_total = c1["statement"]["total_amount_microunits"]
        # a: (10+10)*3=60; b: (4+4)*1=8
        self.assertEqual(frozen_total, 68)

        # 更正已结算的 day1（a）：10 -> 12，影响 day2 差分 10 -> 8
        r2 = ml.apply(self.db, {"request_id": "q2", "expected_generation": 2,
                                "readings": [reading("a", ds[0], 2, 12)]})
        self.assertEqual(sorted((a["date"], a["field_id"]) for a in r2["adjustments"]),
                         [(ds[0], "a"), (ds[1], "a")])
        for adj in r2["adjustments"]:
            self.assertEqual(adj["statement_id"], c1["statement"]["statement_id"])
        by_date = {a["date"]: a for a in r2["adjustments"]}
        self.assertEqual((by_date[ds[0]]["delta_litres"],
                          by_date[ds[0]]["delta_amount_microunits"]), (2, 6))
        self.assertEqual((by_date[ds[1]]["delta_litres"],
                          by_date[ds[1]]["delta_amount_microunits"]), (-2, -6))

        # 再次更正 day1：12 -> 11，差额基于财务已确认值（12、8）而非最初账单
        r3 = ml.apply(self.db, {"request_id": "q3", "expected_generation": 3,
                                "readings": [reading("a", ds[0], 3, 11)]})
        by_date = {a["date"]: a for a in r3["adjustments"]}
        self.assertEqual(by_date[ds[0]]["previous_gross_litres"], 12)
        self.assertEqual(by_date[ds[0]]["delta_litres"], -1)
        self.assertEqual(by_date[ds[1]]["previous_gross_litres"], 8)
        self.assertEqual(by_date[ds[1]]["delta_litres"], 1)

        rep = ml.report(self.db)
        # 原单据逐值冻结
        stmt = rep["statements"][0]
        self.assertEqual(stmt["total_amount_microunits"], frozen_total)
        self.assertEqual([l["gross_litres"] for l in stmt["lines"]],
                         [10, 4, 10, 4])
        # 财务汇总：原 68，差额净额 0（+2-2-1+1 升，a 单价 3 -> 金额净 0）
        self.assertEqual(rep["financial_summary"]["adjustment_amount_microunits"], 0)
        self.assertEqual(rep["financial_summary"]["recognized_amount_microunits"], 68)
        # 恒等式逐行核对
        recognized = {}
        for l in stmt["lines"]:
            recognized[(l["field_id"], l["date"])] = l["gross_litres"]
        for adj in rep["adjustments"]:
            recognized[(adj["field_id"], adj["date"])] += adj["delta_litres"]
        current = {(r["field_id"], r["date"]): r["gross_litres"]
                   for r in rep["daily"]}
        for key, value in recognized.items():
            self.assertEqual(value, current[key])

    def test_cross_settled_unsettled_boundary(self):
        ds = self.ds
        self._seed()
        ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                           "through_date": ds[1]})
        # 修订 day2（已结算末日）：a 20 -> 22；day2 自身 +2，day3 差分 -2（未结算）
        r = ml.apply(self.db, {"request_id": "q2", "expected_generation": 2,
                               "readings": [reading("a", ds[1], 2, 22)]})
        adjusted_dates = {a["date"] for a in r["adjustments"]}
        self.assertEqual(adjusted_dates, {ds[1]})  # day3 未结算，不生成差额
        rep = ml.report(self.db)
        current = {(row["field_id"], row["date"]): row["gross_litres"]
                   for row in rep["daily"]}
        self.assertEqual(current[("a", ds[1])], 12)  # 22 - day1 10
        self.assertEqual(current[("a", ds[2])], 8)   # 30 - 22
        self.assertEqual(current[("a", ds[3])], 10)  # 40 - 30
        # 结算 day2..day3 必须按当前值，且区间严格前进
        c2 = ml.close(self.db, {"request_id": "c2", "expected_generation": 3,
                                "through_date": ds[3]})
        self.assertEqual(c2["statement"]["from_date"], ds[2])
        a_lines = {l["date"]: l for l in c2["statement"]["lines"]
                   if l["field_id"] == "a"}
        self.assertEqual(a_lines[ds[2]]["gross_litres"], 8)

    def test_zero_price_adjustment_kept_and_zero_delta_skipped(self):
        ds = dates(n=2)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "z.db")
        self.make(config([field("a")], weather(ds), [meter("a", 0, 0)]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 10),
                                        reading("a", ds[1], 1, 20)]})
        ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                           "through_date": ds[1]})
        r = ml.apply(self.db, {"request_id": "q2", "expected_generation": 2,
                               "readings": [reading("a", ds[0], 2, 13)]})
        # 0 单价但升数非零更正：仍生成金额为 0 的差额
        self.assertEqual(len(r["adjustments"]), 2)
        self.assertTrue(all(a["delta_amount_microunits"] == 0
                            for a in r["adjustments"]))
        # 量值没变的当前版本切换不产生差额
        r2 = ml.apply(self.db, {"request_id": "q3", "expected_generation": 3,
                                "readings": [reading("a", ds[0], 3, 13)]})
        self.assertEqual(r2["current_changes"][0]["revision"], 3)
        self.assertEqual(r2["adjustments"], [])

    def test_close_requires_known_litres_and_strict_advance(self):
        ds = self.ds
        # 制造缺口：通过重新建库、只录入部分日期实现
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "g.db")
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5),
                                        reading("a", ds[2], 1, 9, reset=0)]})
        with self.assertRaisesRegex(ValueError, "未知毛供水"):
            ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                               "through_date": ds[2]})
        self.assertEqual(ml.report(self.db)["generation"], 1)
        # 超出气象窗口
        with self.assertRaises(ValueError):
            ml.close(self.db, {"request_id": "c2", "expected_generation": 1,
                               "through_date": "2099-01-01"})

    def test_close_fails_when_any_field_day_is_unknown(self):
        ds = self.ds
        # 只录入田块 a，田块 b 全程缺读数：结算必须整次失败
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[i], 1, 4 + i)
                                        for i in range(4)]})
        with self.assertRaisesRegex(ValueError, "b"):
            ml.close(self.db, {"request_id": "c1", "expected_generation": 1,
                               "through_date": ds[3]})
        self.assertEqual(ml.report(self.db)["generation"], 1)
        # 补齐 b 后成功：a 共 7 升×3、b 共 4 升×1
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                           "readings": [reading("b", ds[i], 1, i + 1)
                                        for i in range(4)]})
        c = ml.close(self.db, {"request_id": "c2", "expected_generation": 2,
                               "through_date": ds[3]})
        self.assertEqual(c["statement"]["total_amount_microunits"], 7 * 3 + 4 * 1)


# ---------------------------------------------------------------------------
# 8. 重试优先级与 request_id 规则
# ---------------------------------------------------------------------------

class RetryPriority(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates(n=2)
        self.make(config([field("a")], weather(self.ds), [meter("a")]))

    def test_retry_wins_over_stale_generation(self):
        ds = self.ds
        req = {"request_id": "q1", "expected_generation": 0,
               "readings": [reading("a", ds[0], 1, 5)]}
        first = ml.apply(self.db, req)
        # 先让 generation 前进
        ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                           "readings": [reading("a", ds[1], 1, 9)]})
        # 旧 expected_generation 的精确重试仍返回原回执
        replay = ml.apply(self.db, req)
        self.assertEqual(replay, first)
        self.assertEqual(replay["generation"], 1)

    def test_same_id_different_content_or_operation_fails(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "shared", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5)]})
        # 不同内容（即使 expected_generation 改成当前值）
        with self.assertRaisesRegex(ValueError, "request_id"):
            ml.apply(self.db, {"request_id": "shared", "expected_generation": 1,
                               "readings": [reading("a", ds[0], 1, 6)]})
        # 不同操作复用同一 id
        with self.assertRaisesRegex(ValueError, "request_id"):
            ml.close(self.db, {"request_id": "shared",
                               "expected_generation": 1, "through_date": ds[0]})
        # expected_generation 也属于内容
        with self.assertRaisesRegex(ValueError, "request_id"):
            ml.apply(self.db, {"request_id": "shared", "expected_generation": 9,
                               "readings": [reading("a", ds[0], 1, 5)]})

    def test_unused_id_requires_current_generation(self):
        ds = self.ds
        with self.assertRaisesRegex(ValueError, "generation 冲突"):
            ml.apply(self.db, {"request_id": "q1", "expected_generation": 5,
                               "readings": [reading("a", ds[0], 1, 5)]})
        # 失败不占用 request_id
        ok = ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                                "readings": [reading("a", ds[0], 1, 5)]})
        self.assertEqual(ok["generation"], 1)

    def test_close_retry_returns_original_statement_id(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5),
                                        reading("a", ds[1], 1, 9)]})
        creq = {"request_id": "c1", "expected_generation": 1,
                "through_date": ds[1]}
        first = ml.close(self.db, creq)
        again = ml.close(self.db, creq)
        self.assertEqual(again, first)
        self.assertEqual(ml.report(self.db)["generation"], 2)


# ---------------------------------------------------------------------------
# 9. 并发：同请求只记账一次；不同请求同旧 generation 至多一个提交
# ---------------------------------------------------------------------------

class Concurrency(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ds = dates(n=3)
        self.make(config([field("a")], weather(self.ds), [meter("a")]))

    def test_identical_concurrent_requests_commit_once(self):
        ds = self.ds
        req = {"request_id": "same", "expected_generation": 0,
               "readings": [reading("a", ds[0], 1, 5)]}
        results, errors = [], []

        def worker():
            try:
                results.append(ml.apply(self.db, copy.deepcopy(req)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r == results[0] for r in results))
        rep = ml.report(self.db)
        self.assertEqual(rep["generation"], 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM reading_versions").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM requests").fetchone()[0], 1)
        conn.close()

    def test_distinct_stale_generation_requests_only_one_wins(self):
        ds = self.ds

        def req(i):
            return {"request_id": f"r{i}", "expected_generation": 0,
                    "readings": [reading("a", ds[0], 1, 5 + i)]}

        outcomes = {"ok": [], "conflict": []}

        def worker(i):
            try:
                ml.apply(self.db, req(i))
                outcomes["ok"].append(i)
            except ValueError:
                outcomes["conflict"].append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes["ok"]), 1)
        self.assertEqual(len(outcomes["conflict"]), 5)
        self.assertEqual(ml.report(self.db)["generation"], 1)
        # 失败请求不占用 request_id；获胜者用原 expected_generation 精确重试
        winner = outcomes["ok"][0]
        again = ml.apply(self.db, req(winner))
        self.assertEqual(again["request_id"], f"r{winner}")
        self.assertEqual(again["generation"], 1)

    def test_concurrent_close_and_apply_remain_consistent(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "seed", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5),
                                        reading("a", ds[1], 1, 9)]})
        errors = []

        def do_apply():
            try:
                ml.apply(self.db, {"request_id": "app", "expected_generation": 1,
                                   "readings": [reading("a", ds[0], 2, 6)]})
            except ValueError as exc:
                errors.append(("apply", str(exc)))

        def do_close():
            try:
                ml.close(self.db, {"request_id": "cls", "expected_generation": 1,
                                   "through_date": ds[1]})
            except ValueError as exc:
                errors.append(("close", str(exc)))

        t1, t2 = threading.Thread(target=do_apply), threading.Thread(target=do_close)
        t1.start(); t2.start()
        t1.join(); t2.join()
        rep = ml.report(self.db)
        # 恰有一方提交成功，generation 只前进一次到 2；快照内部自洽
        self.assertEqual(rep["generation"], 2)
        self.assertEqual(len(errors), 1)
        # 无论谁先成功，已结算田块日都满足“原账单 + 历次差额 = 当前实际升数”
        recognized = {}
        for stmt in rep["statements"]:
            for l in stmt["lines"]:
                recognized[(l["field_id"], l["date"])] = l["gross_litres"]
        for adj in rep["adjustments"]:
            key = (adj["field_id"], adj["date"])
            recognized[key] = recognized.get(key, 0) + adj["delta_litres"]
        current = {(r["field_id"], r["date"]): r["gross_litres"]
                   for r in rep["daily"]}
        for key, value in recognized.items():
            self.assertEqual(value, current[key])

    def test_external_write_lock_surfaces_as_database_error(self):
        ds = self.ds
        ml.apply(self.db, {"request_id": "seed", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5)]})
        # 另一连接持有写锁：本进程写请求得到 sqlite3.Error（CLI 归类退出 2）
        old_timeout = ml.TIMEOUT_MS
        ml.TIMEOUT_MS = 200
        try:
            holder = sqlite3.connect(self.db, isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            box = {}

            def attempt():
                try:
                    ml.apply(self.db, {"request_id": "blocked",
                                       "expected_generation": 1,
                                       "readings": [reading("a", ds[0], 2, 6)]})
                except sqlite3.Error as exc:
                    box["exc"] = exc

            t = threading.Thread(target=attempt)
            t.start()
            t.join()
            holder.execute("ROLLBACK")
            holder.close()
        finally:
            ml.TIMEOUT_MS = old_timeout
        self.assertIsInstance(box["exc"], sqlite3.Error)
        # 锁等待期间没有任何写入
        self.assertEqual(ml.report(self.db)["generation"], 1)


# ---------------------------------------------------------------------------
# 10. 提交中断点原子性：真实杀死写进程
# ---------------------------------------------------------------------------

CRASH_RUNNER = r"""
import sys
sys.path.insert(0, %(src)r)
from irrigation import meter_ledger as ml
db, mode = sys.argv[1], sys.argv[2]
ds = ["2026-09-01", "2026-09-02"]
if mode == "apply":
    # 种子提交后 generation=1；本请求在提交中断点崩溃
    ml.apply(db, {"request_id": "crash-req", "expected_generation": 1,
                  "readings": [
                      {"field_id": "a", "date": ds[0], "revision": 2,
                       "closing_litres": 7, "reset_opening_litres": None},
                  ]})
else:
    ml.close(db, {"request_id": "crash-close", "expected_generation": 1,
                  "through_date": ds[1]})
"""


class AtomicCommitCrash(LedgerCase):
    def _db_with_seed(self):
        ds = dates(n=2)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "seed", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5),
                                        reading("a", ds[1], 1, 9)]})
        return ds

    def _run_crash(self, mode):
        env = {**ENV, "IRRIGATION_LEDGER_CRASH_POINT": "before_commit"}
        runner = str(Path(self.tmp.name) / "runner.py")
        Path(runner).write_text(CRASH_RUNNER % {"src": SRC})
        return subprocess.run([sys.executable, "-I", runner, self.db, mode],
                              capture_output=True, text=True, env=env)

    def test_crash_before_commit_leaves_no_partial_effect(self):
        ds = self._db_with_seed()
        proc = self._run_crash("apply")
        self.assertEqual(proc.returncode, 9, proc.stderr)
        # 只能观察到完整提交前状态
        rep = ml.report(self.db)
        self.assertEqual(rep["generation"], 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM requests WHERE request_id='crash-req'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM adjustments").fetchone()[0], 0)
        conn.close()
        # 同一 request_id 重试：仅一份效果，成功一次
        retry = ml.apply(self.db, {"request_id": "crash-req",
                                   "expected_generation": 1,
                                   "readings": [reading("a", ds[0], 2, 7)]})
        self.assertEqual(retry["generation"], 2)
        again = ml.apply(self.db, {"request_id": "crash-req",
                                   "expected_generation": 1,
                                   "readings": [reading("a", ds[0], 2, 7)]})
        self.assertEqual(again, retry)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM requests WHERE request_id='crash-req'").fetchone()[0], 1)
        conn.close()

    def test_close_crash_before_commit_leaves_no_statement(self):
        ds = self._db_with_seed()
        proc = self._run_crash("close")
        self.assertEqual(proc.returncode, 9, proc.stderr)
        rep = ml.report(self.db)
        self.assertEqual(rep["statements"], [])
        self.assertEqual(rep["generation"], 1)
        # 重新结算成功，statement_id 在重开/重试后稳定
        c1 = ml.close(self.db, {"request_id": "crash-close",
                                "expected_generation": 1, "through_date": ds[1]})
        c2 = ml.close(self.db, {"request_id": "crash-close",
                                "expected_generation": 1, "through_date": ds[1]})
        self.assertEqual(c1, c2)
        self.assertEqual(ml.report(self.db)["statements"][0]["statement_id"],
                         c1["statement"]["statement_id"])


# ---------------------------------------------------------------------------
# 11. 输入校验与不可变性
# ---------------------------------------------------------------------------

class ValidationAndImmutability(LedgerCase):
    def _base_cfg(self):
        ds = dates(n=2)
        return config([field("a")], weather(ds), [meter("a")]), ds

    def test_bool_rejected_everywhere_and_identity_rules(self):
        cfg, ds = self._base_cfg()
        bad = copy.deepcopy(cfg)
        bad["meters"][0]["opening_litres"] = True
        with self.assertRaises(ValueError):
            ml.create(str(Path(self.tmp.name) / "b.db"), bad)
        self.make(cfg)
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "q", "expected_generation": 0,
                               "readings": [reading("a", ds[0], True, 5)]})
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "q", "expected_generation": 0,
                               "readings": [reading("a", ds[0], 1, -1)]})
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "q", "expected_generation": 0,
                               "readings": [{"field_id": "a", "date": ds[0],
                                             "revision": 1, "closing_litres": 5}]})
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "q", "expected_generation": 0,
                               "readings": [reading("zzz", ds[0], 1, 5)]})
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "q", "expected_generation": 0,
                               "readings": [reading("a", "2026-09-09", 1, 5)]})
        with self.assertRaises(ValueError):
            ml.apply(self.db, {"request_id": "", "expected_generation": 0,
                               "readings": [reading("a", ds[0], 1, 5)]})

    def test_config_meters_must_cover_fields_and_no_applications(self):
        cfg, ds = self._base_cfg()
        missing = copy.deepcopy(cfg)
        missing["meters"] = []
        with self.assertRaises(ValueError):
            ml.create(str(Path(self.tmp.name) / "m.db"), missing)
        dup = copy.deepcopy(cfg)
        dup["meters"] = [meter("a"), meter("a")]
        with self.assertRaises(ValueError):
            ml.create(str(Path(self.tmp.name) / "d.db"), dup)
        with_apps = copy.deepcopy(cfg)
        with_apps["applications"] = []
        with self.assertRaises(ValueError):
            ml.create(str(Path(self.tmp.name) / "a.db"), with_apps)

    def test_input_objects_not_modified(self):
        cfg, ds = self._base_cfg()
        cfg_snapshot = copy.deepcopy(cfg)
        self.make(cfg)
        self.assertEqual(cfg, cfg_snapshot)
        req = {"request_id": "q1", "expected_generation": 0,
               "readings": [reading("a", ds[0], 1, 5), reading("a", ds[1], 1, 9)]}
        snapshot = copy.deepcopy(req)
        ml.apply(self.db, req)
        self.assertEqual(req, snapshot)
        creq = {"request_id": "c1", "expected_generation": 1,
                "through_date": ds[1]}
        csnap = copy.deepcopy(creq)
        ml.close(self.db, creq)
        self.assertEqual(creq, csnap)

    def test_weather_gap_and_field_limits_reuse_base_validation(self):
        ds = dates(n=2)
        bad_weather = [{"date": ds[0], "rain_mm": 0, "et_mm": 1},
                       {"date": "2026-09-03", "rain_mm": 0, "et_mm": 1}]
        with self.assertRaisesRegex(ValueError, "consecutive"):
            ml.create(str(Path(self.tmp.name) / "w.db"),
                     config([field("a")], bad_weather, [meter("a")]))


# ---------------------------------------------------------------------------
# 12. report 结构与排序
# ---------------------------------------------------------------------------

class ReportShape(LedgerCase):
    def test_covers_all_field_days_and_orders_everything(self):
        ds = dates(n=3)
        self.make(config([field("a"), field("b")], weather(ds),
                         [meter("a"), meter("b")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 4),
                                        reading("b", ds[0], 1, 6)]})
        rep = ml.report(self.db)
        self.assertEqual(rep["schema_version"], 1)
        self.assertEqual(len(rep["daily"]), 6)
        keys = [(r["field_id"], r["date"]) for r in rep["daily"]]
        self.assertEqual(keys, sorted(keys))
        # pending 行完整保留，不被忽略
        pending = [r for r in rep["daily"] if r["meter_status"] == "pending"]
        self.assertEqual(len(pending), 4)
        self.assertTrue(all(r["gross_litres"] is None and r["gross_mm"] is None
                            and r["water_balance"] is None for r in pending))
        ready = rep["daily"][0]
        self.assertEqual(set(ready), {
            "field_id", "date", "current_revision", "meter_status",
            "gross_litres", "gross_mm", "balance_status", "water_balance",
        })
        wb_keys = set(ready["water_balance"])
        self.assertIn("field_id", wb_keys)
        self.assertIn("date", wb_keys)
        self.assertIsInstance(ready["water_balance"]["advised_next_gross_mm"], int)
        self.assertEqual(rep["statements"], [])
        self.assertEqual(rep["adjustments"], [])
        self.assertEqual(rep["financial_summary"], {
            "original_amount_microunits": 0,
            "adjustment_amount_microunits": 0,
            "recognized_amount_microunits": 0,
        })

    def test_report_is_read_only(self):
        ds = dates(n=1)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        before = ml.report(self.db)
        ml.report(self.db)
        self.assertEqual(ml.report(self.db), before)


class NoOpAndReplayGeneration(LedgerCase):
    def test_pure_evidence_replay_still_bumps_generation(self):
        ds = dates(n=2)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5)]})
        # 全部读数都是已存版本的证据重放：仍成功且 generation 加一
        receipt = ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                                     "readings": [reading("a", ds[0], 1, 5)]})
        self.assertEqual(receipt["accepted_versions"], [])
        self.assertEqual(receipt["current_changes"], [])
        self.assertEqual(receipt["adjustments"], [])
        self.assertEqual(receipt["generation"], 2)
        self.assertEqual(ml.report(self.db)["generation"], 2)

    def test_switch_to_equal_metered_value_is_a_current_change(self):
        ds = dates(n=1)
        self.make(config([field("a")], weather(ds), [meter("a")]))
        ml.apply(self.db, {"request_id": "q1", "expected_generation": 0,
                           "readings": [reading("a", ds[0], 1, 5)]})
        # 新版本量值相同（closing 相同、无换表）：当前版本切换但无用水差额
        receipt = ml.apply(self.db, {"request_id": "q2", "expected_generation": 1,
                                     "readings": [reading("a", ds[0], 2, 5)]})
        self.assertEqual([c["revision"] for c in receipt["current_changes"]], [2])
        self.assertEqual(receipt["adjustments"], [])
        self.assertEqual(ml.report(self.db)["daily"][0]["current_revision"], 2)


class LedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        self.db = str(Path(self.folder) / "ledger.db")
        self.ds = dates("2026-09-01", 2)
        self.cfg = config(
            [field("f1", area=2)], weather(self.ds, rain=0, et=[8, 0]),
            [meter("f1", 0, 3)])

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, payload):
        path = Path(self.folder) / name
        if isinstance(payload, str):
            path.write_text(payload)
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False))
        return str(path)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "irrigation.ledger_cli", *args],
            capture_output=True, text=True, env=ENV,
        )

    def test_full_cli_lifecycle_and_report_publish(self):
        cfg_path = self._write("config.json", self.cfg)
        r = self._run("create", "--db", self.db, "--config", cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"schema_version": 1, "generation": 0})

        apply_path = self._write("apply1.json", {
            "request_id": "req-1", "expected_generation": 0, "readings": [
                reading("f1", self.ds[0], 1, 10),
                reading("f1", self.ds[1], 1, 20)]})
        r = self._run("apply", "--db", self.db, "--source", apply_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["generation"], 1)

        close_path = self._write("close1.json", {
            "request_id": "close-1", "expected_generation": 1,
            "through_date": self.ds[0]})
        r = self._run("close", "--db", self.db, "--source", close_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        statement = json.loads(r.stdout)["statement"]
        self.assertEqual(statement["total_amount_microunits"], 30)

        report_path = str(Path(self.folder) / "report.json")
        r = self._run("report", "--db", self.db, "--output", report_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(Path(report_path).read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["financial_summary"]["original_amount_microunits"], 30)

        # 目标已存在时保留原文件并退出 2
        r = self._run("report", "--db", self.db, "--output", report_path)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(Path(report_path).read_text()), report)

        # 已有数据库上 create 失败退出 2，不覆盖
        r = self._run("create", "--db", self.db, "--config", cfg_path)
        self.assertEqual(r.returncode, 2)

        # 业务冲突退出 2 且台账不被污染（generation 不变）
        bad = self._write("bad.json", {
            "request_id": "req-x", "expected_generation": 99, "readings": [
                reading("f1", self.ds[0], 1, 5)]})
        r = self._run("apply", "--db", self.db, "--source", bad)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(ml.report(self.db)["generation"], 2)

    def test_cli_rejects_duplicate_keys_and_nan(self):
        self._write("config.json", self.cfg)
        self._run("create", "--db", self.db,
                  "--config", str(Path(self.folder) / "config.json"))
        dup = self._write("dup.json", '{"request_id": "x", "request_id": "y"}')
        r = self._run("apply", "--db", self.db, "--source", dup)
        self.assertEqual(r.returncode, 2)
        self.assertIn("duplicate JSON key", r.stderr)
        nan = self._write("nan.json",
                          '{"request_id": "x", "expected_generation": 0, "readings": NaN}')
        r = self._run("apply", "--db", self.db, "--source", nan)
        self.assertEqual(r.returncode, 2)
        self.assertIn("non-finite", r.stderr)
        # 非对象根
        root = self._write("root.json", "[]")
        r = self._run("apply", "--db", self.db, "--source", root)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
