"""Validated daily field facts, expressed in mm and square metres."""
from datetime import date, timedelta
from .io import integer, unique


def day(value):
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return parsed


def validate(data):
    fields = unique(data["fields"], "field")
    weather = data["weather"]
    if not fields or len(fields) > 200 or not isinstance(weather, list) or not 1 <= len(weather) <= 3660:
        raise ValueError("require 1..200 fields and 1..3660 consecutive weather days")
    days = []
    for row in weather:
        when = day(row["date"])
        if days and when != days[-1] + timedelta(days=1):
            raise ValueError("weather days must be consecutive and ordered")
        days.append(when)
        integer(row["rain_mm"], "rain_mm")
        integer(row["et_mm"], "et_mm")
    for field in fields.values():
        integer(field["area_m2"], "area_m2", 1)
        capacity = integer(field["capacity_mm"], "capacity_mm", 1)
        opening = integer(field["opening_mm"], "opening_mm")
        threshold = integer(field["threshold_mm"], "threshold_mm")
        target = integer(field["target_mm"], "target_mm")
        integer(field["infiltration_mm"], "infiltration_mm", 1)
        efficiency = integer(field["efficiency_permille"], "efficiency_permille", 1)
        if not 0 <= threshold <= target <= capacity or opening > capacity or efficiency > 1000:
            raise ValueError("invalid storage limits or efficiency")
    applications = {}
    for row in data["applications"]:
        key = (row["field_id"], day(row["date"]))
        if key[0] not in fields or key[1] not in days:
            raise ValueError("application references unknown field or weather day")
        if key in applications:
            raise ValueError("duplicate field/day application")
        applications[key] = integer(row["gross_mm"], "gross_mm")
    return fields, weather, applications
