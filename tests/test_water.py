import copy
import json
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
import sys
from irrigation.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class WaterTests(unittest.TestCase):
    def test_three_day_conservation_and_advice(self):
        result = run(fixture())
        self.assertEqual(result["summary"], {"field_count": 1, "field_days": 3,
                         "gross_supply_litres": 25000, "water_stress_days": 0})
        a, b, c = result["daily"]
        self.assertEqual([r["closing_mm"] for r in [a, b, c]], ["30", "10", "50"])
        self.assertEqual(b["delivery_loss_mm"], "5")
        self.assertEqual(b["advised_next_gross_mm"], 63)
        self.assertEqual(c["runoff_mm"], "30")
        for row in result["daily"]:
            incoming = sum(Fraction(row[k]) for k in ("opening_mm", "rain_mm", "gross_irrigation_mm"))
            outgoing = sum(Fraction(row[k]) for k in ("closing_mm", "delivery_loss_mm", "runoff_mm", "drainage_mm", "actual_et_mm"))
            self.assertEqual(incoming, outgoing)

    def test_fractional_delivery_and_et_shortfall(self):
        data = fixture()
        data["weather"] = [{"date": "2026-06-01", "rain_mm": 0, "et_mm": 50}]
        data["applications"] = [{"field_id": "north", "date": "2026-06-01", "gross_mm": 1}]
        row = run(data)["daily"][0]
        self.assertEqual(row["closing_mm"], "0")
        self.assertEqual(row["unmet_et_mm"], "46/5")
        self.assertEqual(row["delivery_loss_mm"], "1/5")

    def test_capacity_drains_before_evaporation(self):
        data = fixture()
        data["fields"][0].update(opening_mm=90, infiltration_mm=100)
        data["weather"] = [{"date": "2026-06-01", "rain_mm": 50, "et_mm": 10}]
        data["applications"] = []
        row = run(data)["daily"][0]
        self.assertEqual(row["drainage_mm"], "40")
        self.assertEqual(row["closing_mm"], "90")

    def test_rejects_missing_day_and_unknown_application(self):
        data = fixture()
        data["weather"].pop(1)
        with self.assertRaisesRegex(ValueError, "consecutive"):
            run(data)
        data = fixture()
        data["applications"][0]["field_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown"):
            run(data)

    def test_cli_preserves_existing_output_on_error(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            output.write_text("KEEP")
            r = subprocess.run([sys.executable, "-m", "irrigation.cli", "--source",
                "fixtures/input.json", "--output", str(output)], capture_output=True)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(output.read_text(), "KEEP")
