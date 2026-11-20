"""Water conservation and advisory calculation without automatic irrigation."""
from fractions import Fraction


def step(field, opening, rain, irrigation, et):
    efficiency = Fraction(field["efficiency_permille"], 1000)
    delivered = irrigation * efficiency
    delivery_loss = irrigation - delivered
    incoming = rain + delivered
    infiltrated = min(incoming, field["infiltration_mm"])
    runoff = incoming - infiltrated
    storage = opening + infiltrated
    drainage = max(Fraction(0), storage - field["capacity_mm"])
    available = storage - drainage
    actual_et = min(available, et)
    closing = available - actual_et
    shortfall = et - actual_et
    deficit = max(Fraction(0), field["target_mm"] - closing)
    gross_needed = deficit / efficiency if closing < field["threshold_mm"] else Fraction(0)
    advised = -(-gross_needed.numerator // gross_needed.denominator)
    residual = opening + rain + irrigation - (
        closing + runoff + drainage + actual_et + delivery_loss)
    return {
        "opening_mm": opening, "rain_mm": rain, "gross_irrigation_mm": irrigation,
        "delivery_loss_mm": delivery_loss, "runoff_mm": runoff, "drainage_mm": drainage,
        "actual_et_mm": actual_et, "unmet_et_mm": shortfall, "closing_mm": closing,
        "balance_residual_mm": residual, "advised_next_gross_mm": advised,
    }
