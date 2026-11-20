"""田块拆并接续与用水来源审计（日尺度均匀混合归因）。

合作社在季中拆分或合并田块：旧田块退出登记后，土壤里的水留在原地，新田块按
接管土地接续其期末储水与全部来源份额。本模块在完全保留现有日水账口径
（``irrigation.balance.step`` 的输水效率、入渗上限、径流、容量渗漏、实际 ET、
缺水与建议顺序）的前提下，逐笔追踪每一股历史来水（首日土壤存量、每日降雨、
每日毛供水）最终流向哪些终点（输水损耗、径流、渗漏、实际 ET、末日结存）。

公开接口 ``run(data)`` 与命令 ``soil-water-land`` 共用同一业务逻辑，仅用
Python >=3.11 标准库。全部水量用 ``fractions.Fraction`` 精确有理数计算，不使用
浮点或容差；输入对象不会被修改；零水量混合不做除零，也不产生非零来源贡献。
"""
from __future__ import annotations

from fractions import Fraction

from .balance import step
from .io import integer, text
from .model import day as parse_day
from .model import validate as _validate_base

ROOT_KEYS = {
    "fields", "weather", "applications", "repartitions",
    "audit_sources", "audit_sinks",
}
FIELD_KEYS = {
    "id", "area_m2", "capacity_mm", "opening_mm", "threshold_mm",
    "target_mm", "infiltration_mm", "efficiency_permille",
}
NEW_FIELD_KEYS = FIELD_KEYS - {"opening_mm"}
MOVE_KEYS = {"from_field_id", "to_field_id", "area_m2"}
BATCH_KEYS = {"date", "new_fields", "moves"}
IDENTITY_KEYS = {"date", "field_id", "kind"}

SOURCE_KINDS = {"initial", "rain", "irrigation"}
SINK_KINDS = {"delivery_loss", "runoff", "drainage", "et", "storage"}

MAX_DAYS = 60
MAX_ACTIVE_FIELDS = 200
MAX_TOTAL_FIELDS = 1000
MAX_MOVES = 3000
MAX_AUDIT_ROWS = 12

ZERO = Fraction(0)


# ---------------------------------------------------------------------------
# 通用结构校验
# ---------------------------------------------------------------------------

def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组")
    return value


def _exact_keys(obj, keys, label):
    actual = set(obj)
    missing = keys - actual
    if missing:
        raise ValueError(f"{label} 缺少必填字段: {sorted(missing)}")
    extra = actual - keys
    if extra:
        raise ValueError(f"{label} 包含未声明字段: {sorted(extra)}")


def _identity(row, label, allowed_kinds):
    _object(row, label)
    _exact_keys(row, IDENTITY_KEYS, label)
    when = parse_day(text(row["date"], f"{label}.date"))
    fid = text(row["field_id"], f"{label}.field_id")
    kind = text(row["kind"], f"{label}.kind")
    if kind not in allowed_kinds:
        raise ValueError(f"{label}.kind 非法: {kind}")
    return when, fid, kind


def _validate_field_params(row, label, known_ids, allow_opening):
    """沿用 model.validate 对田块参数的同一套整数与限值口径。"""
    _object(row, label)
    keys = FIELD_KEYS if allow_opening else NEW_FIELD_KEYS
    _exact_keys(row, keys, label)
    fid = text(row["id"], f"{label}.id")
    if fid in known_ids:
        raise ValueError(f"田块 id 在整个窗口内不得复用: {fid}")
    area = integer(row["area_m2"], "area_m2", 1)
    capacity = integer(row["capacity_mm"], "capacity_mm", 1)
    threshold = integer(row["threshold_mm"], "threshold_mm")
    target = integer(row["target_mm"], "target_mm")
    infiltration = integer(row["infiltration_mm"], "infiltration_mm", 1)
    efficiency = integer(row["efficiency_permille"], "efficiency_permille", 1)
    if not 0 <= threshold <= target <= capacity or efficiency > 1000:
        raise ValueError("invalid storage limits or efficiency")
    opening = None
    if allow_opening:
        opening = integer(row["opening_mm"], "opening_mm")
        if opening > capacity:
            raise ValueError("invalid storage limits or efficiency")
    return fid, {
        "id": fid,
        "area_m2": area,
        "capacity_mm": capacity,
        "opening_mm": opening if opening is not None else None,
        "threshold_mm": threshold,
        "target_mm": target,
        "infiltration_mm": infiltration,
        "efficiency_permille": efficiency,
    }


def _validate_applications(rows, dates):
    applications = {}
    for row in _array(rows, "applications"):
        _object(row, "applications 行")
        # 沿用旧核算口径：只读取三个共有字段（不新增未声明字段拒绝规则）。
        fid = text(row["field_id"], "applications.field_id")
        when = parse_day(text(row["date"], "applications.date"))
        if when not in dates:
            raise ValueError(f"application 日期超出气象窗口: {row['date']}")
        gross = integer(row["gross_mm"], "gross_mm")
        key = (fid, when)
        if key in applications:
            raise ValueError("duplicate field/day application")
        applications[key] = Fraction(gross)
    return applications


def _validate_repartitions(rows, dates, params, all_ids):
    """结构校验（不依赖逐日在册状态的部分）；面积守恒在模拟时复核。"""
    first_day, last_day = dates[0], dates[-1]
    batches = {}
    total_new = 0
    total_moves = 0
    for row in _array(rows, "repartitions"):
        _object(row, "repartitions 批")
        _exact_keys(row, BATCH_KEYS, "repartitions 批")
        when = parse_day(text(row["date"], "repartitions.date"))
        if when not in dates or when == first_day:
            raise ValueError("repartitions 不能在首日或气象窗口外生效")
        if when in batches:
            raise ValueError(f"每个生效日最多一批 repartition: {row['date']}")
        new_rows = _array(row["new_fields"], "new_fields")
        move_rows = _array(row["moves"], "moves")
        if not new_rows or not move_rows:
            raise ValueError("repartitions 的 new_fields 与 moves 均必须非空")

        new_fields = []
        batch_new_ids = set()
        for new_row in new_rows:
            fid, parsed = _validate_field_params(
                new_row, "new_fields 行", all_ids, allow_opening=False)
            all_ids.add(fid)
            batch_new_ids.add(fid)
            new_fields.append(parsed)
            total_new += 1

        moves = []
        seen_pairs = set()
        for move_row in move_rows:
            _object(move_row, "moves 行")
            _exact_keys(move_row, MOVE_KEYS, "moves 行")
            src = text(move_row["from_field_id"], "moves.from_field_id")
            dst = text(move_row["to_field_id"], "moves.to_field_id")
            area_m2 = integer(move_row["area_m2"], "area_m2", 1)
            if dst not in batch_new_ids:
                raise ValueError(
                    f"moves 转入方必须是本批新田块: {dst}")
            if src in batch_new_ids:
                raise ValueError("本批新田块不能在同一批再次转出")
            pair = (src, dst)
            if pair in seen_pairs:
                raise ValueError(f"同一批每个有向田块对最多一条 moves: {pair}")
            seen_pairs.add(pair)
            moves.append({"from_field_id": src, "to_field_id": dst,
                          "area_m2": area_m2})
            total_moves += 1
        batches[when] = {"date": when, "new_fields": new_fields, "moves": moves}

    if len(params) + total_new > MAX_TOTAL_FIELDS:
        raise ValueError(
            f"窗口内期初与新增田块合计不得超过 {MAX_TOTAL_FIELDS} 个")
    if total_moves > MAX_MOVES:
        raise ValueError(f"交接明细最多 {MAX_MOVES} 条")
    return batches


def _validate_audit_rows(rows, label, allowed_kinds):
    queries = []
    seen = set()
    for row in _array(rows, label):
        when, fid, kind = _identity(row, f"{label} 行", allowed_kinds)
        key = (when, fid, kind)
        if key in seen:
            raise ValueError(f"{label} 存在重复查询身份: {key}")
        seen.add(key)
        queries.append(key)
    if len(queries) > MAX_AUDIT_ROWS:
        raise ValueError(f"{label} 最多 {MAX_AUDIT_ROWS} 条")
    return queries


# ---------------------------------------------------------------------------
# 主核算
# ---------------------------------------------------------------------------

def run(data):  # noqa: A001 - 与既有 service.run / reuse_water.run 同名公开接口
    _object(data, "输入根对象")
    _exact_keys(data, ROOT_KEYS, "输入根对象")

    # fields/weather 沿用旧水账的字段、校验与口径；applications 自行做生命周期校验。
    fields, weather, _no_apps = _validate_base({
        "fields": data["fields"],
        "weather": data["weather"],
        "applications": [],
    })
    dates = [parse_day(row["date"]) for row in weather]
    if not 1 <= len(dates) <= MAX_DAYS:
        raise ValueError(f"气象日必须为 1..{MAX_DAYS} 个严格连续升序日")
    first_day, last_day = dates[0], dates[-1]

    params = dict(fields)
    all_ids = set(params)
    applications = _validate_applications(data["applications"], set(dates))
    batches = _validate_repartitions(
        data["repartitions"], dates, params, all_ids)
    source_queries = _validate_audit_rows(
        data["audit_sources"], "audit_sources", SOURCE_KINDS)
    sink_queries = _validate_audit_rows(
        data["audit_sinks"], "audit_sinks", SINK_KINDS)

    # 田间状态：opening 为 Fraction 水深（mm）；shares 为 {来源身份: 升}，
    # 其合计恒等于 opening × area_m2。内部身份统一用 (date, field_id, kind)。
    state = {}
    source_water = {}
    for fid, field in params.items():
        opening = Fraction(field["opening_mm"])
        source_id = (first_day, fid, "initial")
        amount = opening * field["area_m2"]
        state[fid] = {
            "area": field["area_m2"],
            "opening": opening,
            "shares": {source_id: amount} if amount else {},
        }
        source_water[source_id] = amount

    sink_water = {}
    sink_contrib = {}
    totals = {key: ZERO for key in (
        "initial", "rain", "irrigation", "delivery_loss", "runoff",
        "drainage", "et", "storage")}
    totals["initial"] = sum(source_water.values(), ZERO)

    daily_rows = []
    transfer_rows = []
    active_by_day = {}
    weather_by_date = {row["date"]: row for row in weather}

    def add_sink(sink_id, amount, contributions):
        if sink_id in sink_water:
            raise ValueError("内部错误：终点身份重复")
        sink_water[sink_id] = amount
        sink_contrib[sink_id] = contributions
        check = sum(contributions.values(), ZERO)
        if check != amount:
            raise ValueError("内部错误：终点来源分解之和不等于终点水量")

    for when in dates:
        iso = when.isoformat()
        batch = batches.get(when)

        # --- 时点 0：拆并批在当日降雨与供水之前同时生效 ---
        if batch is not None:
            previous_active = set(state)
            exiting = {move["from_field_id"] for move in batch["moves"]}
            next_state = {fid: st for fid, st in state.items()
                          if fid not in exiting}
            incoming = {
                row["id"]: {"area": 0, "water": ZERO, "shares": {}}
                for row in batch["new_fields"]
            }
            out_sum = {}
            for move in batch["moves"]:
                src, dst = move["from_field_id"], move["to_field_id"]
                if src not in previous_active:
                    raise ValueError(
                        f"moves 转出方 {src} 在 {iso} 前一天不在册")
                old = state[src]
                area_m2 = move["area_m2"]
                out_sum[src] = out_sum.get(src, 0) + area_m2
                fraction = Fraction(area_m2, old["area"])
                # 旧田块上一日末储水按面积比例随土地转移（mm×m²=L）。
                water_litres = old["opening"] * area_m2
                transfer_rows.append({
                    "date": iso, "from_field_id": src,
                    "to_field_id": dst, "area_m2": area_m2,
                    "water_litres": water_litres,
                })
                bucket = incoming[dst]
                bucket["area"] += area_m2
                bucket["water"] += water_litres
                for source_id, amount in old["shares"].items():
                    part = amount * fraction
                    if part:
                        bucket["shares"][source_id] = (
                            bucket["shares"].get(source_id, ZERO) + part)
            # 有转出的田块整体退出：转出面积之和必须恰等于原面积。
            for src, moved in out_sum.items():
                if moved != state[src]["area"]:
                    raise ValueError(
                        f"田块 {src} 在 {iso} 的转出面积之和 "
                        f"{moved} 不等于原面积 {state[src]['area']}")
            # 每个新田块的转入面积之和必须恰等于自身面积。
            for row in batch["new_fields"]:
                fid = row["id"]
                bucket = incoming[fid]
                if bucket["area"] != row["area_m2"]:
                    raise ValueError(
                        f"新田块 {fid} 在 {iso} 的转入面积之和 "
                        f"{bucket['area']} 不等于自身面积 {row['area_m2']}")
                params[fid] = row
                # 汇总各转入边水量后除以接管面积得到当天期初水深；
                # 即使高过容量也不预裁，超量交给当日 balance.step 渗漏。
                next_state[fid] = {
                    "area": row["area_m2"],
                    "opening": bucket["water"] / row["area_m2"],
                    "shares": bucket["shares"],
                }
            state = next_state
            if len(state) > MAX_ACTIVE_FIELDS:
                raise ValueError(
                    f"任一时点在册田块不得超过 {MAX_ACTIVE_FIELDS} 个")

        active = set(state)
        active_by_day[when] = active
        observed = weather_by_date[iso]
        rain_mm = Fraction(observed["rain_mm"])
        et_mm = Fraction(observed["et_mm"])

        for fid in sorted(active):
            field = params[fid]
            area = field["area_m2"]
            st = state[fid]
            opening_mm = st["opening"]
            opening_shares = dict(st["shares"])
            gross_mm = applications.get((fid, when), ZERO)

            result = step(field, opening_mm, rain_mm, gross_mm, et_mm)
            efficiency = Fraction(field["efficiency_permille"], 1000)
            delivered_mm = gross_mm * efficiency
            loss_mm = result["delivery_loss_mm"]
            runoff_mm = result["runoff_mm"]
            drainage_mm = result["drainage_mm"]
            actual_et_mm = result["actual_et_mm"]
            closing_mm = result["closing_mm"]
            incoming_mm = rain_mm + delivered_mm
            infiltrated_mm = min(incoming_mm, field["infiltration_mm"])

            rain_litres = rain_mm * area
            gross_litres = gross_mm * area
            delivered_litres = delivered_mm * area
            loss_litres = loss_mm * area
            runoff_litres = runoff_mm * area
            drainage_litres = drainage_mm * area
            et_litres = actual_et_mm * area
            closing_litres = closing_mm * area
            infiltrated_litres = infiltrated_mm * area
            storage_mm = opening_mm + infiltrated_mm
            storage_litres = storage_mm * area
            available_litres = storage_litres - drainage_litres

            irrigation_source = (when, fid, "irrigation")
            rain_source = (when, fid, "rain")
            source_water[irrigation_source] = gross_litres
            source_water[rain_source] = rain_litres
            totals["irrigation"] += gross_litres
            totals["rain"] += rain_litres

            # 1) 输水损耗只来自本次毛供水。
            loss_contrib = {}
            if loss_litres:
                loss_contrib[irrigation_source] = loss_litres
            add_sink((when, fid, "delivery_loss"), loss_litres, loss_contrib)
            totals["delivery_loss"] += loss_litres

            # 2) 到田水与当日雨水先混匀，径流按比例带走，不能带走期初存量。
            runoff_contrib = {}
            infiltrated_shares = {}
            if incoming_mm > 0:
                if rain_litres:
                    rain_runoff = runoff_litres * rain_litres / (
                        rain_litres + delivered_litres)
                    if rain_runoff:
                        runoff_contrib[rain_source] = rain_runoff
                    rain_in = rain_litres - rain_runoff
                    if rain_in:
                        infiltrated_shares[rain_source] = rain_in
                if delivered_litres:
                    irr_runoff = runoff_litres * delivered_litres / (
                        rain_litres + delivered_litres)
                    if irr_runoff:
                        runoff_contrib[irrigation_source] = irr_runoff
                    irr_in = delivered_litres - irr_runoff
                    if irr_in:
                        infiltrated_shares[irrigation_source] = irr_in
            add_sink((when, fid, "runoff"), runoff_litres, runoff_contrib)
            totals["runoff"] += runoff_litres

            # 3) 入渗水与期初水混匀，再依次记录渗漏、实际 ET 与剩余土壤水。
            mixed_shares = dict(opening_shares)
            for source_id, amount in infiltrated_shares.items():
                mixed_shares[source_id] = (
                    mixed_shares.get(source_id, ZERO) + amount)

            drainage_contrib = {}
            if storage_mm > 0 and drainage_litres:
                for source_id, amount in mixed_shares.items():
                    part = drainage_litres * amount / storage_litres
                    if part:
                        drainage_contrib[source_id] = part
            add_sink((when, fid, "drainage"), drainage_litres, drainage_contrib)
            totals["drainage"] += drainage_litres

            et_contrib = {}
            closing_shares = {}
            if available_litres > 0:
                for source_id, amount in mixed_shares.items():
                    available_part = amount - drainage_contrib.get(
                        source_id, ZERO)
                    if not available_part:
                        continue
                    et_part = et_litres * available_part / available_litres
                    if et_part:
                        et_contrib[source_id] = et_part
                    closing_part = available_part - et_part
                    if closing_part:
                        closing_shares[source_id] = closing_part
            add_sink((when, fid, "et"), et_litres, et_contrib)
            totals["et"] += et_litres

            # 跨日结存（途中存量）；末日再登记为 storage 终点。
            st["opening"] = closing_mm
            st["shares"] = closing_shares
            if sum(closing_shares.values(), ZERO) != closing_litres:
                raise ValueError("内部错误：期末储水来源分解之和不等于结存")

            daily_rows.append({
                "field_id": fid, "date": iso,
                **{key: (str(value) if isinstance(value, Fraction) else value)
                   for key, value in result.items()},
            })

        # 末日仍在册田块的结存才是 storage 终点。
        if when == last_day:
            for fid in sorted(active):
                st = state[fid]
                closing_litres = st["opening"] * st["area"]
                storage_id = (when, fid, "storage")
                add_sink(storage_id, closing_litres, dict(st["shares"]))
                totals["storage"] += closing_litres

    # 应用必须只引用当日有效田块（含新田块生效后的供水）。
    for fid, when in applications:
        if fid not in active_by_day[when]:
            raise ValueError(
                f"application 引用了 {when.isoformat()} 当日无效田块: {fid}")

    # 全窗口来源守恒：每个来源落到全部终点的水量之和等于其原进水量。
    source_out = {}
    for sink_id, contributions in sink_contrib.items():
        for source_id, amount in contributions.items():
            source_out[source_id] = source_out.get(source_id, ZERO) + amount
    for source_id, amount in source_water.items():
        if source_out.get(source_id, ZERO) != amount:
            raise ValueError("内部错误：来源出流之和不等于进水量")

    _validate_audit_lifecycle(
        source_queries, active_by_day, first_day, last_day, True)
    _validate_audit_lifecycle(
        sink_queries, active_by_day, first_day, last_day, False)

    daily_rows.sort(key=lambda row: (row["date"], row["field_id"]))
    transfer_rows.sort(
        key=lambda row: (row["date"], row["from_field_id"],
                         row["to_field_id"]))

    audit_source_rows = _build_source_audit(
        source_queries, source_water, sink_contrib)
    audit_sink_rows = _build_sink_audit(
        sink_queries, sink_water, sink_contrib)

    residual = (
        totals["initial"] + totals["rain"] + totals["irrigation"]
        - totals["delivery_loss"] - totals["runoff"] - totals["drainage"]
        - totals["et"] - totals["storage"]
    )
    if residual != 0:
        raise ValueError("内部错误：系统水量残差不为 0")
    system_balance = {
        "initial_litres": totals["initial"],
        "rain_litres": totals["rain"],
        "irrigation_litres": totals["irrigation"],
        "delivery_loss_litres": totals["delivery_loss"],
        "runoff_litres": totals["runoff"],
        "drainage_litres": totals["drainage"],
        "et_litres": totals["et"],
        "storage_litres": totals["storage"],
        "residual_litres": residual,
    }

    return {
        "schema_version": 1,
        "daily": daily_rows,
        "land_transfers": [
            {"date": row["date"],
             "from_field_id": row["from_field_id"],
             "to_field_id": row["to_field_id"],
             "area_m2": row["area_m2"],
             "water_litres": str(row["water_litres"])}
            for row in transfer_rows
        ],
        "audit_sources": audit_source_rows,
        "audit_sinks": audit_sink_rows,
        "system_balance": {key: str(value)
                           for key, value in system_balance.items()},
    }


# ---------------------------------------------------------------------------
# 审计身份生命周期校验与报告拼装
# ---------------------------------------------------------------------------

def _validate_audit_lifecycle(queries, active_by_day, first_day, last_day,
                              is_source):
    for when, fid, kind in queries:
        if kind == "initial":
            if when != first_day or fid not in active_by_day[first_day]:
                raise ValueError(
                    f"audit 身份非法：initial 只允许首日在册田块: "
                    f"{(when.isoformat(), fid, kind)}")
            continue
        if kind == "storage":
            if when != last_day or fid not in active_by_day[last_day]:
                raise ValueError(
                    f"audit 身份非法：storage 只允许末日在册田块: "
                    f"{(when.isoformat(), fid, kind)}")
            continue
        if fid not in active_by_day[when]:
            raise ValueError(
                f"audit 身份引用了失效田块日: "
                f"{(when.isoformat(), fid, kind)}")
        if is_source and kind not in ("rain", "irrigation"):
            raise ValueError(f"audit 来源 kind 非法: {kind}")
        if not is_source and kind not in (
                "delivery_loss", "runoff", "drainage", "et"):
            raise ValueError(f"audit 终点 kind 非法: {kind}")


def _identity_obj(identity):
    when, fid, kind = identity
    return {"date": when.isoformat(), "field_id": fid, "kind": kind}


def _build_source_audit(queries, source_water, sink_contrib):
    rows = []
    for identity in sorted(queries):
        details = []
        for sink_id, contributions in sink_contrib.items():
            amount = contributions.get(identity, ZERO)
            if amount:  # 只列非零贡献
                details.append((sink_id, amount))
        details.sort(key=lambda item: item[0])
        rows.append({
            "identity": _identity_obj(identity),
            "water_litres": str(source_water.get(identity, ZERO)),
            "sinks": [
                {"identity": _identity_obj(sink_id),
                 "water_litres": str(amount)}
                for sink_id, amount in details
            ],
        })
    return rows


def _build_sink_audit(queries, sink_water, sink_contrib):
    rows = []
    for identity in sorted(queries):
        contributions = sink_contrib.get(identity, {})
        details = sorted(
            ((source_id, amount) for source_id, amount in contributions.items()
             if amount),
            key=lambda item: item[0],
        )
        rows.append({
            "identity": _identity_obj(identity),
            "water_litres": str(sink_water.get(identity, ZERO)),
            "sources": [
                {"identity": _identity_obj(source_id),
                 "water_litres": str(amount)}
                for source_id, amount in details
            ],
        })
    return rows
