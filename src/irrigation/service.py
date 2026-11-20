from fractions import Fraction
from .model import validate, day
from .balance import step


def run(data):
    fields, weather, applications = validate(data)
    rows = []
    supply_litres = 0
    stressed_days = 0
    for field_id, field in sorted(fields.items()):
        opening = Fraction(field["opening_mm"])
        for observed in weather:
            gross = applications.get((field_id, day(observed["date"])), 0)
            result = step(field, opening, observed["rain_mm"], gross, observed["et_mm"])
            supply_litres += gross * field["area_m2"]
            stressed_days += result["unmet_et_mm"] > 0
            opening = result["closing_mm"]
            rows.append({"field_id": field_id, "date": observed["date"],
                         **{k: str(v) if isinstance(v, Fraction) else v for k, v in result.items()}})
    return {"schema_version": 1, "daily": rows,
            "summary": {"field_count": len(fields), "field_days": len(rows),
                        "gross_supply_litres": supply_litres, "water_stress_days": stressed_days}}
