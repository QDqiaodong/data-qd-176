"""回用水盐分核算与水质拦截（日尺度完全混合模型）。

合作社把田间径流与渗漏收回蓄水池再次灌溉。本模块只核算**给定的实测操作**：
逐池核对蒸发、外来补水、同日池间循环输水、抽水与溢流，逐田块核对水质拦截、
输水损失、雨水稀释、径流/渗漏携盐与跨日回流，并给出精确有理数的水盐残差。
它不选择供水计划、不推算收费、不操作设备，也不读写计量台账。

公开接口 ``run(data)`` 与命令 ``soil-water-reuse`` 共用同一业务逻辑。全部计算
使用 ``fractions.Fraction`` 精确有理数，输入对象不会被修改；零水量混合不做
除零，浓度按契约输出 ``null``，干盐保留并在下次遇水时全部重新溶解。
"""
from __future__ import annotations

from datetime import timedelta
from fractions import Fraction

from .balance import step
from .io import integer
from .model import day as _day
from .model import validate as _validate_base

ROOT_KEYS = {"fields", "weather", "tanks", "field_reuse", "operations"}
OP_KEYS = {"date", "inflows", "evaporations", "transfers", "withdrawals"}
TANK_KEYS = {"id", "capacity_litres", "opening_litres", "opening_salt_mg"}
REUSE_KEYS = {"field_id", "opening_salt_mg", "max_source_mg_per_litre",
              "return_permille", "return_tank_id"}
INFLOW_KEYS = {"tank_id", "water_litres", "salt_mg"}
EVAP_KEYS = {"tank_id", "water_litres"}
TRANSFER_KEYS = {"from_tank_id", "to_tank_id", "water_litres"}
WITHDRAWAL_KEYS = {"field_id", "tank_id", "gross_litres"}


# ---------------------------------------------------------------------------
# 基础校验
# ---------------------------------------------------------------------------

def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def _exact_keys(obj, keys, label):
    actual = set(obj)
    missing = keys - actual
    if missing:
        raise ValueError(f"{label} 缺少必填字段: {sorted(missing)}")
    extra = actual - keys
    if extra:
        raise ValueError(f"{label} 包含未声明字段: {sorted(extra)}")


def _text(value, label):
    # 非空字符串，保留原值：不做 strip、大小写或 Unicode 归并。
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组")
    return value


def _validate_tanks(data):
    rows = _array(data.get("tanks"), "tanks")
    if not 1 <= len(rows) <= 12:
        raise ValueError("tanks 必须包含 1..12 条记录")
    tanks = {}
    for row in rows:
        _object(row, "tanks 行")
        _exact_keys(row, TANK_KEYS, "tanks 行")
        tid = _text(row["id"], "tanks.id")
        if tid in tanks:
            raise ValueError(f"tanks 中池 id 重复: {tid}")
        capacity = integer(row["capacity_litres"], "capacity_litres", 1)
        opening = integer(row["opening_litres"], "opening_litres")
        opening_salt = integer(row["opening_salt_mg"], "opening_salt_mg")
        if opening > capacity:
            raise ValueError("opening_litres 不得超过 capacity_litres")
        tanks[tid] = {
            "id": tid,
            "capacity_litres": capacity,
            "opening_litres": opening,
            "opening_salt_mg": opening_salt,
        }
    return tanks


def _validate_field_reuse(data, fields, tank_ids):
    rows = _array(data.get("field_reuse"), "field_reuse")
    if len(rows) != len(fields):
        raise ValueError("field_reuse 必须每个田块恰好一条")
    reuse = {}
    for row in rows:
        _object(row, "field_reuse 行")
        _exact_keys(row, REUSE_KEYS, "field_reuse 行")
        fid = _text(row["field_id"], "field_reuse.field_id")
        if fid not in fields:
            raise ValueError(f"field_reuse 引用了未知田块: {fid}")
        if fid in reuse:
            raise ValueError(f"field_reuse 中田块重复: {fid}")
        opening_salt = integer(row["opening_salt_mg"], "opening_salt_mg")
        limit = integer(row["max_source_mg_per_litre"],
                        "max_source_mg_per_litre")
        permille = integer(row["return_permille"], "return_permille")
        if not 0 <= permille <= 1000:
            raise ValueError("return_permille 必须在 0..1000 之间")
        return_tank = _text(row["return_tank_id"], "return_tank_id")
        if return_tank not in tank_ids:
            raise ValueError(f"return_tank_id 引用了未知蓄水池: {return_tank}")
        reuse[fid] = {
            "field_id": fid,
            "opening_salt_mg": opening_salt,
            "max_source_mg_per_litre": limit,
            "return_permille": permille,
            "return_tank_id": return_tank,
        }
    if set(reuse) != set(fields):
        raise ValueError("field_reuse 必须覆盖全部田块且不得缺失或重复")
    return reuse


def _validate_operations(data, date_set, tank_ids, field_ids):
    rows = _array(data.get("operations"), "operations")
    if len(rows) != len(date_set):
        raise ValueError("operations 必须每个气象日恰好一条")
    per_day = {}
    for row in rows:
        _object(row, "operations 行")
        _exact_keys(row, OP_KEYS, "operations 行")
        when = _day(_text(row["date"], "operations.date"))
        if when not in date_set:
            raise ValueError(f"operations 日期超出气象窗口: {row['date']}")
        if when in per_day:
            raise ValueError(f"operations 日期重复: {row['date']}")

        inflows = {}
        for item in _array(row["inflows"], "inflows"):
            _object(item, "inflows 行")
            _exact_keys(item, INFLOW_KEYS, "inflows 行")
            tid = _text(item["tank_id"], "inflows.tank_id")
            if tid not in tank_ids:
                raise ValueError(f"inflows 引用了未知蓄水池: {tid}")
            if tid in inflows:
                raise ValueError(f"同一池当日 inflows 重复: {tid}")
            water = integer(item["water_litres"], "water_litres")
            salt = integer(item["salt_mg"], "salt_mg")
            if water == 0 and salt != 0:
                raise ValueError("inflows 水量为 0 时盐量必须为 0")
            inflows[tid] = {"tank_id": tid, "water_litres": water,
                            "salt_mg": salt}

        evaporations = {}
        for item in _array(row["evaporations"], "evaporations"):
            _object(item, "evaporations 行")
            _exact_keys(item, EVAP_KEYS, "evaporations 行")
            tid = _text(item["tank_id"], "evaporations.tank_id")
            if tid not in tank_ids:
                raise ValueError(f"evaporations 引用了未知蓄水池: {tid}")
            if tid in evaporations:
                raise ValueError(f"同一池当日 evaporations 重复: {tid}")
            water = integer(item["water_litres"], "water_litres")
            evaporations[tid] = {"tank_id": tid, "water_litres": water}

        transfers = []
        seen_pairs = set()
        for item in _array(row["transfers"], "transfers"):
            _object(item, "transfers 行")
            _exact_keys(item, TRANSFER_KEYS, "transfers 行")
            src = _text(item["from_tank_id"], "from_tank_id")
            dst = _text(item["to_tank_id"], "to_tank_id")
            if src not in tank_ids or dst not in tank_ids:
                raise ValueError("transfers 引用了未知蓄水池")
            if src == dst:
                raise ValueError("transfers 不允许自环输水")
            pair = (src, dst)
            if pair in seen_pairs:
                raise ValueError(f"同一有向池对当日 transfers 重复: {pair}")
            seen_pairs.add(pair)
            water = integer(item["water_litres"], "water_litres", 1)
            transfers.append({"from_tank_id": src, "to_tank_id": dst,
                              "water_litres": water})

        withdrawals = []
        seen_fields = set()
        for item in _array(row["withdrawals"], "withdrawals"):
            _object(item, "withdrawals 行")
            _exact_keys(item, WITHDRAWAL_KEYS, "withdrawals 行")
            fid = _text(item["field_id"], "withdrawals.field_id")
            tid = _text(item["tank_id"], "withdrawals.tank_id")
            if fid not in field_ids:
                raise ValueError(f"withdrawals 引用了未知田块: {fid}")
            if tid not in tank_ids:
                raise ValueError(f"withdrawals 引用了未知蓄水池: {tid}")
            if fid in seen_fields:
                raise ValueError(f"同一田块当日 withdrawals 重复: {fid}")
            seen_fields.add(fid)
            gross = integer(item["gross_litres"], "gross_litres")
            withdrawals.append({"field_id": fid, "tank_id": tid,
                                "gross_litres": gross})

        per_day[when] = {
            "date": when,
            "inflows": inflows,
            "evaporations": evaporations,
            "transfers": transfers,
            "withdrawals": withdrawals,
        }
    if set(per_day) != date_set:
        raise ValueError("operations 必须覆盖全部气象日，不得缺失或重复")
    return per_day


# ---------------------------------------------------------------------------
# 同日完全混合期的浓度联合求解（精确有理数，允许环路）
# ---------------------------------------------------------------------------

def _gaussian_solve(matrix, vector):
    """对 A x = b 做精确 Fraction 高斯-约当消元；奇异矩阵抛 ValueError。"""
    n = len(vector)
    for col in range(n):
        pivot = next((r for r in range(col, n)
                      if matrix[r][col] != 0), None)
        if pivot is None:
            raise ValueError("同日循环输水缺乏正水量底水支撑，浓度无法唯一确定")
        if pivot != col:
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            vector[col], vector[pivot] = vector[pivot], vector[col]
        pivot_value = matrix[col][col]
        for r in range(n):
            if r == col or matrix[r][col] == 0:
                continue
            factor = matrix[r][col] / pivot_value
            matrix[r][col] = Fraction(0)
            for k in range(col + 1, n):
                matrix[r][k] -= factor * matrix[col][k]
            vector[r] -= factor * vector[col]
    return [vector[i] / matrix[i][i] for i in range(n)]


def _solve_mixing(tank_ids, base_water, base_salt, in_edges, out_edges):
    """返回 {tank_id: 浓度 Fraction 或 None}。

    混合期方程（M_i 为混合水量，S_i 为混合前盐量，x_{k->i} 为入池输水）：
        M_i C_i - Σ x_{k->i} C_k = S_i
    底水可达性先做图校验，排除无源伪循环；随后线性方程必有唯一解。
    """
    # 底水 = 蒸发后剩水 + 外来水（含今晨已到账回流），正水量底水为锚点。
    anchors = {tid for tid in tank_ids if base_water[tid] > 0}
    participants = set()
    for src, edges in out_edges.items():
        if edges:
            participants.add(src)
            for dst, _water in edges:
                participants.add(dst)
    reached = set(anchors)
    queue = list(anchors)
    while queue:
        current = queue.pop()
        for dst, _water in out_edges.get(current, ()):  # 正向沿输水图
            if dst not in reached:
                reached.add(dst)
                queue.append(dst)
    unsupported = participants - reached
    if unsupported:
        raise ValueError(
            "存在没有正水量底水支撑的循环输水（伪循环），拒绝凭空造水: "
            + ", ".join(sorted(unsupported))
        )

    mixed_water = {
        tid: base_water[tid]
        + sum(water for _src, water in in_edges.get(tid, ()))
        for tid in tank_ids
    }
    mixed_ids = [tid for tid in tank_ids if mixed_water[tid] > 0]
    index = {tid: i for i, tid in enumerate(mixed_ids)}
    n = len(mixed_ids)
    matrix = [[Fraction(0) for _ in range(n)] for _ in range(n)]
    vector = [Fraction(0) for _ in range(n)]
    for i, tid in enumerate(mixed_ids):
        matrix[i][i] = mixed_water[tid]
        for src, water in in_edges.get(tid, ()):
            matrix[i][index[src]] -= Fraction(water)
        vector[i] = base_salt[tid]
    solved = _gaussian_solve(matrix, vector)
    concentration = {tid: None for tid in tank_ids}
    for tid, value in zip(mixed_ids, solved):
        concentration[tid] = value
    return concentration, mixed_water


# ---------------------------------------------------------------------------
# 主核算
# ---------------------------------------------------------------------------

def run(data):  # noqa: A001 - 与既有 service.run 同名的公开接口
    _object(data, "输入根对象")
    _exact_keys(data, ROOT_KEYS, "输入根对象")

    # fields/weather 沿用旧水账的字段、校验与规模上限（1..200 田块、1..3660 日）。
    fields, weather, _no_apps = _validate_base({
        "fields": data["fields"],
        "weather": data["weather"],
        "applications": [],
    })
    dates = [_day(row["date"]) for row in weather]
    date_set = set(dates)
    weather_by_date = {row["date"]: row for row in weather}
    field_ids = sorted(fields)

    tanks = _validate_tanks(data)
    tank_ids = sorted(tanks)
    tank_id_set = set(tank_ids)
    reuse = _validate_field_reuse(data, fields, tank_id_set)
    operations = _validate_operations(data, date_set, tank_id_set, set(field_ids))

    # 池状态（Fraction；允许有水 0、干盐非 0）。
    tank_water = {tid: Fraction(tanks[tid]["opening_litres"]) for tid in tank_ids}
    tank_salt = {tid: Fraction(tanks[tid]["opening_salt_mg"]) for tid in tank_ids}
    # 田块土壤状态。
    soil_water = {fid: Fraction(fields[fid]["opening_mm"]) for fid in field_ids}
    soil_salt = {fid: Fraction(reuse[fid]["opening_salt_mg"]) for fid in field_ids}
    # 次日一早到账的回流 {tank_id: [water, salt]}。
    pending = {}

    tank_daily = []
    deliveries = []
    field_daily = []

    zero = Fraction(0)

    # 系统累计量。
    sys_open_water = sum(tank_water.values(), zero) + sum(
        Fraction(fields[fid]["opening_mm"]) * fields[fid]["area_m2"]
        for fid in field_ids)
    sys_open_salt = sum(tank_salt.values(), zero) + sum(
        soil_salt.values(), zero)
    sys_external_water = zero
    sys_external_salt = zero
    sys_rain = zero
    sys_evaporation = zero
    sys_actual_et = zero
    sys_spill_water = zero
    sys_spill_salt = zero
    sys_rejected_water = zero
    sys_rejected_salt = zero
    sys_loss_water = zero
    sys_loss_salt = zero
    sys_discharged_water = zero
    sys_discharged_salt = zero

    for when in dates:
        iso = when.isoformat()
        op = operations[when]

        # 期初 = 昨日池末值（不含今晨回流）。
        opening_water = dict(tank_water)
        opening_salt = dict(tank_salt)

        # --- 时点 1：接收昨日排定的回流 ---
        return_water = {tid: zero for tid in tank_ids}
        return_salt = {tid: zero for tid in tank_ids}
        for tid, (water, salt) in pending.items():
            return_water[tid] = water
            return_salt[tid] = salt
            tank_water[tid] += water
            tank_salt[tid] += salt
        pending = {}

        # --- 时点 2：池面蒸发，只移走纯水；外来水/输水不能替它补足 ---
        evap_water = {tid: zero for tid in tank_ids}
        for tid, item in op["evaporations"].items():
            amount = Fraction(item["water_litres"])
            if amount > tank_water[tid]:
                raise ValueError(
                    f"{iso} 池 {tid} 蒸发量 {amount} 超过回流后已有水量 "
                    f"{tank_water[tid]}，外来补水与同日输水不能替它补足"
                )
            evap_water[tid] = amount
            tank_water[tid] -= amount
        sys_evaporation += sum(evap_water.values(), zero)

        # --- 时点 3：接收当日外来补水 ---
        external_water = {tid: zero for tid in tank_ids}
        external_salt = {tid: zero for tid in tank_ids}
        for tid, item in op["inflows"].items():
            water = Fraction(item["water_litres"])
            salt = Fraction(item["salt_mg"])
            external_water[tid] = water
            external_salt[tid] = salt
            tank_water[tid] += water
            tank_salt[tid] += salt
        sys_external_water += sum(external_water.values(), zero)
        sys_external_salt += sum(external_salt.values(), zero)

        # --- 时点 4：同日完全混合期（含环路联合浓度）---
        base_water = dict(tank_water)
        base_salt = dict(tank_salt)
        in_edges = {tid: [] for tid in tank_ids}
        out_edges = {tid: [] for tid in tank_ids}
        for edge in op["transfers"]:
            src, dst = edge["from_tank_id"], edge["to_tank_id"]
            water = Fraction(edge["water_litres"])
            out_edges[src].append((dst, water))
            in_edges[dst].append((src, water))
        concentration, mixed_water = _solve_mixing(
            tank_ids, base_water, base_salt, in_edges, out_edges)

        transfer_in_water = {
            tid: sum((w for _s, w in in_edges[tid]), zero)
            for tid in tank_ids}
        transfer_out_water = {
            tid: sum((w for _d, w in out_edges[tid]), zero)
            for tid in tank_ids}

        # 出池输水 + 抽水总量不得超过混合水量。
        withdrawn_gross = {tid: zero for tid in tank_ids}
        for item in op["withdrawals"]:
            withdrawn_gross[item["tank_id"]] += Fraction(item["gross_litres"])
        for tid in tank_ids:
            if transfer_out_water[tid] + withdrawn_gross[tid] > mixed_water[tid]:
                raise ValueError(
                    f"{iso} 池 {tid} 出池输水与抽水合计超过其混合水量"
                )

        transfer_in_salt = {tid: zero for tid in tank_ids}
        transfer_out_salt = {tid: zero for tid in tank_ids}
        for edge in op["transfers"]:
            src, dst = edge["from_tank_id"], edge["to_tank_id"]
            salt = Fraction(edge["water_litres"]) * concentration[src]
            transfer_out_salt[src] += salt
            transfer_in_salt[dst] += salt

        # --- 时点 5：抽水与水质拦截（判断使用来源池混合浓度）---
        withdrawn_salt = {tid: zero for tid in tank_ids}
        accepted_by_field = {}
        for item in op["withdrawals"]:
            fid, tid = item["field_id"], item["tank_id"]
            gross = Fraction(item["gross_litres"])
            source_conc = concentration[tid]
            # 零升记录无携盐；来源无混合水时浓度仍为 null。
            pumped_salt = gross * source_conc if gross > 0 else zero
            withdrawn_salt[tid] += pumped_salt
            limit = Fraction(reuse[fid]["max_source_mg_per_litre"])
            # 等于限值允许入田；严格大于才拦截。零升始终接受。
            accepted = gross == 0 or source_conc <= limit
            if accepted:
                applied_water, applied_salt = gross, pumped_salt
                rejected_water, rejected_salt = zero, zero
                accepted_by_field[fid] = (gross, pumped_salt)
            else:
                applied_water, applied_salt = zero, zero
                rejected_water, rejected_salt = gross, pumped_salt
                sys_rejected_water += gross
                sys_rejected_salt += pumped_salt
            deliveries.append({
                "date": iso,
                "field_id": fid,
                "tank_id": tid,
                "requested_litres": gross,
                "source_mg_per_litre": source_conc,
                "accepted": accepted,
                "applied_litres": applied_water,
                "applied_salt_mg": applied_salt,
                "rejected_litres": rejected_water,
                "rejected_salt_mg": rejected_salt,
            })

        # --- 时点 6：出水后溢流，按当日浓度携盐 ---
        spill_water = {tid: zero for tid in tank_ids}
        spill_salt = {tid: zero for tid in tank_ids}
        closing_water = {}
        closing_salt = {}
        for tid in tank_ids:
            remaining_water = (
                mixed_water[tid] - transfer_out_water[tid] - withdrawn_gross[tid]
            )
            remaining_salt = (
                base_salt[tid] + transfer_in_salt[tid]
                - transfer_out_salt[tid] - withdrawn_salt[tid]
            )
            capacity = tanks[tid]["capacity_litres"]
            if remaining_water > capacity:
                spill_water[tid] = remaining_water - capacity
                spill_salt[tid] = spill_water[tid] * concentration[tid]
            closing_water[tid] = remaining_water - spill_water[tid]
            closing_salt[tid] = remaining_salt - spill_salt[tid]
        sys_spill_water += sum(spill_water.values(), zero)
        sys_spill_salt += sum(spill_salt.values(), zero)

        # --- 池日账与逐池残差 ---
        for tid in tank_ids:
            water_residual = (
                opening_water[tid] + return_water[tid] + external_water[tid]
                + transfer_in_water[tid] - evap_water[tid]
                - transfer_out_water[tid] - withdrawn_gross[tid]
                - spill_water[tid] - closing_water[tid]
            )
            salt_residual = (
                opening_salt[tid] + return_salt[tid] + external_salt[tid]
                + transfer_in_salt[tid] - transfer_out_salt[tid]
                - withdrawn_salt[tid] - spill_salt[tid]
                - closing_salt[tid]
            )
            if water_residual != 0 or salt_residual != 0:
                raise ValueError("蓄水池日账水盐残差不为 0")
            conc = concentration[tid]
            tank_daily.append({
                "date": iso,
                "tank_id": tid,
                "opening_litres": opening_water[tid],
                "opening_salt_mg": opening_salt[tid],
                "return_litres": return_water[tid],
                "return_salt_mg": return_salt[tid],
                "evaporation_litres": evap_water[tid],
                "external_litres": external_water[tid],
                "external_salt_mg": external_salt[tid],
                "transfer_in_litres": transfer_in_water[tid],
                "transfer_in_salt_mg": transfer_in_salt[tid],
                "mixed_litres": mixed_water[tid],
                "concentration_mg_per_litre": conc,
                "transfer_out_litres": transfer_out_water[tid],
                "transfer_out_salt_mg": transfer_out_salt[tid],
                "withdrawn_litres": withdrawn_gross[tid],
                "withdrawn_salt_mg": withdrawn_salt[tid],
                "spill_litres": spill_water[tid],
                "spill_salt_mg": spill_salt[tid],
                "closing_litres": closing_water[tid],
                "closing_salt_mg": closing_salt[tid],
                "water_residual_litres": water_residual,
                "salt_residual_mg": salt_residual,
            })

        tank_water = closing_water
        tank_salt = closing_salt

        # --- 田块日：严格复用 balance.step 的水账顺序，盐按约定随水迁移 ---
        next_pending = {}
        for fid in field_ids:
            field = fields[fid]
            area = field["area_m2"]
            observed = weather_by_date[iso]
            rain = Fraction(observed["rain_mm"])
            et = Fraction(observed["et_mm"])
            opening_mm = soil_water[fid]
            opening_salt = soil_salt[fid]

            gross_litres, irrigation_salt = accepted_by_field.get(
                fid, (zero, zero))
            gross_mm = gross_litres / area
            result = step(field, opening_mm, rain, gross_mm, et)
            loss_mm = result["delivery_loss_mm"]
            runoff_mm = result["runoff_mm"]
            drainage_mm = result["drainage_mm"]
            delivered_mm = gross_mm - loss_mm
            incoming_mm = rain + delivered_mm

            # 输水损失按损失水量比例携盐。
            if gross_mm > 0:
                loss_salt = irrigation_salt * loss_mm / gross_mm
            else:
                loss_salt = zero
            delivered_salt = irrigation_salt - loss_salt
            # 到田水与无盐降雨先混匀，径流按水量比例携盐。
            if incoming_mm > 0:
                runoff_salt = delivered_salt * runoff_mm / incoming_mm
            else:
                runoff_salt = zero
            infiltrated_salt = delivered_salt - runoff_salt
            # 入渗水盐与期初土壤水盐完全混匀，容量渗漏按此时浓度携盐。
            storage_mm = opening_mm + (incoming_mm - runoff_mm)
            storage_salt = opening_salt + infiltrated_salt
            if storage_mm > 0:
                drainage_salt = storage_salt * drainage_mm / storage_mm
            else:
                drainage_salt = zero
            # ET 只移走纯水，期末盐量不受 ET 影响。
            closing_field_salt = storage_salt - drainage_salt
            field_salt_residual = (
                opening_salt + irrigation_salt - loss_salt - runoff_salt
                - drainage_salt - closing_field_salt
            )
            if field_salt_residual != 0:
                raise ValueError("田块日账盐量残差不为 0")

            # 当日径流/渗漏按回收比例排定为次日回流，其余离开系统。
            permille = Fraction(reuse[fid]["return_permille"], 1000)
            drain_water_litres = (runoff_mm + drainage_mm) * area
            drain_salt = runoff_salt + drainage_salt
            recovered_water = drain_water_litres * permille
            recovered_salt = drain_salt * permille
            discharged_water = drain_water_litres - recovered_water
            discharged_salt = drain_salt - recovered_salt
            target_tank = reuse[fid]["return_tank_id"]
            if recovered_water != 0 or recovered_salt != 0:
                bucket = next_pending.setdefault(
                    target_tank, [zero, zero])
                bucket[0] += recovered_water
                bucket[1] += recovered_salt

            sys_loss_water += loss_mm * area
            sys_loss_salt += loss_salt
            sys_discharged_water += discharged_water
            sys_discharged_salt += discharged_salt
            sys_rain += rain * area
            sys_actual_et += result["actual_et_mm"] * area

            water_balance = {"field_id": fid, "date": iso}
            for key, value in result.items():
                if key == "advised_next_gross_mm":
                    water_balance[key] = value
                else:
                    water_balance[key] = str(Fraction(value))

            field_daily.append({
                "field_id": fid,
                "date": iso,
                "water_balance": water_balance,
                "opening_salt_mg": opening_salt,
                "irrigation_salt_mg": irrigation_salt,
                "delivery_loss_salt_mg": loss_salt,
                "runoff_salt_mg": runoff_salt,
                "drainage_salt_mg": drainage_salt,
                "closing_salt_mg": closing_field_salt,
                "recovered_litres": recovered_water,
                "recovered_salt_mg": recovered_salt,
                "discharged_litres": discharged_water,
                "discharged_salt_mg": discharged_salt,
                "salt_residual_mg": field_salt_residual,
            })

            soil_water[fid] = result["closing_mm"]
            soil_salt[fid] = closing_field_salt

        pending = next_pending

    # --- 业务排序与输入排列无关 ---
    tank_daily.sort(key=lambda r: (r["date"], r["tank_id"]))
    deliveries.sort(key=lambda r: (r["date"], r["field_id"]))
    field_daily.sort(key=lambda r: (r["field_id"], r["date"]))

    pending_rows = []
    pending_total_water = zero
    pending_total_salt = zero
    next_day = (dates[-1] + timedelta(days=1)).isoformat()
    for tid in sorted(pending):
        water, salt = pending[tid]
        pending_total_water += water
        pending_total_salt += salt
        pending_rows.append({
            "date": next_day,
            "tank_id": tid,
            "water_litres": water,
            "salt_mg": salt,
        })

    sys_close_water = sum(tank_water.values(), zero) + sum(
        soil_water[fid] * fields[fid]["area_m2"] for fid in field_ids)
    sys_close_salt = sum(tank_salt.values(), zero) + sum(
        soil_salt.values(), zero)
    water_residual = (
        sys_open_water + sys_external_water + sys_rain
        - sys_close_water - pending_total_water - sys_spill_water
        - sys_rejected_water - sys_loss_water - sys_discharged_water
        - sys_evaporation - sys_actual_et
    )
    salt_residual = (
        sys_open_salt + sys_external_salt
        - sys_close_salt - pending_total_salt - sys_spill_salt
        - sys_rejected_salt - sys_loss_salt - sys_discharged_salt
    )
    if water_residual != 0 or salt_residual != 0:
        raise ValueError("全系统水盐残差不为 0")

    system_balance = {
        "water_litres": {
            "opening": sys_open_water,
            "external": sys_external_water,
            "rain": sys_rain,
            "closing": sys_close_water,
            "pending_returns": pending_total_water,
            "spill": sys_spill_water,
            "rejected": sys_rejected_water,
            "delivery_loss": sys_loss_water,
            "discharged": sys_discharged_water,
            "evaporation": sys_evaporation,
            "actual_et": sys_actual_et,
            "residual": water_residual,
        },
        "salt_mg": {
            "opening": sys_open_salt,
            "external": sys_external_salt,
            "closing": sys_close_salt,
            "pending_returns": pending_total_salt,
            "spill": sys_spill_salt,
            "rejected": sys_rejected_salt,
            "delivery_loss": sys_loss_salt,
            "discharged": sys_discharged_salt,
            "residual": salt_residual,
        },
    }

    return {
        "schema_version": 1,
        "tank_daily": [_stringify(row, null_keys={"concentration_mg_per_litre"})
                       for row in tank_daily],
        "deliveries": [_stringify(row, null_keys={"source_mg_per_litre"})
                       for row in deliveries],
        "field_daily": [_stringify_field(row) for row in field_daily],
        "pending_returns": [_stringify(row) for row in pending_rows],
        "system_balance": {
            kind: {key: str(Fraction(value)) for key, value in items.items()}
            for kind, items in system_balance.items()
        },
    }


def _stringify(row, null_keys=()):
    out = {}
    for key, value in row.items():
        if isinstance(value, str):
            out[key] = value  # date / tank_id / field_id 等身份字符串原样保留
        elif key in null_keys and value is None:
            out[key] = None
        elif isinstance(value, bool):
            out[key] = value
        else:
            out[key] = str(Fraction(value))
    return out


def _stringify_field(row):
    out = {}
    for key, value in row.items():
        if key == "water_balance":
            out[key] = value  # 已按既有 daily 口径格式化（advised 保持整数）
        elif isinstance(value, str):
            out[key] = value
        else:
            out[key] = str(Fraction(value))
    return out
