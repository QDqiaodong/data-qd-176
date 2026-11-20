"""共用蓄水池的多日供水计划。

仅生成供人工审核的供水计划，不操作任何设备。规划模式在原有田块、气象与
已承诺供水（applications）契约之上增加 planning 对象；田间水量平衡完全复用
``irrigation.service.run`` 与 ``irrigation.balance.step`` 的既有口径。

求解采用按日动态规划：状态为（蓄水池当期蓄水，各田块期末储水），同一状态
只保留按（累计加权缺水、累计毛供水升数、决策序列字典序）比较的最优路径，
最终在末日再加入期末目标缺水比较，从而在支持规模内得到精确全局最优，不使用
浮点容差或贪心截断。
"""
from itertools import product
from fractions import Fraction

from .io import integer
from .model import validate as _validate_base, day
from .balance import step
from .service import run as run_balance

MAX_FIELDS = 3
MAX_DAYS = 7


def plan(data):
    # 先完整执行原有输入校验：字段、气象升序连续性、applications 引用与非负整数。
    fields, weather, applications = _validate_base(data)
    capacity, opening_litres, supply, rules = _validate_planning(data, fields, weather)

    dates = [day(row["date"]) for row in weather]
    field_ids = sorted(fields)
    field_count = len(field_ids)
    weights = {fid: rules[fid]["stress_weight"] for fid in field_ids}

    # 状态 -> (累计加权未满足 ET, 累计毛供水升数, 逐日逐田块 gross_mm 决策元组)
    initial_state = (
        opening_litres,
        tuple(Fraction(fields[fid]["opening_mm"]) for fid in field_ids),
    )
    dp = {initial_state: (Fraction(0), 0, ())}

    for di, when in enumerate(dates):
        observed = weather[di]
        sup = supply[when]
        option_lists = []
        for fid in field_ids:
            key = (fid, when)
            if key in applications:
                # 显式锁定，包括显式 gross_mm=0；缺记录才走可选深度。
                option_lists.append([(applications[key], True)])
            else:
                option_lists.append([(gross, False) for gross in rules[fid]["gross_mm_choices"]])

        next_dp = {}
        for (level, closings), (past_unmet, past_litres, past_decisions) in dp.items():
            filled = level + sup["inflow_litres"]
            spill = max(0, filled - capacity)
            available = filled - spill  # 溢流已经流走，当日抽水不能倒减溢流
            for combo in product(*option_lists):
                gross_used = sum(
                    gross * fields[fid]["area_m2"]
                    for (gross, _locked), fid in zip(combo, field_ids)
                )
                if gross_used > sup["pump_limit_litres"]:
                    continue
                closing_level = available - gross_used
                if closing_level < sup["minimum_closing_litres"]:
                    continue  # 不能借入未来来水，最低蓄水是每日硬约束

                next_closings = []
                added_unmet = Fraction(0)
                for (gross, _locked), fid, closing_mm in zip(combo, field_ids, closings):
                    result = step(
                        fields[fid], closing_mm,
                        observed["rain_mm"], gross, observed["et_mm"],
                    )
                    next_closings.append(result["closing_mm"])
                    added_unmet += weights[fid] * result["unmet_et_mm"]

                candidate = (
                    past_unmet + added_unmet,
                    past_litres + gross_used,
                    past_decisions + tuple(gross for gross, _locked in combo),
                )
                state = (closing_level, tuple(next_closings))
                incumbent = next_dp.get(state)
                # 同状态的未来代价完全相同，按前缀目标保留唯一最优路径即可。
                if incumbent is None or candidate < incumbent:
                    next_dp[state] = candidate
        dp = next_dp
        if not dp:
            return _infeasible_report(
                fields, weather, dates, field_ids, applications,
                supply, capacity, opening_litres,
            )

    best_key = None
    best_payload = None
    for (level, closings), (total_unmet, total_litres, decisions) in dp.items():
        terminal_deficit = sum(
            weights[fid]
            * max(Fraction(0), Fraction(fields[fid]["target_mm"]) - closing_mm)
            for fid, closing_mm in zip(field_ids, closings)
        )
        key = (total_unmet, terminal_deficit, total_litres, decisions)
        if best_key is None or key < best_key:
            best_key = key
            best_payload = (level, closings, decisions)

    _level, _closings, decisions = best_payload
    total_unmet, terminal_deficit, total_litres = best_key[0], best_key[1], best_key[2]

    decision_rows = []
    for di, when in enumerate(dates):
        for fi, fid in enumerate(field_ids):
            gross = decisions[di * field_count + fi]
            decision_rows.append({
                "field_id": fid,
                "date": weather[di]["date"],
                "gross_mm": gross,
                "locked": (fid, when) in applications,
            })

    reservoir_rows = _reservoir_rows(
        fields, weather, dates, field_ids, decisions,
        supply, capacity, opening_litres, field_count,
    )

    # 以 decisions 作为完整 applications 调用原有核算；不修改原输入对象。
    balance_input = {
        "fields": data["fields"],
        "weather": data["weather"],
        "applications": [
            {"field_id": row["field_id"], "date": row["date"], "gross_mm": row["gross_mm"]}
            for row in decision_rows
        ],
    }
    water_balance = run_balance(balance_input)

    return {
        "schema_version": 1,
        "status": "optimal",
        "decisions": decision_rows,
        "objective": {
            "weighted_unmet_et_mm": str(total_unmet),
            "weighted_terminal_deficit_mm": str(terminal_deficit),
            "gross_supply_litres": total_litres,
        },
        "reservoir_daily": reservoir_rows,
        "water_balance": water_balance,
    }


def _reservoir_rows(fields, weather, dates, field_ids, decisions,
                    supply, capacity, opening_litres, field_count):
    rows = []
    level = opening_litres
    for di, when in enumerate(dates):
        sup = supply[when]
        opening = level
        filled = opening + sup["inflow_litres"]
        spill = max(0, filled - capacity)
        available = filled - spill
        gross_used = 0
        for fi, fid in enumerate(field_ids):
            gross_used += decisions[di * field_count + fi] * fields[fid]["area_m2"]
        closing = available - gross_used
        residual = opening + sup["inflow_litres"] - spill - gross_used - closing
        rows.append({
            "date": weather[di]["date"],
            "opening_litres": opening,
            "inflow_litres": sup["inflow_litres"],
            "spill_litres": spill,
            "available_litres": available,
            "gross_used_litres": gross_used,
            "closing_litres": closing,
            "minimum_closing_litres": sup["minimum_closing_litres"],
            "pump_limit_litres": sup["pump_limit_litres"],
            "balance_residual_litres": residual,
        })
        level = closing
    return rows


def _infeasible_report(fields, weather, dates, field_ids, applications,
                       supply, capacity, opening_litres):
    """用只执行锁定供水（未锁定全部取 0）的前向模拟给出中文不可行原因。

    未锁定组合始终包含 0，且少抽水只会让池内存水更多，因此只要零供水路径
    违反硬约束，整个问题就不可行；反之零供水路径可行即存在可行计划。
    """
    level = opening_litres
    reason = "不存在满足蓄水池泵量、最低蓄水与锁定供水约束的可行供水组合"
    for di, when in enumerate(dates):
        sup = supply[when]
        filled = level + sup["inflow_litres"]
        spill = max(0, filled - capacity)
        available = filled - spill
        minimum = sup["minimum_closing_litres"]
        label = f"第 {di + 1} 日（{weather[di]['date']}）"
        if available < minimum:
            reason = (
                f"{label}蓄水池接收来水并溢流后仅 {available} 升，"
                f"无法维持最低期末蓄水 {minimum} 升，未来来水不能提前借用"
            )
            break
        locked_used = sum(
            applications.get((fid, when), 0) * fields[fid]["area_m2"]
            for fid in field_ids
        )
        if locked_used > sup["pump_limit_litres"]:
            reason = (
                f"{label}锁定供水合计 {locked_used} 升，"
                f"超过当日泵量上限 {sup['pump_limit_litres']} 升"
            )
            break
        if available - locked_used < minimum:
            reason = (
                f"{label}执行锁定供水 {locked_used} 升后期末蓄水 "
                f"{available - locked_used} 升，低于最低期末蓄水 {minimum} 升"
            )
            break
        level = available - locked_used
    return {"schema_version": 1, "status": "infeasible", "reason": reason}


def _validate_planning(data, fields, weather):
    if not (1 <= len(fields) <= MAX_FIELDS and 1 <= len(weather) <= MAX_DAYS):
        raise ValueError(
            f"planning mode supports 1..{MAX_FIELDS} fields and 1..{MAX_DAYS} weather days"
        )
    planning = data["planning"]
    if not isinstance(planning, dict):
        raise ValueError("planning must be an object")

    reservoir = planning["reservoir"]
    if not isinstance(reservoir, dict):
        raise ValueError("planning.reservoir must be an object")
    capacity = integer(reservoir["capacity_litres"], "capacity_litres", 1)
    opening = integer(reservoir["opening_litres"], "opening_litres")
    if opening > capacity:
        raise ValueError("opening_litres must not exceed capacity_litres")

    dates = [day(row["date"]) for row in weather]
    supply_rows = planning["supply_days"]
    if not isinstance(supply_rows, list) or len(supply_rows) != len(dates):
        raise ValueError("supply_days must contain exactly one row per weather day")
    supply = {}
    for row in supply_rows:
        if not isinstance(row, dict):
            raise ValueError("supply_days rows must be objects")
        when = day(row["date"])
        if when not in dates:
            raise ValueError("supply_days date is outside the weather window")
        if when in supply:
            raise ValueError(f"duplicate supply_days date: {row['date']}")
        inflow = integer(row["inflow_litres"], "inflow_litres")
        pump_limit = integer(row["pump_limit_litres"], "pump_limit_litres")
        minimum = integer(row["minimum_closing_litres"], "minimum_closing_litres")
        if minimum > capacity:
            raise ValueError("minimum_closing_litres must not exceed capacity_litres")
        supply[when] = {
            "inflow_litres": inflow,
            "pump_limit_litres": pump_limit,
            "minimum_closing_litres": minimum,
        }

    rule_rows = planning["field_rules"]
    if not isinstance(rule_rows, list) or len(rule_rows) != len(fields):
        raise ValueError("field_rules must contain exactly one row per field")
    rules = {}
    for row in rule_rows:
        if not isinstance(row, dict):
            raise ValueError("field_rules rows must be objects")
        fid = row["field_id"]
        if not isinstance(fid, str) or not fid:
            raise ValueError("field_rules.field_id must be a nonempty string")
        if fid not in fields:
            raise ValueError(f"field_rules references unknown field: {fid}")
        if fid in rules:
            raise ValueError(f"duplicate field_rules field_id: {fid}")
        weight = integer(row["stress_weight"], "stress_weight", 1)
        choices = row["gross_mm_choices"]
        if not isinstance(choices, list) or not 1 <= len(choices) <= 3:
            raise ValueError("gross_mm_choices must be an array of 1..3 integers")
        values = [integer(value, "gross_mm_choices") for value in choices]
        if values[0] != 0 or any(values[i] <= values[i - 1] for i in range(1, len(values))):
            raise ValueError(
                "gross_mm_choices must be strictly increasing non-negative integers starting with 0"
            )
        rules[fid] = {"stress_weight": weight, "gross_mm_choices": values}
    return capacity, opening, supply, rules
