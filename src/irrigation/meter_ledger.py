"""实际用水计量台账与结算更正（SQLite 持久化）。

本模块只处理已经发生的用水：依据各田块逻辑水表的累计读数版本核算实际毛供水、
真实田间水账与财务结算，并在历史读数更正时以可追溯的正负差额追加 adjustment，
绝不改写已经发出的结算单。模块不选择未来供水、不操作任何设备。

持久化完全落在本地 SQLite 文件中：进程结束后重开同一个文件可以继续工作，
两个录入进程借助 ``BEGIN IMMEDIATE`` 写事务串行化，每个 apply/close 请求都是
一个原子业务提交，generation 在整个台账生命周期内单调递增、绝不重用。

公开接口：

- ``create(db_path, config)``：初始化新台账（只创建不存在的数据库）。
- ``apply(db_path, request)``：录入/重放读数版本，返回完整回执。
- ``close(db_path, request)``：区间结算，生成不可变结算单。
- ``report(db_path)``：只读一致快照报告。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date as _date
from datetime import timedelta
from fractions import Fraction

from .balance import step
from .model import day as _day
from .model import validate as _validate_base

SCHEMA_VERSION = 1
# 固定的应用标记，用于识别本台账数据库，拒绝把其他应用的文件当作台账打开。
APP_ID = 0x4D4C4531  # 'MLE1'
# 默认写锁等待 30 秒；测试可 monkeypatch 该常量缩短等待。
TIMEOUT_MS = 30_000

# 测试用提交中断点：环境变量 IRRIGATION_LEDGER_CRASH_POINT=before_commit 时，
# 写事务在 COMMIT 前立即以 os._exit 中止，模拟进程崩溃（事务随连接关闭回滚）。
_CRASH_POINT = os.environ.get("IRRIGATION_LEDGER_CRASH_POINT", "")

_TABLES = (
    "meta", "config_json", "requests", "reading_versions", "current_readings",
    "statements", "statement_lines", "adjustments",
)


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------

def _is_int(value):
    """整数口径与旧模块一致：bool 不是整数。"""
    return type(value) is int


def _int(value, label, minimum=0):
    if not _is_int(value) or value < minimum:
        raise ValueError(f"{label} 必须是 >= {minimum} 的整数")
    return value


def _text(value, label):
    # 非空字符串，保留原值：不做 strip、大小写或 Unicode 归并。
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def _fingerprint(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validate_config(config):
    """校验并结构化配置；不修改输入对象。"""
    _object(config, "config")
    if "applications" in config or "planning" in config:
        raise ValueError("meter_ledger 配置不包含 applications 或 planning")
    for key in ("fields", "weather", "meters"):
        if key not in config:
            raise ValueError(f"config 缺少 {key}")
    # 沿用原核算的字段、范围、连续日期和校验口径（1..200 田块、1..3660 连续气象日）。
    pseudo = {
        "fields": config["fields"],
        "weather": config["weather"],
        "applications": [],
    }
    fields, weather, _empty_apps = _validate_base(pseudo)
    meters_rows = config["meters"]
    if not isinstance(meters_rows, list) or len(meters_rows) != len(fields):
        raise ValueError("meters 必须是每个田块恰好一条的数组")
    meters = {}
    for row in meters_rows:
        _object(row, "meters 行")
        fid = _text(row.get("field_id"), "meters.field_id")
        if fid not in fields:
            raise ValueError(f"meters 引用了未知田块: {fid}")
        if fid in meters:
            raise ValueError(f"meters 中田块重复: {fid}")
        opening_litres = _int(row.get("opening_litres"), "opening_litres")
        price = _int(row.get("price_microunits_per_litre"),
                     "price_microunits_per_litre")
        meters[fid] = {
            "field_id": fid,
            "opening_litres": opening_litres,
            "price_microunits_per_litre": price,
        }
    if set(meters) != set(fields):
        raise ValueError("meters 必须覆盖全部田块且不得缺失或重复")
    return fields, weather, meters


def _validate_apply_payload(request, field_ids, date_set):
    """结构、类型、范围、引用校验；业务冲突在事务内结合已存事实判断。"""
    _object(request, "request")
    request_id = _text(request.get("request_id"), "request_id")
    expected_generation = _int(request.get("expected_generation"),
                               "expected_generation")
    readings = request.get("readings")
    if not isinstance(readings, list) or not readings:
        raise ValueError("readings 必须是非空数组")
    normalized = []
    seen = set()
    for row in readings:
        _object(row, "readings 行")
        if "reset_opening_litres" not in row:
            raise ValueError("readings 每行必须包含 reset_opening_litres（可为 null）")
        fid = _text(row.get("field_id"), "field_id")
        if fid not in field_ids:
            raise ValueError(f"读数引用了未知田块: {fid}")
        when = _day(_text(row.get("date"), "date"))
        if when not in date_set:
            raise ValueError(f"读数日期超出气象窗口: {row.get('date')}")
        key = (fid, when)
        if key in seen:
            raise ValueError(f"同一请求内 (field_id,date) 重复: {fid} {when}")
        seen.add(key)
        revision = _int(row.get("revision"), "revision", minimum=1)
        closing = _int(row.get("closing_litres"), "closing_litres")
        reset = row.get("reset_opening_litres")
        if reset is not None:
            reset = _int(reset, "reset_opening_litres")
        normalized.append({
            "field_id": fid,
            "date": when.isoformat(),
            "revision": revision,
            "closing_litres": closing,
            "reset_opening_litres": reset,
        })
    return {
        "request_id": request_id,
        "expected_generation": expected_generation,
        "readings": normalized,
    }


def _validate_close_payload(request, date_set):
    _object(request, "request")
    request_id = _text(request.get("request_id"), "request_id")
    expected_generation = _int(request.get("expected_generation"),
                               "expected_generation")
    through = _day(_text(request.get("through_date"), "through_date"))
    if through not in date_set:
        raise ValueError("through_date 超出气象窗口")
    return {
        "request_id": request_id,
        "expected_generation": expected_generation,
        "through_date": through.isoformat(),
    }


# ---------------------------------------------------------------------------
# 连接与数据库打开
# ---------------------------------------------------------------------------

def _connect(path):
    # isolation_level=None：完全由本模块显式 BEGIN/COMMIT/ROLLBACK 管理事务，
    # 避免 sqlite3 模块在 DML 前自动开启事务造成嵌套。
    conn = sqlite3.connect(str(path), timeout=TIMEOUT_MS / 1000,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_ledger(path):
    if not os.path.exists(path):
        raise OSError(f"台账数据库不存在: {path}")
    conn = _connect(path)
    try:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        if app_id != APP_ID:
            raise ValueError(
                "数据库不是 irrigation.meter_ledger 台账（application_id 不匹配），"
                "拒绝自动重建"
            )
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的台账 schema 版本: {user_version}，本软件支持 {SCHEMA_VERSION}"
            )
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        missing = [name for name in _TABLES if name not in names]
        if missing:
            raise ValueError(f"台账数据库结构不完整，缺少表: {', '.join(missing)}")
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise ValueError(f"无法识别的数据库文件: {path} ({exc})") from exc
    except Exception:
        conn.close()
        raise
    return conn


def _load_config(conn):
    payload = conn.execute("SELECT payload FROM config_json WHERE id=1").fetchone()[0]
    config = json.loads(payload)
    fields, weather, meters = _validate_config(config)
    dates = [_day(row["date"]) for row in weather]
    return config, fields, weather, meters, dates


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE config_json (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    generation INTEGER NOT NULL,
    receipt TEXT NOT NULL
);
CREATE TABLE reading_versions (
    field_id TEXT NOT NULL,
    date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    closing_litres INTEGER NOT NULL,
    reset_opening_litres INTEGER,
    PRIMARY KEY (field_id, date, revision)
);
CREATE TABLE current_readings (
    field_id TEXT NOT NULL,
    date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (field_id, date),
    FOREIGN KEY (field_id, date, revision)
        REFERENCES reading_versions (field_id, date, revision)
);
CREATE TABLE statements (
    statement_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL UNIQUE,
    from_date TEXT NOT NULL,
    through_date TEXT NOT NULL,
    total_amount_microunits INTEGER NOT NULL
);
CREATE TABLE statement_lines (
    statement_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    date TEXT NOT NULL,
    gross_litres INTEGER NOT NULL,
    price_microunits_per_litre INTEGER NOT NULL,
    amount_microunits INTEGER NOT NULL,
    PRIMARY KEY (statement_id, field_id, date),
    FOREIGN KEY (statement_id) REFERENCES statements (statement_id)
);
CREATE TABLE adjustments (
    adjustment_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    statement_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    date TEXT NOT NULL,
    previous_gross_litres INTEGER NOT NULL,
    current_gross_litres INTEGER NOT NULL,
    delta_litres INTEGER NOT NULL,
    delta_amount_microunits INTEGER NOT NULL,
    FOREIGN KEY (statement_id) REFERENCES statements (statement_id)
);
"""


def create(db_path, config):
    """初始化新台账。只创建不存在的数据库；失败不留下可正常打开的半初始化台账。"""
    _validate_config(config)  # 配置无效时在触碰文件之前失败
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(db_path), flags, 0o644)
    except FileExistsError as exc:
        raise OSError(f"台账路径已存在，拒绝覆盖: {db_path}") from exc
    os.close(fd)
    conn = _connect(db_path)
    try:
        # WAL 设置本身会触发一次提交，必须在业务事务之前完成。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 逐条执行 DDL：executescript 会隐式提交，破坏原子性。
            for ddl in (
                _stmt.strip() for _stmt in _SCHEMA_SQL.split(";")
                if _stmt.strip()
            ):
                conn.execute(ddl)
            conn.execute(f"PRAGMA application_id={APP_ID}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('generation', '0')"
            )
            conn.execute(
                "INSERT INTO config_json(id, payload) VALUES(1, ?)",
                (_canonical(config),),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        _commit(conn)
    except BaseException:
        conn.close()
        # 仅删除本调用刚刚独占创建的文件；绝不可能覆盖任何既有数据库。
        try:
            os.unlink(str(db_path))
        except OSError:
            pass
        raise
    conn.close()
    return {"schema_version": SCHEMA_VERSION, "generation": 0}


# ---------------------------------------------------------------------------
# 核心核算：读数状态 -> 毛供水 -> 田间水账
# ---------------------------------------------------------------------------

def _load_versions(conn):
    """返回 (全部版本 dict, 当前版本 dict)。

    版本 dict: (fid, iso_date) -> {revision: (closing, reset)}
    当前 dict: (fid, iso_date) -> (revision, closing, reset)
    """
    versions = {}
    for row in conn.execute(
        "SELECT field_id, date, revision, closing_litres, reset_opening_litres "
        "FROM reading_versions"
    ):
        versions.setdefault((row["field_id"], row["date"]), {})[row["revision"]] = (
            row["closing_litres"], row["reset_opening_litres"],
        )
    current = {}
    for row in conn.execute(
        "SELECT field_id, date, revision FROM current_readings"
    ):
        closing, reset = versions[(row["field_id"], row["date"])][row["revision"]]
        current[(row["field_id"], row["date"])] = (
            row["revision"], closing, reset,
        )
    return versions, current


def _compute_gross(field_ids, dates, meters, current):
    """按当前读数版本计算每个田块日的毛供水升数；未知为 None。

    规则：
    - 换表日（reset 非 null）：升数 = closing - reset_opening，独立成立。
    - 普通日：升数 = 当日 closing - 前一气象日当前 closing；首日前值取配置
      opening_litres。
    - 没有读数、或普通日缺前一日读数时未知（None），不能记零或均摊。
    - 任何可算差分必须非负，否则抛 ValueError（整批失败）。
    """
    gross = {}
    revision_by_day = {}
    for fid in field_ids:
        prev_closing = meters[fid]["opening_litres"]
        for when in dates:
            key = (fid, when.isoformat())
            cur = current.get(key)
            if cur is None:
                gross[key] = None
                revision_by_day[key] = None
                prev_closing = None
                continue
            revision, closing, reset = cur
            revision_by_day[key] = revision
            if reset is not None:
                litres = closing - reset
            elif prev_closing is None:
                litres = None
            else:
                litres = closing - prev_closing
            if litres is not None and litres < 0:
                raise ValueError(
                    f"读数差分为负（{fid} {when.isoformat()}：{litres} 升），"
                    "整批读数被拒绝"
                )
            gross[key] = litres
            # 即使当日毛供水因缺前日读数而未知，当日 closing 仍是次日差分前值。
            prev_closing = closing
    return gross, revision_by_day


def _compute_balances(field_ids, dates, fields, weather, gross):
    """从配置 opening_mm 起按 balance.step 顺序重算真实田间水账。

    一个日期之前若有未知毛供水，该田块从缺口当天起全部待补（None），
    换表不重置土壤储水。返回 {(fid, iso_date): step 结果 dict 或 None}。
    """
    weather_by_date = {row["date"]: row for row in weather}
    balances = {}
    for fid in field_ids:
        field = fields[fid]
        opening = Fraction(field["opening_mm"])
        pending = False
        for when in dates:
            iso = when.isoformat()
            key = (fid, iso)
            litres = gross[key]
            if litres is None:
                pending = True
            if pending:
                balances[key] = None
                continue
            observed = weather_by_date[iso]
            gross_mm = Fraction(litres, field["area_m2"])
            result = step(
                field, opening,
                Fraction(observed["rain_mm"]), gross_mm,
                Fraction(observed["et_mm"]),
            )
            balances[key] = result
            opening = result["closing_mm"]
    return balances


# ---------------------------------------------------------------------------
# 事务与请求去重
# ---------------------------------------------------------------------------

def _commit(conn):
    if _CRASH_POINT == "before_commit":
        # 模拟进程在提交中断点崩溃：关闭连接会回滚未提交事务，随后立即退出。
        conn.close()
        os._exit(9)
    conn.execute("COMMIT")


def _get_generation(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])


def _bump_generation(conn, new_generation):
    conn.execute(
        "UPDATE meta SET value=? WHERE key='generation'", (str(new_generation),)
    )


def _stored_request(conn, request_id):
    return conn.execute(
        "SELECT operation, content_hash, receipt FROM requests WHERE request_id=?",
        (request_id,),
    ).fetchone()


def _apply_fingerprint(request):
    # readings 数组顺序不影响业务内容；expected_generation 仍属于内容。
    readings = sorted(
        (dict(row) for row in request["readings"]),
        key=lambda row: (row["field_id"], row["date"]),
    )
    normalized = {
        "request_id": request["request_id"],
        "expected_generation": request["expected_generation"],
        "readings": readings,
    }
    return _fingerprint(normalized)


def _close_fingerprint(request):
    return _fingerprint({
        "request_id": request["request_id"],
        "expected_generation": request["expected_generation"],
        "through_date": request["through_date"],
    })


def _replay_or_fail(stored, operation, content_hash):
    """request_id 判断优先于 generation 冲突判断。"""
    if stored is None:
        return None
    if stored["operation"] != operation or stored["content_hash"] != content_hash:
        raise ValueError(
            "request_id 已被不同操作或不同业务内容占用，拒绝复用"
        )
    return json.loads(stored["receipt"])


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply(db_path, request):
    """录入一个读数批次（原子提交），返回 schema_version=1 的完整回执。"""
    conn = _open_ledger(db_path)
    try:
        _config, fields, weather, meters, dates = _load_config(conn)
        field_ids = sorted(fields)
        date_set = set(dates)
        payload = _validate_apply_payload(request, field_ids, date_set)
        content_hash = _apply_fingerprint(payload)
        request_id = payload["request_id"]

        conn.execute("BEGIN IMMEDIATE")
        stored = _stored_request(conn, request_id)
        replay = _replay_or_fail(stored, "apply", content_hash)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        generation = _get_generation(conn)
        if payload["expected_generation"] != generation:
            raise ValueError(
                f"generation 冲突：expected_generation={payload['expected_generation']}，"
                f"当前 generation={generation}"
            )

        versions, current = _load_versions(conn)

        # 1) 同身份同 revision 内容不同 -> 整批冲突；完全相同则是证据重放。
        incoming = {}
        for row in payload["readings"]:
            key = (row["field_id"], row["date"])
            identity = key + (row["revision"],)
            incoming[identity] = (row["closing_litres"], row["reset_opening_litres"])
            existing = versions.get(key, {}).get(row["revision"])
            if existing is not None and existing != incoming[identity]:
                raise ValueError(
                    f"读数版本冲突：{row['field_id']} {row['date']} "
                    f"revision={row['revision']} 已存内容不同，整批回滚"
                )

        # 2) 整体纳入候选状态后再判断，避免数组顺序造成临时负用水误拒。
        candidate_versions = {
            key: dict(revs) for key, revs in versions.items()
        }
        for identity, content in incoming.items():
            fid, iso, revision = identity
            candidate_versions.setdefault((fid, iso), {})[revision] = content
        candidate_current = {}
        for key, revs in candidate_versions.items():
            top_revision = max(revs)
            closing, reset = revs[top_revision]
            candidate_current[key] = (top_revision, closing, reset)

        # 3) 候选状态差分核算（含负值整批拒绝）。
        new_gross, _new_revisions = _compute_gross(
            field_ids, dates, meters, candidate_current
        )
        old_gross, _old_revisions = _compute_gross(
            field_ids, dates, meters, current
        )

        # 4) 本次首次归档的版本、确实切换当前版本的田块日。
        accepted_versions = sorted(
            (identity for identity in incoming
             if identity[2] not in versions.get(identity[:2], {})),
            key=lambda identity: (identity[0], identity[1], identity[2]),
        )
        current_changes = []
        for identity in accepted_versions:
            fid, iso, revision = identity
            incumbent = current.get((fid, iso))
            if incumbent is None or revision > incumbent[0]:
                current_changes.append(identity)
        current_changes.sort()

        # 5) 已结算田块日的财务差额（基于财务已确认值，而非最初账单）。
        settled = _settled_lines(conn)
        adjustments = []
        seq = 0
        new_generation = generation + 1
        for (fid, iso), current_litres in sorted(new_gross.items(),
                                                 key=lambda item: (item[0][1], item[0][0])):
            line_info = settled.get((fid, iso))
            if line_info is None:
                continue  # 未结算日期不生成差额
            if old_gross[(fid, iso)] == current_litres:
                continue  # 零差额不生成记录
            recognized = line_info["line_litres"] + line_info["adj_litres"]
            delta_litres = current_litres - recognized
            if delta_litres == 0:
                continue
            price = meters[fid]["price_microunits_per_litre"]
            seq += 1
            adjustment_id = f"adj-{new_generation:010d}-{seq:04d}"
            adjustments.append({
                "adjustment_id": adjustment_id,
                "request_id": request_id,
                "generation": new_generation,
                "statement_id": line_info["statement_id"],
                "field_id": fid,
                "date": iso,
                "previous_gross_litres": recognized,
                "current_gross_litres": current_litres,
                "delta_litres": delta_litres,
                "delta_amount_microunits": delta_litres * price,
            })

        # 6) 原子写入：新版本、当前版本、差额、请求回执与 generation 同生共死。
        for identity, (closing, reset) in incoming.items():
            fid, iso, revision = identity
            conn.execute(
                "INSERT OR IGNORE INTO reading_versions"
                "(field_id, date, revision, closing_litres, reset_opening_litres) "
                "VALUES(?, ?, ?, ?, ?)",
                (fid, iso, revision, closing, reset),
            )
        for fid, iso, revision in current_changes:
            conn.execute(
                "INSERT INTO current_readings(field_id, date, revision) VALUES(?, ?, ?) "
                "ON CONFLICT(field_id, date) DO UPDATE SET revision=excluded.revision",
                (fid, iso, revision),
            )
        for adj in adjustments:
            conn.execute(
                "INSERT INTO adjustments"
                "(adjustment_id, request_id, generation, statement_id, field_id, date, "
                "previous_gross_litres, current_gross_litres, delta_litres, "
                "delta_amount_microunits) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (adj["adjustment_id"], adj["request_id"], adj["generation"],
                 adj["statement_id"], adj["field_id"], adj["date"],
                 adj["previous_gross_litres"], adj["current_gross_litres"],
                 adj["delta_litres"], adj["delta_amount_microunits"]),
            )

        receipt = {
            "schema_version": SCHEMA_VERSION,
            "operation": "apply",
            "request_id": request_id,
            "generation": new_generation,
            "accepted_versions": [
                {"field_id": fid, "date": iso, "revision": revision}
                for fid, iso, revision in accepted_versions
            ],
            "current_changes": [
                {"field_id": fid, "date": iso, "revision": revision}
                for fid, iso, revision in current_changes
            ],
            "adjustments": adjustments,
        }
        conn.execute(
            "INSERT INTO requests(request_id, operation, content_hash, generation, receipt) "
            "VALUES(?, 'apply', ?, ?, ?)",
            (request_id, content_hash, new_generation, _canonical(receipt)),
        )
        _bump_generation(conn, new_generation)
        _commit(conn)
        return receipt
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _settled_lines(conn):
    """返回已结算田块日的财务确认状态。

    {(fid, iso): {statement_id, line_litres, adj_litres}}
    adj_litres 为该日此前全部 adjustment 的升数累计。
    """
    settled = {}
    for row in conn.execute(
        "SELECT statement_id, field_id, date, gross_litres FROM statement_lines"
    ):
        settled[(row["field_id"], row["date"])] = {
            "statement_id": row["statement_id"],
            "line_litres": row["gross_litres"],
            "adj_litres": 0,
        }
    for row in conn.execute(
        "SELECT field_id, date, delta_litres FROM adjustments"
    ):
        settled[(row["field_id"], row["date"])]["adj_litres"] += row["delta_litres"]
    return settled


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def close(db_path, request):  # noqa: A001 - 契约要求的接口名
    """对一个严格前进的连续区间结算，生成不可变结算单。"""
    conn = _open_ledger(db_path)
    try:
        _config, fields, weather, meters, dates = _load_config(conn)
        field_ids = sorted(fields)
        date_set = set(dates)
        payload = _validate_close_payload(request, date_set)
        content_hash = _close_fingerprint(payload)
        request_id = payload["request_id"]
        through_d = _date.fromisoformat(payload["through_date"])

        conn.execute("BEGIN IMMEDIATE")
        stored = _stored_request(conn, request_id)
        replay = _replay_or_fail(stored, "close", content_hash)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        generation = _get_generation(conn)
        if payload["expected_generation"] != generation:
            raise ValueError(
                f"generation 冲突：expected_generation={payload['expected_generation']}，"
                f"当前 generation={generation}"
            )

        last_row = conn.execute(
            "SELECT MAX(through_date) AS last_through FROM statements"
        ).fetchone()
        last_through = last_row["last_through"]
        if last_through is None:
            from_d = dates[0]
        else:
            last_d = _date.fromisoformat(last_through)
            if through_d <= last_d:
                raise ValueError(
                    "结算必须严格前进：through_date 晚于上次结算末日"
                )
            from_d = last_d + timedelta(days=1)
        if through_d > dates[-1]:
            raise ValueError("结算区间不能超出气象窗口")
        interval = [d for d in dates if from_d <= d <= through_d]
        if (from_d.isoformat() != interval[0].isoformat()
                or (through_d - from_d).days + 1 != len(interval)):
            raise ValueError("结算区间不允许跳过中间日期")

        versions, current = _load_versions(conn)
        gross, _revisions = _compute_gross(field_ids, dates, meters, current)
        lines = []
        for when in interval:
            for fid in field_ids:
                litres = gross[(fid, when.isoformat())]
                if litres is None:
                    raise ValueError(
                        f"结算区间存在未知毛供水（{fid} {when.isoformat()}），"
                        "整次结算失败"
                    )
                price = meters[fid]["price_microunits_per_litre"]
                lines.append({
                    "field_id": fid,
                    "date": when.isoformat(),
                    "gross_litres": litres,
                    "price_microunits_per_litre": price,
                    "amount_microunits": litres * price,
                })

        new_generation = generation + 1
        statement_id = f"stmt-{new_generation:010d}"
        total = sum(line["amount_microunits"] for line in lines)
        statement = {
            "statement_id": statement_id,
            "generation": new_generation,
            "from_date": from_d.isoformat(),
            "through_date": through_d.isoformat(),
            "lines": lines,
            "total_amount_microunits": total,
        }
        conn.execute(
            "INSERT INTO statements(statement_id, generation, from_date, through_date, "
            "total_amount_microunits) VALUES(?, ?, ?, ?, ?)",
            (statement_id, new_generation, from_d.isoformat(),
             through_d.isoformat(), total),
        )
        for line in lines:
            conn.execute(
                "INSERT INTO statement_lines(statement_id, field_id, date, gross_litres, "
                "price_microunits_per_litre, amount_microunits) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (statement_id, line["field_id"], line["date"], line["gross_litres"],
                 line["price_microunits_per_litre"], line["amount_microunits"]),
            )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "operation": "close",
            "request_id": request_id,
            "generation": new_generation,
            "statement": statement,
        }
        conn.execute(
            "INSERT INTO requests(request_id, operation, content_hash, generation, receipt) "
            "VALUES(?, 'close', ?, ?, ?)",
            (request_id, content_hash, new_generation, _canonical(receipt)),
        )
        _bump_generation(conn, new_generation)
        _commit(conn)
        return receipt
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# report（只读一致快照）
# ---------------------------------------------------------------------------

def _balance_row(fid, iso, result):
    row = {"field_id": fid, "date": iso}
    for key, value in result.items():
        if key == "advised_next_gross_mm":
            row[key] = value  # 仍是整数
        else:
            row[key] = str(value)  # 其余 *_mm 统一 Fraction 规范字符串
    return row


def report(db_path):
    """返回台账当前 generation 的完整一致快照，不改变任何业务记录。"""
    conn = _open_ledger(db_path)
    try:
        conn.execute("BEGIN")
        _config, fields, weather, meters, dates = _load_config(conn)
        snapshot_generation = _get_generation(conn)
        field_ids = sorted(fields)
        versions, current = _load_versions(conn)
        gross, revisions = _compute_gross(field_ids, dates, meters, current)
        balances = _compute_balances(field_ids, dates, fields, weather, gross)

        daily = []
        for fid in field_ids:
            for when in dates:
                iso = when.isoformat()
                key = (fid, iso)
                litres = gross[key]
                water_balance = balances[key]
                daily.append({
                    "field_id": fid,
                    "date": iso,
                    "current_revision": revisions[key],
                    "meter_status": "ready" if litres is not None else "pending",
                    "gross_litres": litres,
                    "gross_mm": (
                        str(Fraction(litres, fields[fid]["area_m2"]))
                        if litres is not None else None
                    ),
                    "balance_status": "ready" if water_balance is not None else "pending",
                    "water_balance": (
                        _balance_row(fid, iso, water_balance)
                        if water_balance is not None else None
                    ),
                })

        statements = []
        for srow in conn.execute(
            "SELECT statement_id, generation, from_date, through_date, "
            "total_amount_microunits FROM statements ORDER BY generation"
        ):
            lines = []
            for lrow in conn.execute(
                "SELECT field_id, date, gross_litres, price_microunits_per_litre, "
                "amount_microunits FROM statement_lines WHERE statement_id=? "
                "ORDER BY date, field_id",
                (srow["statement_id"],),
            ):
                lines.append({
                    "field_id": lrow["field_id"],
                    "date": lrow["date"],
                    "gross_litres": lrow["gross_litres"],
                    "price_microunits_per_litre": lrow["price_microunits_per_litre"],
                    "amount_microunits": lrow["amount_microunits"],
                })
            statements.append({
                "statement_id": srow["statement_id"],
                "generation": srow["generation"],
                "from_date": srow["from_date"],
                "through_date": srow["through_date"],
                "lines": lines,
                "total_amount_microunits": srow["total_amount_microunits"],
            })

        adjustments = []
        for arow in conn.execute(
            "SELECT adjustment_id, request_id, generation, statement_id, field_id, date, "
            "previous_gross_litres, current_gross_litres, delta_litres, "
            "delta_amount_microunits FROM adjustments "
            "ORDER BY generation, date, field_id"
        ):
            adjustments.append({key: arow[key] for key in arow.keys()})

        original_amount = sum(s["total_amount_microunits"] for s in statements)
        adjustment_amount = sum(a["delta_amount_microunits"] for a in adjustments)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "generation": snapshot_generation,
        "daily": daily,
        "statements": statements,
        "adjustments": adjustments,
        "financial_summary": {
            "original_amount_microunits": original_amount,
            "adjustment_amount_microunits": adjustment_amount,
            "recognized_amount_microunits": original_amount + adjustment_amount,
        },
    }
