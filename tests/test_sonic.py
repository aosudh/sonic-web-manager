"""No switch access: all live operations use an in-memory database and fake CLI."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from server.sonic import AdapterError, SonicAdapter, _revision


class FakeLive(SonicAdapter):
    def __init__(self, path):
        super().__init__(path, demo=False)
        self.config = self._seed_demo()
        self.saved = copy.deepcopy(self.config)
        self.calls = []
        self.missing = set()
        self.failure_at = None
        self.config_reads = 0
        self.on_run = None

    def _load_config(self):
        self.config_reads += 1
        return copy.deepcopy(self.config)

    def _startup_config(self):
        return copy.deepcopy(self.saved)

    def _help(self, path):
        if tuple(path) in self.missing:
            raise AdapterError("No such command", 502)
        return "Usage: config " + " ".join(path) + " [OPTIONS]"

    def _db_table(self, database, table, separator="|"):
        if table == "PORT_TABLE":
            return {n: {"oper_status": "up" if n in ("Ethernet52", "Ethernet64") else "down"} for n in self.config["PORT"]}
        if table == "EEPROM_INFO":
            return {"Label Revision": {"value": "R1N26A"}}
        return {}

    def _port_counters(self):
        return {}

    def _run(self, argv, timeout=10):
        self.calls.append((argv, timeout))
        if self.failure_at == len(self.calls):
            raise AdapterError("simulated CLI failure", 502)
        if self.on_run:
            self.on_run(argv)
        if argv[:2] == ["config", "hostname"]:
            self.config["DEVICE_METADATA"]["localhost"]["hostname"] = argv[2]
        if argv[:3] == ["config", "interface", "mtu"]:
            self.config["PORT"][argv[3]]["mtu"] = argv[4]
        if argv[:2] == ["config", "save"]:
            self.saved = copy.deepcopy(self.config)
        return "mock accepted"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.live = FakeLive(self.temp.name)
        self.addCleanup(self.live.close)

    def plan(self, action, values):
        return self.live.plan(action, values, _revision(self.live.config))

    def assert_error(self, status, callback):
        with self.assertRaises(AdapterError) as caught:
            callback()
        self.assertEqual(status, caught.exception.status)

    def test_all_catalog_pages_have_honest_data_contract(self):
        demo = SonicAdapter(self.temp.name, demo=True)
        pages = [p for g in demo.catalog()["groups"] for p in g["pages"]]
        self.assertEqual(70, len(pages))
        for page in pages:
            result = demo.page(page["id"])
            for key in ("rows", "columns", "actions", "capability", "revision", "sampled_at", "pending_save"):
                self.assertIn(key, result, page["id"])
            self.assertIn("DEMO", result["description"])
        self.assertEqual("DEMO simulated health", demo.overview()["health"]["message"])

    def test_hpe_mapping_is_cage_association_not_geometry(self):
        result = self.live.ports()
        rows = {r["name"]: r for r in result["items"]}
        self.assertEqual(("QSFP2", 50, [69, 70, 71, 72]), (rows["Ethernet52"]["cage_id"], rows["Ethernet52"]["index"], rows["Ethernet52"]["lanes"]))
        self.assertEqual("QSFP5", rows["Ethernet64"]["cage_id"])
        self.assertFalse(result["layout"]["verified"])
        self.assertEqual("logical", result["layout"]["kind"])
        self.assertNotIn("row", rows["Ethernet52"])
        self.assertIsNone(rows["Ethernet52"]["rx_bps"])

    def test_console_page_does_not_require_configuration_or_bmc(self):
        with patch.object(self.live, "_config", side_effect=AssertionError("CONFIG_DB unavailable")), \
             patch.object(self.live._bmc, "snapshot", side_effect=AssertionError("BMC unavailable")):
            page = self.live.page("system.console")
        self.assertTrue(page["capability"]["read"])
        self.assertEqual(page["actions"], [])
        self.assertIsNone(page["pending_save"])

    def test_alias_change_does_not_change_cage(self):
        self.live.config["PORT"]["Ethernet52"]["alias"] = "my-port-999"
        row = next(p for p in self.live.ports()["items"] if p["name"] == "Ethernet52")
        self.assertEqual("QSFP2", row["cage_id"])

    def test_breakout_children_share_cage_and_unique_lanes(self):
        base = self.live.config["PORT"]["Ethernet52"]
        for i in range(4):
            self.live.config["PORT"][f"Ethernet{52+i}"] = {**base, "speed": "25000", "lanes": str(69+i)}
        rows = [p for p in self.live.ports()["items"] if p["cage_id"] == "QSFP2"]
        self.assertEqual(4, len(rows))
        self.assertEqual({"4x25G"}, {p["breakout_mode"] for p in rows})
        self.assertFalse(self.live.port_detail("Ethernet52")["capabilities"]["dynamic_breakout"])

    def test_unknown_platform_has_no_invented_panel(self):
        self.live.config["DEVICE_METADATA"]["localhost"]["platform"] = "other-platform"
        row = self.live.ports()["items"][0]
        self.assertEqual("module-index:1", row["cage_id"])
        self.assertEqual("unknown", row["connector"])

    def test_hpe_requires_exact_oem_and_consistent_lanes(self):
        self.live._db_table = lambda *args: {}
        self.assertFalse(self.live.ports()["layout"]["binding_verified"])
        self.live = FakeLive(self.temp.name)
        self.live.config["PORT"]["Ethernet52"]["lanes"] = "1,2,3,4"
        self.assertFalse(self.live.ports()["layout"]["binding_verified"])

    def test_multi_asic_write_disabled(self):
        self.live.config["DEVICE_METADATA"]["localhost"]["num_asic"] = "2"
        self.assert_error(501, lambda: self.plan("system.hostname", {"hostname": "updated"}))
        self.assertFalse(self.live.page("ports.ports")["capability"]["write"])
        self.assertTrue(self.live.ports()["multi_asic"])

    def test_pending_save_compares_running_and_startup_not_memory(self):
        self.assertFalse(self.live.overview()["pending_save"])
        self.live.config["DEVICE_METADATA"]["localhost"]["hostname"] = "external-cli-change"
        self.live._invalidate()
        self.assertTrue(self.live.overview()["pending_save"])
        self.live.saved = copy.deepcopy(self.live.config)
        self.live._invalidate()
        self.assertFalse(self.live.overview()["pending_save"])

    def test_unreadable_startup_means_unknown(self):
        self.live._startup_config = lambda: (_ for _ in ()).throw(AdapterError("unknown", 503))
        self.assertIsNone(self.live.overview()["pending_save"])
        self.assertIsNone(self.live.page("setup.configurations")["pending_save"])

    def test_partial_state_unknown_not_missing(self):
        self.live._db_table = lambda *args: {}
        self.assertEqual("unknown", self.live.ports()["items"][0]["oper_status"])
        result = self.live.page("status.fans")
        self.assertFalse(result["capability"]["read"])
        self.assertEqual([], result["rows"])

    def test_plan_is_read_only_no_backup_or_cli(self):
        original = copy.deepcopy(self.live.config)
        result = self.plan("port.configure", {"interfaces": ["Ethernet52"], "mtu": 9000})
        self.assertTrue(result["requires_confirmation"])
        self.assertNotIn("commands", result)
        self.assertEqual([], self.live.calls)
        self.assertEqual(original, self.live.config)
        self.assertFalse((Path(self.temp.name) / "backups").exists())

    def test_unsupported_leaf_not_exposed(self):
        self.live.missing.add(("interface", "description"))
        fields = self.live.page("ports.ports")["actions"][0]["fields"]
        self.assertNotIn("description", {f["name"] for f in fields})
        self.assert_error(400, lambda: self.plan("port.configure", {"interfaces": ["Ethernet52"], "description": "x"}))

    def test_unknown_actions_and_fields_rejected(self):
        self.assert_error(400, lambda: self.plan("shell.exec", {"command": "reboot"}))
        self.assert_error(400, lambda: self.plan("system.hostname", {"hostname": "good", "shell": "reboot"}))

    def test_injection_and_argument_boundaries(self):
        for hostname in ("--help", "a;reboot", "a\nreboot", "$(id)", "a b"):
            self.assert_error(400, lambda h=hostname: self.plan("system.hostname", {"hostname": h}))
        for interface in ("Ethernet52;reboot", "--help", "eth0", "Ethernet9999"):
            with self.assertRaises(AdapterError):
                self.plan("port.configure", {"interfaces": [interface], "admin_status": "down"})
        self.assertEqual([], self.live.calls)

    def test_mtu_speed_fec_validation(self):
        for field, value in (("mtu", 999999), ("mtu", True), ("fec", "unknown"), ("speed", 400000)):
            self.assert_error(400, lambda f=field, v=value: self.plan("port.configure", {"interfaces": ["Ethernet52"], f: v}))

    def test_stale_plan_rejected_before_backup(self):
        result = self.plan("system.hostname", {"hostname": "updated"})
        self.live.config["NTP_SERVER"]["new.example.com"] = {}
        self.assert_error(409, lambda: self.live.apply(result["id"]))
        self.assertEqual([], self.live.calls)
        self.assertFalse((Path(self.temp.name) / "backups").exists())

    def test_stale_page_revision_rejected(self):
        self.assert_error(409, lambda: self.live.plan("system.hostname", {"hostname": "updated"}, "stale"))

    def test_expiry_and_one_shot(self):
        plan = self.plan("system.hostname", {"hostname": "updated"})
        self.live._plans[plan["id"]]["expires"] = time.time() - 1
        self.assert_error(409, lambda: self.live.apply(plan["id"]))
        self.assert_error(404, lambda: self.live.apply(plan["id"]))

    def test_backup_precedes_write_and_is_not_public_export(self):
        self.live.config["TACPLUS_SERVER"] = {"1.2.3.4": {"passkey": "test-secret"}}
        plan = self.plan("system.hostname", {"hostname": "updated"})
        def observe(argv):
            files = list((Path(self.temp.name) / "backups").glob("*.json"))
            self.assertEqual(1, len(files))
            self.assertEqual("test-secret", json.loads(files[0].read_text(encoding="utf-8"))["TACPLUS_SERVER"]["1.2.3.4"]["passkey"])
        self.live.on_run = observe
        result = self.live.apply(plan["id"])
        self.assertTrue(result["success"])
        self.assertFalse(result["details"]["hardware_verified"])
        self.assertTrue(result["pending_save"])

    def test_backup_failure_prevents_mutation(self):
        plan = self.plan("system.hostname", {"hostname": "updated"})
        with patch("server.sonic.os.open", side_effect=OSError("disk full")):
            self.assert_error(500, lambda: self.live.apply(plan["id"]))
        self.assertEqual([], self.live.calls)

    def test_partial_failure_not_fake_success(self):
        plan = self.plan("port.configure", {"interfaces": ["Ethernet0", "Ethernet1"], "mtu": 9000})
        self.live.failure_at = 2
        result = self.live.apply(plan["id"])
        self.assertFalse(result["success"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["pending_save"])
        self.assertEqual(1, len(result["details"]["completed"]))
        self.assertFalse(result["details"]["auto_rollback"])

    def test_zero_exit_without_config_change_is_not_success(self):
        plan = self.plan("system.hostname", {"hostname": "updated"})
        self.live._run = lambda *args, **kwargs: "CLI silently declined"
        result = self.live.apply(plan["id"])
        self.assertFalse(result["success"])
        self.assertIn("预期配置未读回", result["details"]["failure"])

    def test_dns_bgp_pfc_mirror_plan_exact_argv(self):
        cases = [("dns.add", {"server": "1.1.1.1"}, ["config", "dns", "nameserver", "add", "1.1.1.1"])]
        self.live.config["BGP_NEIGHBOR"] = {"10.0.0.2": {"asn": "65101", "admin_status": "up"}}
        self.live.config["PORT_QOS_MAP"] = {"Ethernet0": {"pfc_enable": "3", "tc_to_queue_map": "AZURE"}}
        cases += [("bgp.neighbor.shutdown", {"neighbor": "10.0.0.2"}, ["config", "bgp", "shutdown", "neighbor", "10.0.0.2"]),
                  ("pfc.priority", {"interface": "Ethernet0", "priority": "4", "status": "on"}, ["config", "interface", "pfc", "priority", "Ethernet0", "4", "on"]),
                  ("mirror.span.add", {"name": "Monitor1", "source": "Ethernet0", "destination": "Ethernet1", "direction": "rx"}, ["config", "mirror_session", "span", "add", "Monitor1", "Ethernet1", "Ethernet0", "rx"])]
        for action, values, expected in cases:
            result = self.plan(action, values)
            internal = self.live._plans[result["id"]]
            self.assertEqual([expected], internal["commands"])
            if action == "mirror.span.add":
                self.assertEqual("RX", internal["candidate"]["MIRROR_SESSION"]["Monitor1"]["direction"])
        self.assertEqual([], self.live.calls)

    def test_new_actions_guard_existing_dependencies(self):
        self.assert_error(404, lambda: self.plan("bgp.neighbor.shutdown", {"neighbor": "10.0.0.2"}))
        self.assert_error(409, lambda: self.plan("pfc.priority", {"interface": "Ethernet0", "priority": 3, "status": "on"}))
        self.assert_error(400, lambda: self.plan("dns.add", {"server": "example.com"}))
        self.live.config["DNS_NAMESERVER"] = {f"1.1.1.{i}": {} for i in (1, 2, 3)}
        self.assert_error(409, lambda: self.plan("dns.add", {"server": "8.8.8.8"}))
        self.live.config["INTERFACE"]["Ethernet1|10.0.0.1/31"] = {}
        self.assert_error(409, lambda: self.plan("mirror.span.add", {"name": "Monitor1", "source": "Ethernet0", "destination": "Ethernet1", "direction": "rx"}))

    def test_serialization(self):
        plan = self.plan("system.hostname", {"hostname": "updated"})
        self.live._write_lock.acquire()
        try:
            self.assert_error(409, lambda: self.live.apply(plan["id"]))
        finally:
            self.live._write_lock.release()
        self.assertIn(plan["id"], self.live._plans)

    def test_export_masks_community_key_and_nested_secrets(self):
        self.live.config["SNMP_COMMUNITY"] = {"do-not-export": {"TYPE": "RO"}}
        self.live.config["TACPLUS_SERVER"] = {"1.2.3.4": {"passkey": "secret-value", "priority": "1"}}
        text = json.dumps(self.live.export_config())
        self.assertNotIn("do-not-export", text)
        self.assertNotIn("secret-value", text)
        self.assertIn("priority", text)

    def test_export_masks_macsec_ntp_and_aaa_secrets_by_context(self):
        self.live.config.update({
            "MACSEC_PROFILE": {"secure": {"primary_cak": "primary-cak-value", "fallback_cak": "fallback-cak-value", "priority": "1"}},
            "NTP_KEY": {"42": {"value": "ntp-auth-secret", "type": "SHA256"}},
            "SNMP_USER": {"admin": {"SNMP_USER_AUTH_PASSWORD": "snmp-auth-secret", "SNMP_USER_ENCRYPTION_PASSWORD": "snmp-encryption-secret"}},
            "RADIUS_SERVER": {"1.2.3.4": {"passkey": "radius-shared-secret"}},
            "TACPLUS_SERVER": {"1.2.3.5": {"passkey": "tacacs-shared-secret"}},
        })
        result = self.live.export_config()
        encoded = json.dumps(result)
        for secret in ("primary-cak-value", "fallback-cak-value", "ntp-auth-secret", "snmp-auth-secret", "snmp-encryption-secret", "radius-shared-secret", "tacacs-shared-secret"):
            self.assertNotIn(secret, encoded)
        self.assertEqual("SHA256", result["NTP_KEY"]["42"]["type"])
        self.assertEqual("1", result["MACSEC_PROFILE"]["secure"]["priority"])

    def test_save_zero_exit_but_no_startup_update_is_failure(self):
        self.live.config["DEVICE_METADATA"]["localhost"]["hostname"] = "unsaved"
        plan = self.plan("config.save", {})
        self.live._run = lambda *args, **kwargs: "exit zero but did not save"
        result = self.live.apply(plan["id"])
        self.assertFalse(result["success"])
        self.assertTrue(result["pending_save"])

    def test_save_unknown_startup_is_not_success(self):
        plan = self.plan("config.save", {})
        self.live._startup_config = lambda: (_ for _ in ()).throw(AdapterError("not readable", 503))
        result = self.live.apply(plan["id"])
        self.assertFalse(result["success"])
        self.assertIsNone(result["pending_save"])

    def test_command_oserror_preserves_partial_result(self):
        plan = self.plan("port.configure", {"interfaces": ["Ethernet0", "Ethernet1"], "mtu": 9000})
        original_run = self.live._run
        def run(argv, timeout=10):
            if self.live.calls:
                raise OSError("cannot create second process")
            return original_run(argv, timeout)
        self.live._run = run
        result = self.live.apply(plan["id"])
        self.assertFalse(result["success"])
        self.assertTrue(result["changed"])
        self.assertEqual(1, len(result["details"]["completed"]))
        self.assertNotIn("备份失败", result["message"])

    def test_fec_unknown_support_cannot_change_current_mode(self):
        self.assert_error(400, lambda: self.plan("port.configure", {"interfaces": ["Ethernet52"], "fec": "none"}))
        original = self.live._db_table
        self.live._db_table = lambda db, table, separator="|": {"Ethernet52": {"supported_fecs": "rs,none"}} if table == "PORT_TABLE" else original(db, table, separator)
        plan = self.plan("port.configure", {"interfaces": ["Ethernet52"], "fec": "none"})
        self.assertEqual("none", self.live._plans[plan["id"]]["candidate"]["PORT"]["Ethernet52"]["fec"])

    def test_forms_use_actual_named_interface_options(self):
        for page, field_name in (("ethernet.vlan", "interface"), ("ethernet.lag", "interface"), ("routing.interfaces", "interface"), ("ethernet.pfc", "interface"), ("ports.mirror", "source")):
            forms = self.live.page(page)["actions"]
            fields = [f for form in forms for f in form["fields"] if f["name"] == field_name]
            self.assertTrue(fields)
            option = next(o for o in fields[0]["options"] if o["value"] == "Ethernet52")
            self.assertEqual("select", fields[0]["type"])
            self.assertIn("QSFP2", option["label"])

    def test_health_single_hash_shape(self):
        self.live._db_read = lambda database, key: {"summary": "OK"} if key == "SYSTEM_HEALTH_INFO" else {}
        self.assertEqual("OK", self.live.overview()["health"]["status"])
        self.assertEqual("系统健康正常", self.live.overview()["health"]["message"])
        self.assertEqual("OK", self.live.page("status.alerts")["rows"][0]["summary"])

    def test_recursive_acl_references_block_lag_and_mirror(self):
        self.live.config["ACL_TABLE"] = {"acl": {"ports": ["Ethernet0"]}}
        self.live.config["PORTCHANNEL"]["PortChannel1"] = {}
        self.assert_error(409, lambda: self.plan("lag.member.add", {"name": "PortChannel1", "interface": "Ethernet0"}))
        self.assert_error(409, lambda: self.plan("mirror.span.add", {"name": "Probe", "source": "Ethernet1", "destination": "Ethernet0", "direction": "rx"}))
        self.live.config["ACL_TABLE"]["acl"]["ports"] = {"nested": ("PortChannel1",)}
        self.assert_error(409, lambda: self.plan("lag.delete", {"name": "PortChannel1"}))

    def test_ip_inputs_reject_scope_and_whitespace_everywhere(self):
        cases = [("ntp.add", {"server": "fe80::1%eth0 --bad"}),
                 ("dns.add", {"server": "fe80::1%eth0"}),
                 ("route.add", {"prefix": "2001:db8::/64", "nexthop": "fe80::1%eth0"}),
                 ("interface.address.add", {"interface": "Ethernet0", "prefix": "fe80::1%eth0/64"}),
                 ("bgp.neighbor.shutdown", {"neighbor": "fe80::1%eth0"}),
                 ("ntp.add", {"server": "1.1.1.1 "})]
        for action, values in cases:
            self.assert_error(400, lambda a=action, v=values: self.plan(a, v))
        self.assertEqual([], self.live.calls)

    def test_profile_runtime_does_not_use_demo_seed(self):
        with patch.object(self.live, "_seed_demo", side_effect=AssertionError("demo seed used by live mapping")):
            self.assertEqual("hpe-r1n26a-d3030", self.live.ports()["layout"]["profile_id"])

    def test_vlan_membership_dependencies(self):
        self.live.config["INTERFACE"]["Ethernet0|10.0.0.1/31"] = {}
        self.assert_error(409, lambda: self.plan("vlan.member.add", {"vlan_id": 100, "interface": "Ethernet0", "tagging_mode": "untagged"}))
        self.live.config["VLAN_MEMBER"]["Vlan100|Ethernet1"] = {"tagging_mode": "tagged"}
        self.assert_error(409, lambda: self.plan("vlan.delete", {"vlan_id": 100}))

    def test_lag_dependency_and_member_guard(self):
        self.live.config["PORTCHANNEL"]["PortChannel1"] = {"admin_status": "up"}
        self.live.config["PORTCHANNEL_MEMBER"]["PortChannel1|Ethernet0"] = {}
        self.assert_error(409, lambda: self.plan("lag.delete", {"name": "PortChannel1"}))
        self.assert_error(409, lambda: self.plan("lag.member.add", {"name": "PortChannel1", "interface": "Ethernet0"}))

    def test_route_validation_and_argument_shape(self):
        self.assert_error(400, lambda: self.plan("route.add", {"prefix": "10.1.1.5/24", "nexthop": "10.0.0.2"}))
        self.assert_error(400, lambda: self.plan("route.add", {"prefix": "10.1.1.0/24", "nexthop": "::1"}))
        plan = self.plan("route.add", {"prefix": "10.1.1.0/24", "nexthop": "10.0.0.2"})
        argv = self.live._plans[plan["id"]]["commands"][0]
        self.assertEqual(["config", "route", "add", "prefix", "10.1.1.0/24", "nexthop", "10.0.0.2"], argv)

    def test_management_ip_not_modifiable(self):
        self.assert_error(400, lambda: self.plan("interface.address.add", {"interface": "eth0", "prefix": "10.0.0.1/24"}))

    def test_ntp_rejects_options_and_shell_input(self):
        for server in ("--help", "a;reboot", "example.com\n", "-pool.example"):
            self.assert_error(400, lambda s=server: self.plan("ntp.add", {"server": s}))

    def test_demo_apply_never_calls_host_and_save_is_simulated(self):
        demo = SonicAdapter(self.temp.name, demo=True)
        with patch.object(demo, "_run", side_effect=AssertionError("host command called")):
            plan = demo.plan("system.hostname", {"hostname": "new-demo"}, demo.overview()["revision"])
            result = demo.apply(plan["id"])
            self.assertTrue(result["details"]["simulated"])
            self.assertEqual("new-demo", demo.overview()["device"]["hostname"])
            save = demo.plan("config.save", {})
            self.assertFalse(demo.apply(save["id"])["pending_save"])
        self.assertFalse((Path(self.temp.name) / "backups").exists())

    def test_live_failure_never_becomes_demo(self):
        actual = SonicAdapter(self.temp.name, demo=False)
        with patch.dict("sys.modules", {"swsscommon": None}), patch.object(actual, "_run", side_effect=AdapterError("missing", 503)):
            self.assert_error(503, actual.overview)
        self.assertIsNone(actual._demo_config)

    def test_shared_cache_does_not_reload_each_browser(self):
        for _ in range(10):
            self.live.ports()
        self.assertEqual(1, self.live.config_reads)

    def test_unknown_routes_raise_not_fake_data(self):
        self.assert_error(404, lambda: self.live.page("not-a-page"))
        self.assert_error(404, lambda: self.live.port_detail("Ethernet9999"))

    def test_mac_table_calls_fdbshow_directly(self):
        with patch.object(self.live, "_command_read", return_value="Total number of entries 0") as read:
            result = self.live.page("ethernet.mac")
        read.assert_called_once_with(["fdbshow"])
        self.assertTrue(result["capability"]["read"])

    def test_ip_delete_hidden_and_rejected_when_sudo_helper_unavailable(self):
        self.live.config["INTERFACE"]["Ethernet0|10.0.0.1/31"] = {}
        probes = []
        def unavailable(argv, timeout=10):
            probes.append(argv)
            raise AdapterError("sudo: unable to change uid", 502)
        self.live._run = unavailable
        result = self.live.page("routing.interfaces")
        self.assertNotIn("interface.address.delete", [a["id"] for a in result["actions"]])
        self.assertIn("interface.address.add", [a["id"] for a in result["actions"]])
        self.assertIn("路由保护前置检查", result["capability"]["reason"])
        self.assert_error(501, lambda: self.plan("interface.address.delete", {"interface": "Ethernet0", "prefix": "10.0.0.1/31"}))
        self.assertEqual([["sudo", "-n", "/usr/bin/true"]], probes)

    def test_ip_delete_supported_when_fixed_sudo_probe_passes(self):
        self.live.config["INTERFACE"]["Ethernet0|10.0.0.1/31"] = {}
        result = self.plan("interface.address.delete", {"interface": "Ethernet0", "prefix": "10.0.0.1/31"})
        self.assertEqual([["config", "interface", "ip", "remove", "Ethernet0", "10.0.0.1/31"]], self.live._plans[result["id"]]["commands"])
        self.assertEqual([["sudo", "-n", "/usr/bin/true"]], [call[0] for call in self.live.calls])

    def test_ip_delete_demo_never_executes_sudo(self):
        demo = SonicAdapter(self.temp.name, demo=True)
        demo._demo_config["INTERFACE"]["Ethernet0|10.0.0.1/31"] = {}
        with patch.object(demo, "_run", side_effect=AssertionError("host command called")):
            plan = demo.plan("interface.address.delete", {"interface": "Ethernet0", "prefix": "10.0.0.1/31"})
            self.assertTrue(demo.apply(plan["id"])["details"]["simulated"])

    def test_speed_modes_use_real_capabilities_and_unknown_keeps_current_only(self):
        row = next(p for p in self.live.ports()["items"] if p["name"] == "Ethernet52")
        self.assertEqual([100000], [m["speed"] for m in row["port_modes"] if m["kind"] == "speed"])
        self.live._db_table = lambda database, table, separator="|": {"Ethernet52": {"supported_speeds": "40000,100000"}} if table == "PORT_TABLE" else {}
        self.live._invalidate()
        row = next(p for p in self.live.ports()["items"] if p["name"] == "Ethernet52")
        self.assertEqual([40000, 100000], [m["speed"] for m in row["port_modes"] if m["kind"] == "speed"])

    def test_live_unverified_breakout_is_visible_but_not_executable(self):
        row = next(p for p in self.live.ports()["items"] if p["name"] == "Ethernet52")
        modes = [m for m in row["port_modes"] if m["kind"] == "breakout"]
        self.assertEqual(2, len(modes))
        self.assertFalse(any(m["available"] for m in modes))
        self.assertTrue(all(m["requires_reboot"] and m["method"] == "static-reboot" for m in modes))
        self.assertNotIn("port.breakout", [a["id"] for a in self.live.page("ports.ports")["actions"]])
        self.assert_error(501, lambda: self.plan("port.breakout", {"interface": "Ethernet52", "mode": "4x25G"}))

    def test_demo_breakout_split_merge_updates_same_cage_without_host_execution(self):
        demo = SonicAdapter(self.temp.name, demo=True)
        demo._demo_config["INTERFACE"] = {"Ethernet52|10.0.0.98/31": {}}
        with patch.object(demo, "_run", side_effect=AssertionError("host command called")):
            plan = demo.plan("port.breakout", {"interface": "Ethernet52", "mode": "4x25G"})
            self.assertTrue(plan["reboot_required"])
            self.assertTrue(demo.apply(plan["id"])["success"])
            rows = [p for p in demo.ports()["items"] if p["cage_id"] == "QSFP2"]
            self.assertEqual(4, len(rows))
            self.assertTrue(all(p["speed"] == 25000 and p["breakout_mode"] == "4x25G" for p in rows))
            self.assertEqual({"Ethernet52"}, {p["breakout"]["parent_interface"] for p in rows})
            self.assertEqual([10000, 25000], [m["speed"] for m in rows[0]["port_modes"] if m["kind"] == "speed"])
            plan = demo.plan("port.breakout", {"interface": "Ethernet52", "mode": "1x100G"})
            self.assertTrue(demo.apply(plan["id"])["success"])
            self.assertEqual(56, len(demo.ports()["items"]))
            self.assertIn("Ethernet52|10.0.0.98/31", demo._demo_config["INTERFACE"])

    def test_demo_breakout_rejects_dependencies_and_extra_values(self):
        demo = SonicAdapter(self.temp.name, demo=True)
        demo._demo_config["ACL_TABLE"] = {"test": {"ports": ["Ethernet52"]}}
        self.assert_error(409, lambda: demo.plan("port.breakout", {"interface": "Ethernet52", "mode": "4x25G"}))
        self.assert_error(400, lambda: demo.plan("port.breakout", {"interface": "Ethernet52", "mode": "4x25G", "force": True}))

    def test_persistent_breakout_blocks_all_writes_and_save_survives_adapter_restart(self):
        directory = Path(self.temp.name) / "breakout" / ("a" * 32)
        directory.mkdir(parents=True)
        (directory.parent / "current.json").write_text(json.dumps({"id": directory.name}))
        (directory / "status.json").write_text(json.dumps({"status": "pending_reboot", "verified": False}))
        restarted = FakeLive(self.temp.name)
        self.assertIsNone(restarted.overview()["pending_save"])
        self.assertTrue(restarted.page("ports.ports")["operation"]["blocking"])
        for action, values in (("config.save", {}), ("system.hostname", {"hostname": "updated"}), ("port.configure", {"interfaces": ["Ethernet0"], "mtu": 9000})):
            self.assert_error(501, lambda: restarted.plan(action, values))
        (directory / "status.json").write_text(json.dumps({"status": "failed"}))
        (directory / "manifest.json").write_text(json.dumps({"status": "staged_pending_reboot"}))
        self.assertTrue(restarted._breakout_status()["blocking"])
        (directory / "manifest.json").write_text(json.dumps({"status": "stage_failed_original_files_restored"}))
        self.assertFalse(restarted._breakout_status()["blocking"])

    def test_breakout_launch_persists_private_plan_and_uses_fixed_worker_argv(self):
        result = self.live._launch_breakout({"private": "configuration"})
        self.assertTrue(result["success"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["details"]["hardware_verified"])
        identifier = result["details"]["operation_id"]
        self.assertRegex(identifier, r"^[a-f0-9]{32}$")
        argv = self.live.calls[-1][0]
        self.assertEqual(["/usr/bin/systemd-run", "--unit", "sonic-web-breakout", "--collect", "/usr/bin/python3", "/host/sonic-web/current/deploy/breakout_worker.py", "--operation", identifier], argv)
        self.assertTrue((Path(self.temp.name) / "breakout" / identifier / "plan.json").is_file())
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
