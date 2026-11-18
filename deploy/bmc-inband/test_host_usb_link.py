"""Local mock tests only. No real iproute2 invocation or switch access."""
import copy
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from host_usb_link import Conflict, LinkManager, TransportHealth, validate_config


PROFILE = json.loads(Path(__file__).with_name("questone2-usb-profile.example.json").read_text())
PROFILE.update(serial="TEST_USB_SERIAL", usb_path="1-9.9", transport_recovery="questone2-bmc-0.32-cdc")


class FakeManager(LinkManager):
    def __init__(self, directory):
        super().__init__(PROFILE, directory, proc_root=Path(directory) / "proc")
        path = self.proc / "sys/net/ipv4/conf/all/rp_filter"
        path.parent.mkdir(parents=True)
        path.write_text("0")
        self.current = {"ifname": "usb0", "ifindex": "7", "mac": "02:00:00:00:00:01",
                        "usb_path": "1-9.9", "devnum": "4", "boot_id": "test-boot"}
        self.rules = [{"priority": 0, "src": "all", "table": "local"},
                      {"priority": 32766, "src": "all", "table": "main"},
                      {"priority": 32767, "src": "all", "table": "default"}]
        self.routes = []
        self.links = [{"ifname": "usb0", "flags": ["BROADCAST", "MULTICAST"], "addr_info": []}]
        self.values = {k: "1" for k in self.SYSCTLS}
        self.calls = []
        self.fail = None

    def discover(self):
        return copy.deepcopy(self.current)

    def snapshot(self):
        return copy.deepcopy({"rules": self.rules, "routes": self.routes, "links": self.links})

    def sysctl_read(self, ifname, key):
        return self.values[key]

    def sysctl_write(self, ifname, key, value):
        self.calls.append(["sysctl", ifname, key, value])
        self.values[key] = value

    def command(self, args, json_result=False, allow_error=False):
        self.calls.append(args)
        if self.fail and args[:len(self.fail)] == self.fail:
            raise Conflict("injected command failure")
        if args == ["-4", "-j", "rule", "show"]:
            return copy.deepcopy(self.rules)
        if args[:4] == ["-4", "-j", "route", "get"]:
            if not self.routes:
                raise Conflict("unreachable")
            return [{"dev": self.current["ifname"], "prefsrc": PROFILE["host_address"]}]
        if args[:2] == ["-4", "rule"]:
            action = args[2]
            priority = int(args[4])
            kind = "deny" if priority == PROFILE["rule_priority"] + 1 else "local"
            if action == "add":
                rule = {"priority": priority, "src": "all", "dst": PROFILE["peer_address"]}
                if kind == "deny":
                    rule["action"] = "unreachable"
                else:
                    rule.update(iif="lo", table=PROFILE["route_table"])
                self.rules.append(rule)
            else:
                self.rules = [r for r in self.rules if r["priority"] != priority]
        elif args[:2] == ["-4", "route"]:
            if args[2] == "add":
                self.routes.append({"dst": PROFILE["peer_address"], "dev": self.current["ifname"],
                                    "scope": "link", "prefsrc": PROFILE["host_address"],
                                    "protocol": "static", "table": PROFILE["route_table"]})
            else:
                self.routes = []
        elif args[:2] == ["-4", "address"]:
            if args[2] == "add":
                self.links[0]["addr_info"].append({"family": "inet", "local": PROFILE["host_address"], "prefixlen": 32})
            else:
                self.links[0]["addr_info"] = []
        elif args[:2] == ["link", "set"]:
            if args[-1] == "up":
                self.links[0]["flags"].append("UP")
            else:
                self.links[0]["flags"] = [x for x in self.links[0]["flags"] if x != "UP"]
        else:
            raise AssertionError("Unexpected command: " + repr(args))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = FakeManager(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_order_scoped_commands_and_cleanup(self):
        result = self.m.reconcile()
        self.assertEqual(result["state"], "configured")
        writes = [c for c in self.m.calls if c[:2] != ["-4", "-j"]]
        self.assertEqual(writes[0], self.m.rule_args("deny"))
        self.assertEqual(writes[1], self.m.rule_args("local"))
        self.assertTrue(all("default" not in x and "flush" not in x and "replace" not in x for x in writes))
        self.assertIn("noprefixroute", next(x for x in writes if x[:3] == ["-4", "address", "add"]))
        self.assertTrue(all(value == "0" for value in self.m.values.values()))
        self.m.cleanup()
        self.assertEqual(len(self.m.rules), 3)
        self.assertEqual(self.m.routes, [])
        self.assertEqual(self.m.links[0]["addr_info"], [])
        self.assertNotIn("UP", self.m.links[0]["flags"])
        self.assertTrue(all(value == "1" for value in self.m.values.values()))
        self.assertFalse(self.m.state_path.exists())

    def test_second_poll_does_not_write(self):
        self.m.reconcile()
        self.m.calls = []
        self.m.reconcile()
        self.assertEqual(self.m.calls, [["-4", "-j", "route", "get", "169.254.100.1"]])

    def test_readonly_preflight_does_not_write(self):
        self.m.preflight(self.m.discover(), self.m.snapshot())
        self.assertEqual(self.m.calls, [])
        self.assertFalse(self.m.state_path.exists())

    def test_renumeration_recreates_only_owned_resources(self):
        self.m.reconcile()
        self.m.current = None
        self.m.links = []
        self.m.routes = []  # The kernel removed routes when the USB function disappeared.
        self.assertEqual(self.m.reconcile()["state"], "waiting_for_usb")
        self.assertEqual(len(self.m.rules), 5)  # Exact peer deny remains during absence.
        self.m.current = {"ifname": "enx020000000001", "ifindex": "9", "mac": "02:00:00:00:00:01",
                          "usb_path": "1-9.9", "devnum": "5", "boot_id": "test-boot"}
        self.m.links = [{"ifname": self.m.current["ifname"], "flags": [], "addr_info": []}]
        self.m.values = {k: "1" for k in self.m.SYSCTLS}
        self.assertEqual(self.m.reconcile()["interface"], "enx020000000001")
        self.assertEqual(self.m.routes[0]["dev"], "enx020000000001")

    def test_journal_recovers_after_process_restart(self):
        self.m.reconcile()
        recovered = FakeManager.__new__(FakeManager)
        recovered.__dict__ = dict(self.m.__dict__)
        recovered.state = {}
        recovered.load()
        recovered.cleanup()
        self.assertFalse(recovered.state_path.exists())
        self.assertEqual(recovered.routes, [])

    def test_rule_conflict_refused_before_writes(self):
        self.m.rules.append({"priority": 17171, "src": "all", "table": "main"})
        with self.assertRaisesRegex(Conflict, "priority"):
            self.m.reconcile()
        self.assertEqual(self.m.calls, [])

    def test_earlier_policy_requires_review(self):
        self.m.rules.append({"priority": 1000, "src": "all", "table": 1000})
        with self.assertRaisesRegex(Conflict, "earlier"):
            self.m.reconcile()
        self.assertEqual(self.m.calls, [])

    def test_sonic_unfiltered_local_rule_priority_1001_allowed(self):
        self.m.rules[0] = {"priority": 1001, "src": "all", "table": "local"}
        self.assertEqual(self.m.reconcile()["state"], "configured")
        self.m.cleanup()
        self.assertIn({"priority": 1001, "src": "all", "table": "local"}, self.m.rules)

    def test_pure_local_rule_accepts_numeric_table_but_no_filters_or_actions(self):
        for priority in (0, 1, 1001, 9999, 32001):
            for table in ("local", 255, "255"):
                self.assertTrue(self.m.pure_local_rule({"priority": priority, "src": "all", "table": table}))
        for extra in ({"dst": "169.254.100.1"}, {"iif": "lo"}, {"action": "goto"},
                      {"action": "to_tbl"}, {"fwmark": "0x1"}, {"src": "192.0.2.0/24"},
                      {"flags": ["not"]}):
            for priority in (0, 1001):
                self.m.rules[0] = {"priority": priority, "src": "all", "table": "local", **extra}
                with self.subTest(extra=extra, priority=priority), self.assertRaisesRegex(Conflict, "earlier"):
                    self.m.preflight(self.m.discover(), self.m.snapshot())

    def test_matching_local_table_peer_route_refused(self):
        self.m.rules[0] = {"priority": 1001, "src": "all", "table": "local"}
        for destination in ("169.254.100.1", "169.254.100.1/32", "169.254.0.0/16", "default"):
            self.m.routes = [{"dst": destination, "type": "local", "table": "local", "dev": "lo"}]
            with self.subTest(destination=destination), self.assertRaisesRegex(Conflict, "local-table"):
                self.m.reconcile()
        self.assertEqual(self.m.calls, [])

    def test_main_peer_route_and_other_address_refused(self):
        self.m.routes.append({"dst": "169.254.100.1", "dev": "usb0"})
        with self.assertRaisesRegex(Conflict, "outside"):
            self.m.reconcile()
        self.m.routes = []
        self.m.links[0]["addr_info"].append({"family": "inet", "local": "169.254.100.2", "prefixlen": 32})
        with self.assertRaisesRegex(Conflict, "pre-existing"):
            self.m.reconcile()

    def test_global_rpf_refused_without_change(self):
        (self.m.proc / "sys/net/ipv4/conf/all/rp_filter").write_text("1")
        with self.assertRaisesRegex(Conflict, "rp_filter"):
            self.m.reconcile()
        self.assertEqual(self.m.calls, [])

    def test_partial_failure_cleanup_uses_write_ahead_record(self):
        self.m.fail = ["-4", "route", "add"]
        with self.assertRaises(Conflict):
            self.m.reconcile()
        self.assertTrue(self.m.state["device"]["route"])
        self.m.fail = None
        self.m.cleanup()
        self.assertEqual(self.m.links[0]["addr_info"], [])
        self.assertEqual(len(self.m.rules), 3)

    def test_cleanup_does_not_delete_external_route_or_rule(self):
        self.m.reconcile()
        self.m.routes[0]["prefsrc"] = "169.254.0.99"
        with self.assertRaisesRegex(Conflict, "altered"):
            self.m.cleanup()
        self.assertEqual(self.m.routes[0]["prefsrc"], "169.254.0.99")
        self.assertEqual(len(self.m.rules), 5)

    def test_cleanup_never_touches_reused_interface_name(self):
        self.m.reconcile()
        self.m.routes = []
        self.m.current["devnum"] = "6"
        self.m.current["ifindex"] = "11"
        self.m.calls = []
        self.m.cleanup_device()
        self.assertEqual(self.m.calls, [])
        self.assertEqual(len(self.m.links[0]["addr_info"]), 1)

    def test_original_up_is_preserved(self):
        self.m.links[0]["flags"].append("UP")
        self.m.reconcile()
        self.m.cleanup()
        self.assertIn("UP", self.m.links[0]["flags"])

    def test_cleanup_leaves_external_address_link_unchanged(self):
        self.m.reconcile()
        self.m.links[0]["addr_info"].append({"family": "inet", "local": "169.254.0.99", "prefixlen": 32})
        # Real ip address del removes only the requested address; simulate that detail here.
        original_command = self.m.command
        def command(args, *a, **kw):
            if args[:3] == ["-4", "address", "del"]:
                self.m.links[0]["addr_info"] = [x for x in self.m.links[0]["addr_info"] if x["local"] != PROFILE["host_address"]]
                return None
            return original_command(args, *a, **kw)
        self.m.command = command
        with self.assertRaisesRegex(Conflict, "unrelated"):
            self.m.cleanup()
        self.assertIn("UP", self.m.links[0]["flags"])
        self.assertEqual(self.m.links[0]["addr_info"][0]["local"], "169.254.0.99")

    def test_profile_rejects_unknown_fields_commands_and_addresses(self):
        for key, value in [("vendor", "046b;id"), ("usb_path", "../usb0"),
                           ("host_address", "192.0.2.18"), ("route_table", 254),
                           ("rule_priority", True), ("drivers", ["igb"]), ("shell", "id")]:
            c = dict(PROFILE)
            c[key] = value
            with self.subTest(key=key), self.assertRaises(Conflict):
                validate_config(c)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = FakeManager(self.tmp.name)
        boot = self.m.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("test-boot")
        self.result = self.m.reconcile()
        self.now = 100.0
        self.h = TransportHealth(self.m, lambda: self.now)
        self.h.probe = mock.Mock(return_value="connect_timeout")
        self.h.verified_node = mock.Mock(return_value=(Path("/dev/bus/usb/001/004"), 0))
        self.h.perform_reset = mock.Mock(return_value="reset_requested")

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, advance=10):
        self.now += advance
        return self.h.tick(self.result)

    def trigger(self):
        self.tick()
        self.tick()
        return self.tick()

    def test_only_third_connect_timeout_resets(self):
        self.assertEqual(self.tick()["consecutive_connect_timeouts"], 1)
        self.assertIsNone(self.tick(1))
        self.assertEqual(self.tick()["consecutive_connect_timeouts"], 2)
        self.assertEqual(self.tick()["recovery"], "reset_requested")
        self.h.perform_reset.assert_called_once()
        self.assertEqual(self.h.failures, 0)

    def test_refused_or_connected_service_issue_breaks_timeout_sequence(self):
        for status in ("port_refused_network_alive", "banner_timeout", "unexpected_banner", "service_closed", "socket_error_113", "ssh_banner_verified"):
            self.h.failures = 2
            self.h.probe.return_value = status
            with self.subTest(status=status):
                self.assertEqual(self.tick()["consecutive_connect_timeouts"], 0)
                self.h.perform_reset.assert_not_called()

    def test_rate_survives_restart_and_enforces_cooldown_and_hourly_limit(self):
        self.assertEqual(self.trigger()["recovery"], "reset_requested")
        previous = self.h
        self.h = TransportHealth(self.m, lambda: self.now)
        self.h.probe, self.h.verified_node, self.h.perform_reset = previous.probe, previous.verified_node, previous.perform_reset
        self.assertEqual(self.trigger()["recovery"], "cooldown")
        self.now += 300
        self.assertEqual(self.tick()["recovery"], "reset_requested")
        self.now += 300
        self.assertEqual(self.trigger()["recovery"], "reset_requested")
        self.now += 300
        self.assertEqual(self.trigger()["recovery"], "hourly_limit")
        self.assertEqual(self.h.perform_reset.call_count, 3)
        self.now += 3600
        self.assertEqual(self.tick()["recovery"], "reset_requested")

    def test_failures_are_counted_and_unknown_helper_blocks_future_resets(self):
        self.h.perform_reset.return_value = "reset_failed"
        self.assertEqual(self.trigger()["recovery"], "reset_failed")
        self.assertEqual(self.trigger()["recovery"], "cooldown")
        self.now += 300
        self.h.perform_reset.return_value = "reset_helper_stuck"
        self.assertEqual(self.tick()["recovery"], "reset_helper_stuck")
        self.now += 3601
        self.assertEqual(self.trigger()["recovery"], "blocked_previous_helper_stuck")
        self.assertEqual(self.h.perform_reset.call_count, 2)

    def test_identity_or_config_mismatch_never_resets(self):
        self.h.verified_node.side_effect = Conflict("configuration is not 2")
        self.assertEqual(self.trigger()["recovery"], "refused")
        self.h.perform_reset.assert_not_called()
        self.assertFalse(self.h.rate_path.exists())

    def test_journal_records_block_before_helper_and_crash_cannot_retry(self):
        def uncertain(_):
            record = json.loads(self.h.rate_path.read_text())
            self.assertTrue(record["blocked"])
            self.assertEqual(len(record["attempts"]), 1)
            raise OSError("uncertain reset start")
        self.h.perform_reset.side_effect = uncertain
        self.assertEqual(self.trigger()["recovery"], "refused")
        self.now += 3601
        self.assertEqual(self.tick()["recovery"], "blocked_previous_helper_stuck")

    def test_waiting_or_new_identity_clears_consecutive_failures(self):
        self.tick()
        self.tick()
        self.h.tick({"state": "waiting_for_usb"})
        self.assertEqual(self.h.failures, 0)
        self.assertEqual(self.tick()["consecutive_connect_timeouts"], 1)
        self.m.state["device"]["identity"]["ifindex"] = "11"
        self.assertEqual(self.tick()["consecutive_connect_timeouts"], 1)

    def test_disabled_by_default_and_recovery_token_not_portable(self):
        c = dict(PROFILE)
        c.pop("transport_recovery")
        self.m.c = validate_config(c)
        health = TransportHealth(self.m, lambda: self.now)
        health.probe = mock.Mock()
        self.assertIsNone(health.tick(self.result))
        health.probe.assert_not_called()
        for key, value in (("product", "ffb1"), ("serial", "REPLACE_WITH_VERIFIED_USB_SERIAL"), ("drivers", ["rndis_host"])):
            config = dict(PROFILE)
            config[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(Conflict, "Questone2"):
                validate_config(config)

    def test_public_example_is_not_a_runnable_machine_profile(self):
        example = json.loads(Path(__file__).with_name("questone2-usb-profile.example.json").read_text())
        self.assertNotIn("transport_recovery", example)
        with self.assertRaisesRegex(Conflict, "USB path"):
            validate_config(example)

    def test_real_sysfs_verification_checks_config_and_device_number(self):
        self.m.sys = Path(self.tmp.name) / "sys"
        usb = self.m.sys / "bus/usb/devices/1-9.9"
        usb.mkdir(parents=True)
        fields = {"idVendor": "046b", "idProduct": "ffb0", "serial": "TEST_USB_SERIAL", "bConfigurationValue": "2",
                  "busnum": "1", "devnum": "4", "dev": "189:3"}
        for key, value in fields.items():
            (usb / key).write_text(value)
        health = TransportHealth(self.m, lambda: self.now)
        self.assertEqual(str(health.verified_node(self.m.current)[0]), "/dev/bus/usb/001/004")
        (usb / "bConfigurationValue").write_text("1")
        with self.assertRaisesRegex(Conflict, "configuration"):
            health.verified_node(self.m.current)
        (usb / "bConfigurationValue").write_text("2")
        (usb / "devnum").write_text("5")
        with self.assertRaisesRegex(Conflict, "number changed"):
            health.verified_node(self.m.current)

    def test_probe_classifies_without_sending_application_data(self):
        health = TransportHealth(self.m, lambda: self.now)
        for failure, banner, expected in ((TimeoutError(), b"", "connect_timeout"),
                (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), b"", "port_refused_network_alive"),
                (None, b"SSH-2.0-OpenSSH_7.4p1 Debian-8\r\n", "ssh_banner_verified"),
                (None, b"HTTP/1.0 200 OK\r\n", "unexpected_banner")):
            stream = mock.MagicMock()
            stream.__enter__.return_value = stream
            stream.connect.side_effect = failure
            stream.recv.return_value = banner
            with self.subTest(expected=expected), mock.patch("host_usb_link.socket.socket", return_value=stream):
                self.assertEqual(health.probe(), expected)
                stream.send.assert_not_called()
                stream.sendall.assert_not_called()
        stream.connect.side_effect = None
        stream.recv.side_effect = TimeoutError()
        with mock.patch("host_usb_link.socket.socket", return_value=stream):
            self.assertEqual(health.probe(), "banner_timeout")

    def test_reset_helper_only_gets_twice_verified_character_fd(self):
        health = TransportHealth(self.m, lambda: self.now)
        number = os.makedev(189, 3)
        health.verified_node = mock.Mock(return_value=(Path("/dev/bus/usb/001/004"), number))
        child = mock.Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        with mock.patch("host_usb_link.os.open", return_value=55) as opened, \
             mock.patch("host_usb_link.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_rdev=number)), \
             mock.patch("host_usb_link.os.close") as closed, \
             mock.patch("host_usb_link.subprocess.Popen", return_value=child) as spawned:
            self.assertEqual(health.perform_reset(self.m.current), "reset_requested")
            self.assertEqual(health.verified_node.call_count, 2)
            self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
            self.assertEqual(spawned.call_args.kwargs["pass_fds"], (55,))
            self.assertNotIn("shell", spawned.call_args.kwargs)
            self.assertIn("0x5514", spawned.call_args.args[0][3])
            child.wait.assert_called_once_with(timeout=15)
            closed.assert_called_once_with(55)

    def test_wrong_character_node_or_reenumeration_prevents_helper(self):
        health = TransportHealth(self.m, lambda: self.now)
        number = os.makedev(189, 3)
        health.verified_node = mock.Mock(return_value=(Path("/dev/bus/usb/001/004"), number))
        for mode, dev in ((stat.S_IFREG | 0o600, number), (stat.S_IFCHR | 0o600, os.makedev(189, 4))):
            with mock.patch("host_usb_link.os.open", return_value=55), \
                 mock.patch("host_usb_link.os.fstat", return_value=SimpleNamespace(st_mode=mode, st_rdev=dev)), \
                 mock.patch("host_usb_link.os.close") as closed, mock.patch("host_usb_link.subprocess.Popen") as spawned:
                with self.assertRaisesRegex(Conflict, "Opened USB node"):
                    health.perform_reset(self.m.current)
                spawned.assert_not_called()
                closed.assert_called_once_with(55)
        health.verified_node.side_effect = [(Path("/dev/bus/usb/001/004"), number), Conflict("identity changed")]
        with mock.patch("host_usb_link.os.open", return_value=55), \
             mock.patch("host_usb_link.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_rdev=number)), \
             mock.patch("host_usb_link.os.close"), mock.patch("host_usb_link.subprocess.Popen") as spawned:
            with self.assertRaisesRegex(Conflict, "identity changed"):
                health.perform_reset(self.m.current)
            spawned.assert_not_called()

    def test_stuck_helper_is_killed_and_bounded(self):
        health = TransportHealth(self.m, lambda: self.now)
        number = os.makedev(189, 3)
        health.verified_node = mock.Mock(return_value=(Path("/dev/bus/usb/001/004"), number))
        child = mock.Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired("helper", 15), subprocess.TimeoutExpired("helper", 2)]
        child.poll.return_value = None
        with mock.patch("host_usb_link.os.open", return_value=55), \
             mock.patch("host_usb_link.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_rdev=number)), \
             mock.patch("host_usb_link.os.close"), mock.patch("host_usb_link.subprocess.Popen", return_value=child):
            self.assertEqual(health.perform_reset(self.m.current), "reset_helper_stuck")
            child.kill.assert_called_once()
            self.assertEqual(child.wait.call_args_list, [mock.call(timeout=15), mock.call(timeout=2)])
            self.assertIs(health.pending, child)


if __name__ == "__main__":
    unittest.main()
