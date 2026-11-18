import json
import unittest
from unittest.mock import patch

from server.fan_control import DemoFanController, control_metadata


CONFIG = {"DEVICE_METADATA": {"localhost": {
    "platform": "x86_64-cel_questone_2-r0", "hwsku": "Questone_2"}}}


class FanControlTests(unittest.TestCase):
    def test_live_capability_never_claims_mode_or_safe_duty(self):
        data = control_metadata(CONFIG)
        self.assertFalse(data["write_supported"])
        self.assertFalse(data["manual_available"])
        self.assertTrue(data["reason"])
        self.assertFalse(data["protection"]["active"])
        self.assertEqual(data["mode"], "unknown")
        self.assertEqual(data["available_modes"], [])
        for field in ("duty_percent", "min_percent", "max_percent", "step_percent"):
            self.assertIsNone(data[field])
        json.dumps(data)

    def test_unknown_platform_stays_unknown(self):
        for config in ({}, None, {"DEVICE_METADATA": None}, {"DEVICE_METADATA": {"localhost": None}}, {"DEVICE_METADATA": {"localhost": {"platform": "other"}}}):
            data = control_metadata(config)
            self.assertEqual(data["scope"], "unknown")
            self.assertFalse(data["write_supported"])

    def test_demo_opt_in_is_strict(self):
        for value in (False, None, "true", 1):
            with self.assertRaises(ValueError):
                DemoFanController(demo=value)
        with self.assertRaises(TypeError):
            control_metadata(CONFIG, demo="false")

    def test_demo_is_explicit_and_does_not_use_host(self):
        with patch("subprocess.run", side_effect=AssertionError("host access")), patch("builtins.open", side_effect=AssertionError("file access")):
            fan = DemoFanController(demo=True)
            self.assertTrue(fan.metadata()["simulated"])
            self.assertEqual(fan.apply({"mode": "fixed", "duty_percent": 42})["duty_percent"], 42)
            self.assertEqual(fan.apply({"mode": "auto"})["mode"], "auto")
            self.assertIsNone(fan.metadata()["duty_percent"])

    def test_invalid_fixed_inputs_and_extra_keys_rejected(self):
        fan = DemoFanController(demo=True)
        for duty in (None, True, 19, 101, 42.5, "42", float("nan")):
            with self.assertRaises(ValueError):
                fan.apply({"mode": "fixed", "duty_percent": duty})
        for values in ({}, [], {"mode": "shell"}, {"mode": "auto", "duty_percent": 35}, {"mode": "fixed", "duty_percent": 35, "command": "bad"}):
            with self.assertRaises(ValueError):
                fan.apply(values)
        self.assertEqual(fan.metadata()["mode"], "auto")

    def test_metadata_cannot_mutate_controller(self):
        fan = DemoFanController(demo=True)
        data = fan.apply({"mode": "fixed", "duty_percent": 40})
        data["mode"] = "auto"
        data["protection"]["active"] = True
        self.assertEqual(fan.metadata()["mode"], "fixed")
        self.assertFalse(fan.metadata()["protection"]["active"])

    def test_close_resets_simulation_and_disables_future_changes(self):
        fan = DemoFanController(demo=True)
        fan.apply({"mode": "fixed", "duty_percent": 40})
        fan.close()
        fan.close()
        self.assertEqual(fan.metadata()["mode"], "auto")
        self.assertFalse(fan.metadata()["write_supported"])
        with self.assertRaises(RuntimeError):
            fan.apply({"mode": "auto"})


if __name__ == "__main__":
    unittest.main()
