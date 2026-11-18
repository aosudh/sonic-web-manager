"""Real static platform fixtures; no switch writes or reboot calls."""
import copy
import base64
import json
from pathlib import Path
import shutil
import re
import tempfile
import unittest
from unittest.mock import patch

from server import port_breakout as pb
from server.sonic import SonicAdapter


class StaticBreakoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = SonicAdapter._seed_demo()
        self.config["INTERFACE"] = {"Ethernet52": {}, "Ethernet52|10.0.0.98/31": {}}
        self.config["NTP_SERVER"]["time.example.org"] = {}
        self.sku = self.root / pb.SKU
        self.sku.mkdir(parents=True)
        for name in pb.STATIC:
            shutil.copyfile(Path(__file__).parent / "fixtures" / "questone2" / name, self.sku / name)
        (self.root / "etc/sonic").mkdir(parents=True)
        (self.root / "etc/sonic/sonic_version.yml").write_text("build_version: '" + pb.BUILD + "'\n")
        (self.root / "etc/sonic/config_db.json").write_bytes(pb.encoded(self.config))
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/random/boot_id").write_text("boot-before")
        self.operation = self.root / "operation"
        self.operation.mkdir()

    def prepare(self, parent="Ethernet52", mode="4x25G", config=None):
        return pb.prepare({"interface": parent, "mode": mode}, self.config if config is None else config, root=self.root)

    def stage(self, plan):
        (self.operation / "plan.json").write_bytes(pb.encoded(plan))
        return pb.stage(plan, self.operation, current_config=plan["config_before"], root=self.root)

    def hardware_snapshot(self, plan, reverting=False):
        config = plan["config_before" if reverting else "config_after"]
        bcm = base64.b64decode(plan["files"][pb.STATIC[0]]["before" if reverting else "after"]).decode()
        mapping = {int(physical): int(logical) for logical, physical in re.findall(r"^portmap_(\d+)\.0=(\d+):\d+\s*$", bcm, re.M)}
        attrs = {"oid:0x1000000000000": {"NULL": "NULL"}}  # CPU is not a data port.
        inventory = {"port_names": {}, "vid_to_rid": {}, "lanes": {}, "sdk_portmap": "cpu0 0 0 0 64 64\nlb0 0 65 130 65 65\n", "sdk_status": ""}
        for i, (name, row) in enumerate(config["PORT"].items(), 1):
            lanes = list(map(int, row["lanes"].split(",")))
            vid, rid = f"oid:0x10000000{i:05x}", f"oid:0x90000000{i:05x}"
            # Captured RC4 static ports have SPEED/FEC/ADMIN but no HW_LANE_LIST.
            attrs[vid] = {"SAI_PORT_ATTR_SPEED": row["speed"], "SAI_PORT_ATTR_FEC_MODE": "SAI_PORT_FEC_MODE_RS", "SAI_PORT_ATTR_ADMIN_STATE": "true", "NULL": "NULL"}
            inventory["port_names"][name] = vid
            inventory["vid_to_rid"][vid] = rid
            inventory["lanes"].update({str(lane): rid for lane in lanes})
            logical = mapping[lanes[0]]
            sdk_name = ("ce" if len(lanes) == 4 else "xe") + str(i)
            inventory["sdk_portmap"] += f"  {sdk_name}  {int(lanes[0] > 64)} {logical:3} {lanes[0]:3} 0 0 0/10 0/10\n"
            inventory["sdk_status"] += f" {sdk_name}({logical:3}) down {len(lanes)} {int(row['speed'])//1000}G FD SW No Forward None IEEE\n"
        return config, attrs, inventory, bcm

    def test_plan_is_readonly_preserves_parent_l3_and_ntp(self):
        before = {n: (self.root / p).read_bytes() for n, p in pb.TARGETS.items()}
        plan = self.prepare()
        self.assertEqual(before, {n: (self.root / p).read_bytes() for n, p in pb.TARGETS.items()})
        self.assertEqual(59, len(plan["config_after"]["PORT"]))
        self.assertEqual(self.config["INTERFACE"], plan["config_after"]["INTERFACE"])
        self.assertEqual(self.config["NTP_SERVER"], plan["config_after"]["NTP_SERVER"])
        self.assertEqual(80, plan["summary"]["unique_lanes"])
        self.assertEqual("50", plan["config_after"]["PORT"]["Ethernet55"]["index"])
        self.assertIn("dport_map_port_72=57", plan["static_diff"][pb.STATIC[0]])

    def test_stage_then_merge_restores_lane_inventory_and_preserves_other_config(self):
        plan = self.prepare()
        manifest = self.stage(plan)
        self.assertEqual("staged_pending_reboot", manifest["status"])
        merged = self.prepare(mode="1x100G", config=plan["config_after"])
        self.assertEqual(56, len(merged["config_after"]["PORT"]))
        self.assertEqual("69,70,71,72", merged["config_after"]["PORT"]["Ethernet52"]["lanes"])
        self.assertNotIn("Ethernet55", merged["config_after"]["PORT"])
        self.assertEqual(self.config["INTERFACE"], merged["config_after"]["INTERFACE"])
        self.assertEqual(self.config["NTP_SERVER"], merged["config_after"]["NTP_SERVER"])

    def test_split_both_cages_has_62_ports_and_unique_dport_slots(self):
        plan = self.prepare()
        self.stage(plan)
        second = self.prepare("Ethernet64", config=plan["config_after"])
        self.assertEqual(62, second["summary"]["ports_after"])
        self.assertIn("dport_map_port_2=60", second["static_diff"][pb.STATIC[0]])

    def test_rejects_parent_acl_and_child_ip_dependencies(self):
        self.config["ACL_TABLE"] = {"protected": {"ports": ["Ethernet52"]}}
        with self.assertRaisesRegex(pb.BreakoutError, "ACL_TABLE"):
            self.prepare()
        self.config.pop("ACL_TABLE")
        self.config["INTERFACE"]["Ethernet53|10.2.0.1/31"] = {}
        with self.assertRaisesRegex(pb.BreakoutError, "INTERFACE"):
            self.prepare()

    def test_stage_detects_config_file_and_boot_conflicts(self):
        plan = self.prepare()
        self.config["NTP_SERVER"]["new.example.org"] = {}
        with self.assertRaises(pb.BreakoutError):
            pb.stage(plan, self.operation, current_config=self.config, root=self.root)
        self.config = plan["config_before"]
        (self.sku / "hwsku.json").write_text((self.sku / "hwsku.json").read_text() + "\n")
        with self.assertRaises(pb.BreakoutError):
            self.stage(plan)
        self.assertFalse((self.operation / "manifest.json").exists())

    def test_partial_stage_write_failure_restores_originals(self):
        plan = self.prepare()
        original = pb.atomic_write
        writes = []
        def fail_one(path, *args, **kwargs):
            if Path(path) in {self.root / p for p in pb.TARGETS.values()}:
                writes.append(str(path))
                if len(writes) == 2:
                    raise OSError("disk full")
            return original(path, *args, **kwargs)
        with patch.object(pb, "atomic_write", fail_one):
            with self.assertRaises(OSError):
                self.stage(plan)
        for name, entry in plan["files"].items():
            self.assertEqual(entry["before_sha256"], pb.digest((self.root / entry["path"]).read_bytes()))

    def test_verify_requires_new_boot_and_asic_not_just_configdb(self):
        plan = self.prepare()
        self.stage(plan)
        config = plan["config_after"]
        state = {n: {"state": "ok", "speed": r["speed"]} for n, r in config["PORT"].items()}
        app = copy.deepcopy(config["PORT"])
        _, asic, inventory, _ = self.hardware_snapshot(plan)
        def verify():
            return pb.verify(self.operation, config=config, state_ports=state, appl_ports=app, asic_ports=asic, asic_inventory=inventory, root=self.root)
        self.assertFalse(verify()["verified"])
        (self.root / "proc/sys/kernel/random/boot_id").write_text("boot-after")
        self.assertTrue(verify()["verified"])
        state["Ethernet0"].pop("speed")  # Down ports omit operational speed on RC4.
        self.assertTrue(verify()["verified"])
        asic.pop(inventory["port_names"]["Ethernet52"])
        self.assertFalse(verify()["verified"])

    def test_rollback_restores_exact_files_and_rejects_external_edits(self):
        plan = self.prepare()
        self.stage(plan)
        result = pb.rollback(self.operation, current_config=self.config, root=self.root)
        self.assertFalse(result["requires_reboot"])
        self.assertEqual("rolled_back", result["status"])
        for name, entry in plan["files"].items():
            self.assertEqual(entry["before_sha256"], pb.digest((self.root / entry["path"]).read_bytes()))

    def test_only_exact_image_and_known_cages_supported(self):
        with self.assertRaises(pb.BreakoutError):
            self.prepare("Ethernet48")
        (self.root / "etc/sonic/sonic_version.yml").write_text("build_version: another\n")
        with self.assertRaises(pb.BreakoutError):
            self.prepare()

    def test_stage_replace_then_fsync_failure_is_registered_for_restore(self):
        plan = self.prepare()
        original = pb.atomic_write
        raised = []
        def fail_after_replace(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if Path(path) == self.sku / pb.STATIC[0] and not raised:
                raised.append(True)
                raise OSError("directory fsync after replace")
            return result
        with patch.object(pb, "atomic_write", fail_after_replace):
            with self.assertRaises(OSError):
                self.stage(plan)
        self.assertEqual(plan["files"][pb.STATIC[0]]["before_sha256"], pb.digest((self.sku / pb.STATIC[0]).read_bytes()))

    def test_rollback_failure_is_retryable_but_unknown_external_hash_is_not(self):
        plan = self.prepare()
        self.stage(plan)
        original = pb.atomic_write
        writes = []
        def failure(path, *args, **kwargs):
            if Path(path) in {self.root / p for p in pb.TARGETS.values()}:
                writes.append(str(path))
                if len(writes) in (2, 3):
                    raise OSError("rollback and restore failed")
            return original(path, *args, **kwargs)
        with patch.object(pb, "atomic_write", failure):
            with self.assertRaises(OSError):
                pb.rollback(self.operation, current_config=self.config, root=self.root)
        self.assertTrue(pb.rollback(self.operation, current_config=self.config, root=self.root)["success"])
        (self.sku / "hwsku.json").write_text("external modification")
        with self.assertRaisesRegex(pb.BreakoutError, "外部"):
            pb.rollback(self.operation, current_config=self.config, root=self.root)

    def test_unsaved_configuration_must_be_saved_before_plan(self):
        self.config["NTP_SERVER"]["unsaved.example.org"] = {}
        with self.assertRaisesRegex(pb.BreakoutError, "未保存"):
            self.prepare()

    def test_verify_detects_nonport_and_startup_changes(self):
        plan = self.prepare()
        self.stage(plan)
        after = copy.deepcopy(plan["config_after"])
        after["NTP_SERVER"]["external.example.org"] = {}
        result = pb.verify(self.operation, config=after, state_ports={}, appl_ports={}, asic_ports={}, asic_inventory={}, root=self.root)
        self.assertTrue(any("非端口" in e for e in result["errors"]))
        (self.root / "etc/sonic/config_db.json").write_bytes(pb.encoded(after))
        result = pb.verify(self.operation, config=plan["config_after"], state_ports={}, appl_ports={}, asic_ports={}, asic_inventory={}, root=self.root)
        self.assertTrue(any("启动配置" in e for e in result["errors"]))

    def test_real_static_sai_shape_uses_vid_rid_lanes_and_sdk(self):
        plan = self.prepare()
        config, attrs, inventory, bcm = self.hardware_snapshot(plan)
        self.assertTrue(all("SAI_PORT_ATTR_HW_LANE_LIST" not in a for a in attrs.values()))
        errors, evidence = pb._verify_asic(config["PORT"], attrs, inventory, bcm)
        self.assertEqual([], errors)
        self.assertEqual({"port_names": 59, "unique_vids": 59, "unique_rids": 59, "hardware_lanes": 80,
                          "sdk_portmap_ports": 59, "sdk_status_ports": 59}, {k: v for k, v in evidence.items() if k != "source"})

    def test_swapped_hardware_lane_ownership_fails_with_correct_counts(self):
        config, attrs, inventory, bcm = self.hardware_snapshot(self.prepare())
        inventory["lanes"]["69"], inventory["lanes"]["70"] = inventory["lanes"]["70"], inventory["lanes"]["69"]
        errors, evidence = pb._verify_asic(config["PORT"], attrs, inventory, bcm)
        self.assertEqual(80, evidence["hardware_lanes"])
        self.assertTrue(any("Ethernet52 SAI" in e for e in errors))
        self.assertTrue(any("Ethernet53 SAI" in e for e in errors))

    def test_missing_translation_or_duplicate_vid_never_passes(self):
        config, attrs, inventory, bcm = self.hardware_snapshot(self.prepare())
        inventory["vid_to_rid"].pop(inventory["port_names"]["Ethernet52"])
        self.assertTrue(pb._verify_asic(config["PORT"], attrs, inventory, bcm)[0])
        config, attrs, inventory, bcm = self.hardware_snapshot(self.prepare())
        inventory["port_names"]["Ethernet53"] = inventory["port_names"]["Ethernet52"]
        self.assertTrue(any("VID" in e for e in pb._verify_asic(config["PORT"], attrs, inventory, bcm)[0]))

    def test_sdk_lane_count_and_speed_must_match_independently_of_sai(self):
        config, attrs, inventory, bcm = self.hardware_snapshot(self.prepare())
        inventory["sdk_status"] = re.sub(r"\(\s*71\) down 1 25G", "( 71) down 4 100G", inventory["sdk_status"])
        self.assertTrue(any("Ethernet52 SDK" in e for e in pb._verify_asic(config["PORT"], attrs, inventory, bcm)[0]))
        inventory["sdk_portmap"] = "show portmap\nnot initialized"
        self.assertTrue(any("show portmap" in e for e in pb._verify_asic(config["PORT"], attrs, inventory, bcm)[0]))

    def test_present_but_conflicting_hw_lane_list_and_sai_speed_fail(self):
        config, attrs, inventory, bcm = self.hardware_snapshot(self.prepare())
        target = attrs[inventory["port_names"]["Ethernet52"]]
        target["SAI_PORT_ATTR_HW_LANE_LIST"] = "1:70"
        target["SAI_PORT_ATTR_SPEED"] = "100000"
        errors, _ = pb._verify_asic(config["PORT"], attrs, inventory, bcm)
        self.assertTrue(any("HW_LANE_LIST" in e for e in errors))
        self.assertTrue(any("Ethernet52 SAI" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
