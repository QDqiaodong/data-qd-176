"""雨量证据补核与供水建议签发（历史复核）。

合作社月底核对田间水账时，某些日期的自动雨量记录只留下几个候选值；分段雨量
合计（``rain_windows``）、土壤探头日终储水闭区间（``probes``）与完整实灌记录
仍可查。本模块在完全保留现有田间水量平衡口径（``irrigation.balance.step`` 的
输水效率、入渗上限、径流、容量渗漏、实际 ET、缺水与建议顺序）的前提下，枚举
全部**完整可能历史**——每天取一个雨量候选、所有分段合计成立、逐田块从原期初
值延续真实上日结存并逐日满足探头界限的逐日雨量序列。

公开接口 ``run(data)`` 与命令 ``soil-water-rain-audit`` 共用同一业务逻辑，仅用
Python >=3.11 标准库。全部水量与边界比较使用 ``fractions.Fraction`` 精确有理数，
不使用浮点或容差；输入对象不会被修改。本模块只复核历史记录与既有建议，不重派
已经发生的灌溉，也不下发任何设备指令。
"""
from __future__ import annotations

import re
from datetime import timedelta
from fractions import Fraction

from .balance import step
from .io import integer, text
from .model import day as parse_day

ROOT_KEYS = {
    "fields", "applications", "weather", "rain_windows", "probes",
    "checkable_rain_dates",
}
WEATHER_KEYS = {"date", "et_mm", "rain_mm_choices"}
WINDOW_KEYS = {"from_date", "through_date", "total_rain_mm"}
PROBE_KEYS = {"field_id", "date", "min_closing_mm", "max_closing_mm"}

MAX_FIELDS = 4
MAX_DAYS = 31
MAX_MULTI_CHOICE_DAYS = 9
MAX_CHOICES = 3

ZERO = Fraction(0)
_CANONICAL_RATIONAL = re.compile(r"(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?")


# ---------------------------------------------------------------------------
# 结构校验
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


def _required(row, key, label):
    """已有对象沿用原口径：只要求字段存在，不拒绝额外字段。"""
    if key not in row:
        raise ValueError(f"{label} 缺少必填字段: {key}")
    return row[key]


def _canonical_fraction(value, label):
    """解析探头上/下界：仅接受规范整数或分数字符串（如 ``"0"``、``"3/2"``）。"""
    if not isinstance(value, str) or not _CANONICAL_RATIONAL.fullmatch(value):
        raise ValueError(f"{label} 必须是规范整数或分数字符串")
    parsed = Fraction(value)
    if str(parsed) != value:
        raise ValueError(f"{label} 不是规范有理数字符串: {value}")
    return parsed


def _validate_fields(rows):
    rows = _array(rows, "fields")
    if not 1 <= len(rows) <= MAX_FIELDS:
        raise ValueError(f"fields 必须包含 1..{MAX_FIELDS} 个田块")
    fields = {}
    for row in rows:
        _object(row, "fields 行")
        fid = text(_required(row, "id", "fields 行"), "fields.id")
        if fid in fields:
            raise ValueError(f"duplicate field id: {fid}")
        area = integer(_required(row, "area_m2", "fields 行"), "area_m2", 1)
        capacity = integer(
            _required(row, "capacity_mm", "fields 行"), "capacity_mm", 1)
        opening = integer(
            _required(row, "opening_mm", "fields 行"), "opening_mm")
        threshold = integer(
            _required(row, "threshold_mm", "fields 行"), "threshold_mm")
        target = integer(
            _required(row, "target_mm", "fields 行"), "target_mm")
        infiltration = integer(
            _required(row, "infiltration_mm", "fields 行"),
            "infiltration_mm", 1)
        efficiency = integer(
            _required(row, "efficiency_permille", "fields 行"),
            "efficiency_permille", 1)
        if not 0 <= threshold <= target <= capacity or opening > capacity:
            raise ValueError("invalid storage limits or efficiency")
        if efficiency > 1000:
            raise ValueError("invalid storage limits or efficiency")
        fields[fid] = {
            "id": fid, "area_m2": area, "capacity_mm": capacity,
            "opening_mm": opening, "threshold_mm": threshold,
            "target_mm": target, "infiltration_mm": infiltration,
            "efficiency_permille": efficiency,
        }
    return fields


def _validate_weather(rows):
    rows = _array(rows, "weather")
    if not 1 <= len(rows) <= MAX_DAYS:
        raise ValueError(f"weather 必须包含 1..{MAX_DAYS} 个气象日")
    days = []
    candidates = []
    ets = []
    multi_choice_days = 0
    for row in rows:
        _object(row, "weather 行")
        _exact_keys(row, WEATHER_KEYS, "weather 行")
        when = parse_day(row["date"])
        if days and when != days[-1] + timedelta(days=1):
            raise ValueError("weather days must be consecutive and ordered")
        choices = _array(row["rain_mm_choices"], "rain_mm_choices")
        if not 1 <= len(choices) <= MAX_CHOICES:
            raise ValueError(
                f"rain_mm_choices 必须包含 1..{MAX_CHOICES} 个候选值")
        values = [integer(value, "rain_mm_choices") for value in choices]
        if len(set(values)) != len(values):
            raise ValueError("rain_mm_choices 候选值不得重复")
        if len(values) > 1:
            multi_choice_days += 1
        days.append(when)
        candidates.append(tuple(sorted(values)))
        ets.append(Fraction(integer(row["et_mm"], "weather.et_mm")))
    if multi_choice_days > MAX_MULTI_CHOICE_DAYS:
        raise ValueError(
            f"最多 {MAX_MULTI_CHOICE_DAYS} 天可以存在多个雨量候选")
    return days, candidates, ets


def _validate_applications(rows, fields, date_index):
    applications = {}
    for row in _array(rows, "applications"):
        _object(row, "applications 行")
        fid = text(_required(row, "field_id", "applications 行"),
                   "applications.field_id")
        when = parse_day(_required(row, "date", "applications 行"))
        gross = integer(_required(row, "gross_mm", "applications 行"),
                        "gross_mm")
        if fid not in fields:
            raise ValueError(f"application 引用了未知田块: {fid}")
        if when not in date_index:
            raise ValueError("application 日期超出气象窗口")
        key = (fid, when)
        if key in applications:
            raise ValueError("duplicate field/day application")
        applications[key] = Fraction(gross)
    return applications


def _validate_windows(rows, date_index):
    windows = []
    seen = set()
    for row in _array(rows, "rain_windows"):
        _object(row, "rain_windows 行")
        _exact_keys(row, WINDOW_KEYS, "rain_windows 行")
        start = parse_day(row["from_date"])
        end = parse_day(row["through_date"])
        if start not in date_index or end not in date_index:
            raise ValueError("rain_windows 起止日期必须在气象窗口内")
        if start > end:
            raise ValueError("rain_windows 起日不得晚于末日")
        if (start, end) in seen:
            raise ValueError("rain_windows 同一起止组合至多一条")
        seen.add((start, end))
        total = integer(row["total_rain_mm"], "total_rain_mm")
        windows.append((date_index[start], date_index[end], total))
    return windows


def _validate_probes(rows, fields, date_index):
    probes = {}
    for row in _array(rows, "probes"):
        _object(row, "probes 行")
        _exact_keys(row, PROBE_KEYS, "probes 行")
        fid = text(row["field_id"], "probes.field_id")
        if fid not in fields:
            raise ValueError(f"probes 引用了未知田块: {fid}")
        when = parse_day(row["date"])
        if when not in date_index:
            raise ValueError("probes 日期超出气象窗口")
        key = (fid, date_index[when])
        if key in probes:
            raise ValueError("同一田块日最多一条 probes")
        lower = _canonical_fraction(row["min_closing_mm"], "min_closing_mm")
        upper = _canonical_fraction(row["max_closing_mm"], "max_closing_mm")
        capacity = fields[fid]["capacity_mm"]
        if not 0 <= lower <= upper <= capacity:
            raise ValueError(
                "probes 必须满足 0 ≤ 下界 ≤ 上界 ≤ 该田块容量")
        probes[key] = (lower, upper)
    return probes


def _validate_checkable(rows, date_index):
    result = set()
    for value in _array(rows, "checkable_rain_dates"):
        when = parse_day(value)
        if when not in date_index:
            raise ValueError("checkable_rain_dates 日期必须在气象窗口内")
        index = date_index[when]
        if index in result:
            raise ValueError("checkable_rain_dates 日期不得重复")
        result.add(index)
    return result


# ---------------------------------------------------------------------------
# 完整可能历史枚举
# ---------------------------------------------------------------------------

def _enumerate_histories(fields, days, candidates, ets, applications,
                         windows, probes):
    """返回 (history_count, ranges, rain_sets, advice_sets, records)。

    ``ranges`` 以 ``(day_index, field_id)`` 为键保存四项指标的最小/最大；
    ``records`` 保存每条完整历史的（逐日雨量, 末日建议）用于追查单。
    """
    field_ids = sorted(fields)
    field_params = [fields[fid] for fid in field_ids]
    field_count = len(field_ids)
    day_count = len(days)

    gross = [
        [applications.get((fid, when), ZERO) for when in days]
        for fid in field_ids
    ]

    ranges = {}
    rain_sets = [set() for _ in range(day_count)]
    advice_sets = {fid: set() for fid in field_ids}
    records = []
    count = 0

    # 分段合计的可行性前缀界，用于在枚举中途剪枝。
    prefix_min = [0]
    prefix_max = [0]
    for values in candidates:
        prefix_min.append(prefix_min[-1] + values[0])
        prefix_max.append(prefix_max[-1] + values[-1])

    window_partials = [0] * len(windows)
    transition_cache = {}

    def transition(fi, di, opening, rain):
        key = (fi, di, opening, rain)
        cached = transition_cache.get(key)
        if cached is not None:
            return cached
        result = step(
            field_params[fi], opening, Fraction(rain), gross[fi][di],
            ets[di],
        )
        cached = (
            result["closing_mm"], result["unmet_et_mm"],
            result["runoff_mm"], result["drainage_mm"],
            result["advised_next_gross_mm"],
        )
        transition_cache[key] = cached
        return cached

    def accept(path, rain_sequence):
        nonlocal count
        count += 1
        for di in range(day_count):
            rain_sets[di].add(rain_sequence[di])
            for fi, fid in enumerate(field_ids):
                closing, unmet, runoff, drainage, _advised = path[di][fi]
                key = (di, fid)
                slot = ranges.get(key)
                if slot is None:
                    ranges[key] = [
                        closing, closing, unmet, unmet,
                        runoff, runoff, drainage, drainage,
                    ]
                else:
                    for offset, value in ((0, closing), (2, unmet),
                                          (4, runoff), (6, drainage)):
                        if value < slot[offset]:
                            slot[offset] = value
                        if value > slot[offset + 1]:
                            slot[offset + 1] = value
        advices = tuple(entry[4] for entry in path[-1])
        for fi, fid in enumerate(field_ids):
            advice_sets[fid].add(advices[fi])
        records.append((tuple(rain_sequence), advices))

    initial_openings = tuple(
        Fraction(params["opening_mm"]) for params in field_params)
    path = []
    rain_sequence = []

    def dfs(di, openings):
        if di == day_count:
            accept(path, rain_sequence)
            return
        for rain in candidates[di]:
            next_openings = []
            results = []
            feasible = True
            for fi in range(field_count):
                result = transition(fi, di, openings[fi], rain)
                probe = probes.get((field_ids[fi], di))
                if probe is not None:
                    closing = result[0]
                    if closing < probe[0] or closing > probe[1]:
                        feasible = False
                        break
                next_openings.append(result[0])
                results.append(result)
            if not feasible:
                continue

            touched = []
            windows_ok = True
            for wi, (start, end, total) in enumerate(windows):
                if start <= di <= end:
                    window_partials[wi] += rain
                    touched.append(wi)
                    partial = window_partials[wi]
                    if partial > total:
                        windows_ok = False
                        break
                    if di == end:
                        if partial != total:
                            windows_ok = False
                            break
                    else:
                        remaining_min = prefix_min[end + 1] - prefix_min[di + 1]
                        remaining_max = prefix_max[end + 1] - prefix_max[di + 1]
                        if (partial + remaining_max < total
                                or partial + remaining_min > total):
                            windows_ok = False
                            break
            if windows_ok:
                path.append(results)
                rain_sequence.append(rain)
                dfs(di + 1, tuple(next_openings))
                rain_sequence.pop()
                path.pop()
            for wi in touched:
                window_partials[wi] -= rain

    dfs(0, initial_openings)
    return count, ranges, rain_sets, advice_sets, records


# ---------------------------------------------------------------------------
# 报告拼装
# ---------------------------------------------------------------------------

def _advice_rows(field_ids, advice_sets):
    rows = []
    for fid in field_ids:
        values = sorted(advice_sets[fid])
        rows.append({
            "field_id": fid,
            "possible_gross_mm": values,
            "released_gross_mm": values[0] if len(values) == 1 else None,
        })
    return rows


def _range_object(lower, upper):
    return {"min": str(lower), "max": str(upper)}


def _follow_up(field_ids, advice_sets, rain_sets, records, checkable,
               day_isos):
    """按契约选择唯一的单日追查单；无可改善日期时返回 None。"""
    pending = [fid for fid in field_ids if len(advice_sets[fid]) > 1]
    if not pending:
        return None

    candidates = []
    for day_index in sorted(checkable):
        values = sorted(rain_sets[day_index])
        if len(values) < 2:
            continue

        outcomes = []
        improved_any = False
        withheld_counts = []
        for value in values:
            branch_sets = {fid: set() for fid in field_ids}
            branch_count = 0
            for rain_sequence, advices in records:
                if rain_sequence[day_index] != value:
                    continue
                branch_count += 1
                for fi, fid in enumerate(field_ids):
                    branch_sets[fid].add(advices[fi])
            withheld = 0
            for fid in field_ids:
                if len(branch_sets[fid]) > 1:
                    withheld += 1
                elif len(advice_sets[fid]) > 1:
                    improved_any = True
            withheld_counts.append(withheld)
            outcomes.append((value, branch_count, branch_sets))

        if not improved_any:
            continue
        candidates.append({
            "date_index": day_index,
            "worst": max(withheld_counts),
            "best": min(withheld_counts),
            "outcomes": outcomes,
        })

    if not candidates:
        return None

    chosen = min(candidates, key=lambda item: (
        item["worst"], item["best"], item["date_index"]))
    return {
        "date": day_isos[chosen["date_index"]],
        "worst_withheld_count": chosen["worst"],
        "best_withheld_count": chosen["best"],
        "outcomes": [
            {
                "rain_mm": value,
                "history_count": branch_count,
                "next_advice": _advice_rows(field_ids, branch_sets),
            }
            for value, branch_count, branch_sets in chosen["outcomes"]
        ],
    }


def run(data):  # noqa: A001 - 与既有 service.run / reuse_water.run 同名公开接口
    _object(data, "输入根对象")
    _exact_keys(data, ROOT_KEYS, "输入根对象")

    fields = _validate_fields(data["fields"])
    days, candidates, ets = _validate_weather(data["weather"])
    date_index = {when: index for index, when in enumerate(days)}
    applications = _validate_applications(
        data["applications"], fields, date_index)
    windows = _validate_windows(data["rain_windows"], date_index)
    probes = _validate_probes(data["probes"], fields, date_index)
    checkable = _validate_checkable(
        data["checkable_rain_dates"], date_index)

    field_ids = sorted(fields)
    day_isos = [when.isoformat() for when in days]

    count, ranges, rain_sets, advice_sets, records = _enumerate_histories(
        fields, days, candidates, ets, applications, windows, probes)

    if count == 0:
        return {
            "schema_version": 1,
            "status": "inconsistent",
            "history_count": 0,
            "reason": (
                "现有分段雨量合计、实灌记录与探头日终储水界限共同不允许"
                "任何逐日雨量序列；历史水账无法确定，不签发供水建议"
            ),
        }

    field_daily = []
    for di, iso in enumerate(day_isos):
        for fid in field_ids:
            slot = ranges[(di, fid)]
            field_daily.append({
                "date": iso,
                "field_id": fid,
                "closing_mm": _range_object(slot[0], slot[1]),
                "unmet_et_mm": _range_object(slot[2], slot[3]),
                "runoff_mm": _range_object(slot[4], slot[5]),
                "drainage_mm": _range_object(slot[6], slot[7]),
            })

    rain_daily = [
        {"date": iso, "possible_rain_mm": sorted(rain_sets[di])}
        for di, iso in enumerate(day_isos)
    ]

    return {
        "schema_version": 1,
        "status": "consistent",
        "history_count": count,
        "rain_daily": rain_daily,
        "field_daily": field_daily,
        "next_advice": _advice_rows(field_ids, advice_sets),
        "follow_up": _follow_up(
            field_ids, advice_sets, rain_sets, records, checkable,
            day_isos),
    }
