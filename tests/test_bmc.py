"""No BMC/device access; IPMI output fixtures and bounded local child processes."""
import concurrent.futures
import copy
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from server.bmc import BmcMonitor, BmcReadError, COMMANDS, DEMO


class FixtureMonitor(BmcMonitor):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.outputs = copy.deepcopy(DEMO)
        self.failures = {}

    def _execute(self, command_id, timeout):
        self.calls.append((command_id, timeout))
        if command_id in self.failures:
            raise self.failures[command_id]
        return self.outputs[command_id]


class BmcTests(unittest.TestCase):
    def test_live_fixture_identity_and_console_are_observations_only(self):
        monitor = FixtureMonitor()
        monitor.outputs["identity"] = "Manufacturer ID: 12290\nManufacturer Name: Celestica International Inc.\nProduct ID: 1156 (0x0484)\nFirmware Revision: 0.32\nIPMI Version: 2.0\nDevice Available: yes\n"
        data = monitor.snapshot()
        self.assertEqual("live", data["mode"])
        self.assertEqual(7, len(monitor.calls))
        self.assertEqual("0.32", next(r["value"] for r in data["identity"]["rows"] if r["field"] == "BMC 固件版本"))
        self.assertTrue(data["console"]["supported"])
        self.assertFalse(data["console"]["available"])
        self.assertFalse(data["console"]["session_verified"])
        self.assertFalse(data["network"]["configured"])
        self.assertIn("IPv4 未配置", data["console"]["reason"])
        self.assertIn("不表示不支持加密", json.dumps(data["console"], ensure_ascii=False))
        self.assertEqual("ok", data["status"]["state"])
        self.assertIn("读取成功", data["collection"]["state_semantics"])

    def test_demo_is_explicit_and_never_executes_host_commands(self):
        for value in ("false", "true", 1, None):
            with self.assertRaises(TypeError):
                BmcMonitor(demo=value)
        with patch("server.bmc.subprocess.Popen", side_effect=AssertionError("host command")):
            snapshot = BmcMonitor(demo=True).snapshot()
        self.assertEqual("demo", snapshot["mode"])
        self.assertTrue(snapshot["collection"]["simulated"])
        self.assertIn("DEMO", snapshot["collection"]["notice"])

    def test_network_allowlist_discards_credentials_unknown_fields_and_bad_addresses(self):
        monitor = FixtureMonitor()
        monitor.outputs["network"] += "SNMP Community String: community-value\nPassword: password-value\nUsername: admin-private\nRMCP+ Cipher Suites: do-not-return\nPrivate Key: private-value\nUnknown: secret-value\nDefault Gateway IP: 192.0.2.1 password=hidden\n"
        data = monitor.snapshot()
        text = json.dumps(data)
        for secret in ("community-value", "password-value", "admin-private", "do-not-return", "private-value", "secret-value", "hidden"):
            self.assertNotIn(secret, text)
        self.assertNotIn("stdout", text)
        self.assertNotIn("stderr", text)
        self.assertTrue(data["network"]["rows"])

    def test_failed_and_unknown_sections_never_fall_back_to_demo_or_healthy(self):
        monitor = FixtureMonitor()
        monitor.failures = {k: BmcReadError("timeout") for k in COMMANDS}
        data = monitor.snapshot()
        for section in ("identity", "status", "network", "console", "events"):
            self.assertEqual("unknown", data[section]["state"])
            self.assertEqual([], data[section]["rows"])
            self.assertTrue(data[section]["error"])
        self.assertIsNone(data["console"]["supported"])
        self.assertFalse(data["console"]["available"])
        self.assertEqual("live", data["mode"])
        self.assertFalse(data["collection"]["simulated"])

    def test_partial_failure_preserves_other_real_sections_and_hides_exception_text(self):
        monitor = FixtureMonitor()
        monitor.failures["selftest"] = RuntimeError("password=do-not-leak stderr")
        monitor.outputs["identity"] = "unrecognized response"
        data = monitor.snapshot()
        self.assertEqual("unknown", data["identity"]["state"])
        self.assertEqual("partial", data["status"]["state"])
        self.assertTrue(data["status"]["rows"])
        self.assertEqual("ok", data["network"]["state"])
        self.assertNotIn("do-not-leak", json.dumps(data))

    def test_cache_includes_failures_and_returns_independent_copies(self):
        monitor = FixtureMonitor()
        monitor.failures["network"] = BmcReadError("failed")
        first = monitor.snapshot()
        first["identity"]["rows"].clear()
        again = monitor.snapshot()
        self.assertEqual(7, len(monitor.calls))
        self.assertTrue(again["identity"]["rows"])
        self.assertTrue(again["collection"]["cached"])
        self.assertEqual(first["collection"]["sampled_at"], again["collection"]["sampled_at"])
        monitor._cached_at -= 31
        monitor.snapshot()
        self.assertEqual(14, len(monitor.calls))

    def test_concurrent_first_read_collects_once_not_once_per_waiter(self):
        monitor = FixtureMonitor()
        execute = monitor._execute
        started = threading.Barrier(8)
        def slow(command_id, timeout):
            time.sleep(0.01)
            return execute(command_id, timeout)
        monitor._execute = slow
        def read():
            started.wait(timeout=2)
            return monitor.snapshot()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: read(), range(8)))
        self.assertEqual(7, len(monitor.calls))
        self.assertEqual(1, sum(not r["collection"]["cached"] for r in results))

    def test_total_budget_stops_remaining_commands_and_marks_unknown(self):
        monitor = FixtureMonitor()
        monitor.COLLECTION_SECONDS = 0.04
        original = monitor._execute
        def slow(command_id, timeout):
            time.sleep(0.03)
            return original(command_id, timeout)
        monitor._execute = slow
        data = monitor.snapshot()
        self.assertEqual(2, len(monitor.calls))
        self.assertTrue(all(t <= 0.04 for _, t in monitor.calls))
        self.assertEqual("unknown", data["events"]["state"])
        self.assertTrue(any("总时限" in c["error"] for c in data["collection"]["commands"] if c["error"]))

    def test_sel_has_twenty_seconds_but_cannot_exceed_collection_deadline(self):
        monitor = FixtureMonitor()
        monitor.snapshot()
        self.assertEqual(20, dict(monitor.calls)["events"])
        self.assertTrue(all(timeout == 5 for command, timeout in monitor.calls if command != "events"))
        monitor = FixtureMonitor()
        clock = [100.0]
        original = monitor._execute
        def consume_budget(command_id, timeout):
            value = original(command_id, timeout)
            clock[0] += timeout if command_id == "events" else 2.0
            return value
        monitor._execute = consume_budget
        with patch("server.bmc.time.monotonic", side_effect=lambda: clock[0]):
            result = monitor.snapshot()
        self.assertEqual(13, dict(monitor.calls)["events"])
        self.assertEqual(25000, result["collection"]["elapsed_ms"])
        self.assertEqual(30, result["collection"]["cache_seconds"])

    def test_recent_events_are_bounded_and_empty_description_is_not_a_cpu_fault(self):
        monitor = FixtureMonitor()
        monitor.outputs["events"] = "\n".join(f"{i:x} | 11/18/2026 | 02:50:18 PM UTC | Processor #0x1c |  | Asserted" for i in range(40))
        data = monitor.snapshot()
        self.assertEqual(30, len(data["events"]["rows"]))
        self.assertTrue(all(r["event"] == "未提供事件描述" for r in data["events"]["rows"]))
        self.assertTrue(all("severity" not in r for r in data["events"]["rows"]))
        self.assertIn("不直接代表当前", data["events"]["reason"])

    def test_plain_sel_list_preserves_sensor_id_without_inventing_sdr_detail(self):
        monitor = FixtureMonitor()
        monitor.outputs["events"] = " 2b9 | 11/18/2026 | 02:33:26 PM UTC | Voltage #0x45 | Lower Critical going low | Deasserted\n"
        result = monitor.snapshot()["events"]
        self.assertEqual("ok", result["state"])
        self.assertEqual("Voltage #0x45", result["rows"][0]["sensor"])
        self.assertEqual("", result["rows"][0]["detail"])
        self.assertEqual("Deasserted", result["rows"][0]["direction"])
        self.assertIn("未额外解析 SDR 名称", result["reason"])

    def test_empty_sel_is_distinguished_from_unrecognized_response(self):
        monitor = FixtureMonitor()
        monitor.outputs["events"] = "SEL has no entries"
        self.assertEqual({"state": "ok", "rows": [], "error": None, "limit": 30}, monitor.snapshot()["events"])
        monitor = FixtureMonitor()
        monitor.outputs["events"] = "unrecognized device error"
        self.assertEqual("unknown", monitor.snapshot()["events"]["state"])

    def test_fixed_commands_never_activate_sol_modify_network_or_list_accounts(self):
        self.assertEqual(("sol", "info", "1"), COMMANDS["sol_info"][1])
        self.assertEqual(("sel", "list", "last", "30"), COMMANDS["events"][1])
        for section, argv in COMMANDS.values():
            self.assertFalse({"activate", "deactivate", "set", "reset", "clear", "user", "password", "raw"}.intersection(argv))
        with patch("server.bmc.subprocess.Popen", side_effect=AssertionError("should not run")):
            with self.assertRaises(BmcReadError):
                BmcMonitor()._execute("sol activate", 1)

    def run_child(self, code, callback):
        real_popen, processes, seen = subprocess.Popen, [], []
        def child(argv, **kwargs):
            seen.append((argv, kwargs))
            process = real_popen([sys.executable, "-c", code], **kwargs)
            processes.append(process)
            return process
        with patch("server.bmc.subprocess.Popen", side_effect=child):
            callback(BmcMonitor())
        self.assertEqual(["ipmitool", "-I", "open", "mc", "info"], seen[0][0])
        self.assertIs(seen[0][1]["shell"], False)
        self.assertEqual(os.name == "posix", seen[0][1]["start_new_session"])
        self.assertTrue(all(p.poll() is not None for p in processes))

    def test_actual_subprocess_timeout_terminates_process_without_returning_raw_output(self):
        def check(monitor):
            started = time.monotonic()
            with self.assertRaises(BmcReadError) as error:
                monitor._execute("identity", 0.05)
            self.assertEqual("timeout", error.exception.code)
            self.assertLess(time.monotonic() - started, 3)
        self.run_child("import time; time.sleep(10)", check)

    def test_actual_subprocess_output_is_bounded(self):
        def check(monitor):
            with self.assertRaises(BmcReadError) as error:
                monitor._execute("identity", 2)
            self.assertEqual("output_limit", error.exception.code)
        self.run_child("import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(2)", check)

    def test_nonzero_exit_does_not_expose_stderr(self):
        def check(monitor):
            with self.assertRaises(BmcReadError) as error:
                monitor._execute("identity", 2)
            self.assertEqual("failed", error.exception.code)
            self.assertNotIn("private-password", str(error.exception))
        self.run_child("import sys; sys.stderr.write('private-password'); sys.exit(1)", check)


if __name__ == "__main__":
    unittest.main()
