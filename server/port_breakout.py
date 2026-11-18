"""HPE Questone_2 static breakout: pure planning, guarded files, boot readback.

No reboot, service restart, running Redis mutation, or implicit rollback occurs
here. The separately installed worker owns reboot and durable job status.
"""
from __future__ import annotations

import base64
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

PLATFORM = "x86_64-cel_questone_2-r0"
HWSKU = "Questone_2"
BUILD = "test_rc4-candidate-kamel.77-837e6f735"
SKU = "usr/share/sonic/device/" + PLATFORM + "/" + HWSKU
STATIC = ("td3-as13-48f8h.config.bcm", "port_config.ini", "platform.json", "hwsku.json")
TARGETS = {name: SKU + "/" + name for name in STATIC}
TARGETS["config_db.json"] = "etc/sonic/config_db.json"
# Only these two cages have a measured SDK logical-port allocation and 690
# lane/LED validation. RC4 enablement requires a separate verified marker.
GROUPS = {
    "Ethernet52": {"number": 52, "lanes": [69, 70, 71, 72], "index": "50", "alias": "QSFP2", "logicals": [71, 72, 73, 74], "dports": [50, 57, 58, 59]},
    "Ethernet64": {"number": 64, "lanes": [1, 2, 3, 4], "index": "53", "alias": "QSFP5", "logicals": [1, 2, 3, 4], "dports": [53, 60, 61, 62]},
}
TERMINAL = {"succeeded", "rolled_back", "cancelled"}


class BreakoutError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BreakoutError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def revision(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _read_config():
    from swsscommon import swsscommon
    connector = swsscommon.ConfigDBConnector()
    connector.connect()
    def normal(v):
        if isinstance(v, dict):
            return {"|".join(k) if isinstance(k, tuple) else k: normal(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [normal(x) for x in v]
        return v
    return normal(connector.get_config())


def _path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    require(path.is_file() and not path.is_symlink() and path.resolve() == path,
            "静态文件必须是预期位置的普通文件：" + relative)
    return path


def _build(root):
    text = (Path(root) / "etc/sonic/sonic_version.yml").read_text()
    match = re.search(r"^build_version:\s*['\"]?([^'\"\s]+)", text, re.M)
    require(match and match.group(1) == BUILD, "静态拆分只适用于已核对的 RC4 .77 镜像。")
    return match.group(1)


def _boot_id(root):
    return (Path(root) / "proc/sys/kernel/random/boot_id").read_text().strip()


def _mentions(value, name):
    if isinstance(value, dict):
        return any(_mentions(k, name) or _mentions(v, name) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_mentions(v, name) for v in value)
    return name in re.split(r"[,|\s]+", str(value))


def dependencies(config, parent):
    group = GROUPS[parent]
    names = ["Ethernet" + str(group["number"] + i) for i in range(4)]
    result = []
    for table, entries in config.items():
        if table in ("PORT", "BREAKOUT_CFG") or not isinstance(entries, dict):
            continue
        for key, row in entries.items():
            for name in names:
                if not (_mentions(key, name) or _mentions(row, name)):
                    continue
                # The base interface keeps its name in both modes. Preserve its
                # existing L3 entries, never copy them to new children.
                if table == "INTERFACE" and str(key).split("|")[0] == parent:
                    continue
                result.append(table + ":" + str(key))
    return sorted(set(result))


def _lanes(config):
    return [int(lane) for row in config["PORT"].values() for lane in row["lanes"].split(",")]


def _mode(config, parent):
    group = GROUPS[parent]
    members = {name: row for name, row in config["PORT"].items() if str(row.get("index")) == group["index"]}
    count = len(members)
    names = ["Ethernet" + str(group["number"] + i) for i in range(4)]
    if count == 1 and set(members) == {parent} and members[parent]["lanes"] == ",".join(map(str, group["lanes"])) and members[parent]["speed"] == "100000":
        return "1x100G"
    if count == 4 and set(members) == set(names) and all(members[name]["lanes"] == str(lane) and members[name]["speed"] == "25000" for name, lane in zip(names, group["lanes"])):
        return "4x25G"
    raise BreakoutError(parent + " 当前 lane/速率组合不是已核对的 1x100G 或 4x25G。")


def prepare(values, config, *, root="/"):
    """Read-only. Return private JSON plan; caller must not send raw config to UI."""
    require(isinstance(values, dict) and set(values) == {"interface", "mode"}, "拆分参数只能包含 interface 和 mode。")
    parent, target = values["interface"], values["mode"]
    require(parent in GROUPS and target in ("1x100G", "4x25G"), "该机型/端口模式尚未适配。")
    meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
    require(meta.get("platform") == PLATFORM and meta.get("hwsku") == HWSKU, "平台/HwSKU 不匹配。")
    _build(root)
    group = GROUPS[parent]
    old_mode = _mode(config, parent)
    require(old_mode != target, "端口已处于所选模式。")
    refs = dependencies(config, parent)
    require(not refs, "端口仍被引用，先移除依赖：" + ", ".join(refs[:20]))
    before_lanes = _lanes(config)
    require(len(before_lanes) == len(set(before_lanes)) == 80, "全机必须保持 80 个唯一物理 lane。")
    source = {name: _path(root, rel).read_bytes() for name, rel in TARGETS.items()}
    require(revision(json.loads(source["config_db.json"])) == revision(config), "存在未保存配置；请先保存配置，再生成拆分计划。")
    platform, hwsku = [json.loads(source[name]) for name in ("platform.json", "hwsku.json")]
    p = platform["interfaces"][parent]
    require(p["lanes"] == ",".join(map(str, group["lanes"])) and p["index"] == ",".join([group["index"]] * 4), "平台 lane/index 与已核对的机型不符。")
    require(hwsku["interfaces"][parent]["default_brkout_mode"] == old_mode, "HwSKU 默认模式与当前端口不同。")
    require(config.get("BREAKOUT_CFG", {}).get(parent, {}).get("brkout_mode", old_mode) == old_mode, "BREAKOUT_CFG 与实际端口不一致。")
    names = ["Ethernet" + str(group["number"] + i) for i in range(4)]
    candidate = copy.deepcopy(config)
    base = copy.deepcopy(config["PORT"][parent])
    expected_names = set(config["PORT"])
    for name in names:
        candidate["PORT"].pop(name, None)
    count, speed = (4, "25000") if target == "4x25G" else (1, "100000")
    for i in range(count):
        child = copy.deepcopy(base)
        child.update(lanes=str(group["lanes"][i]) if count == 4 else ",".join(map(str, group["lanes"])),
                     speed=speed, index=group["index"], fec="rs", alias=group["alias"] + ("/" + str(i + 1) if count == 4 else ""))
        if count == 4:
            child["subport"] = str(i + 1)
        else:
            child.pop("subport", None)
        candidate["PORT"][names[i]] = child
    candidate.setdefault("BREAKOUT_CFG", {}).setdefault(parent, {})["brkout_mode"] = target
    p["breakout_modes"]["4x25G"] = [group["alias"] + "/" + str(i) for i in range(1, 5)]
    hwsku["interfaces"][parent]["default_brkout_mode"] = target
    bcm = source[STATIC[0]].decode()
    require("pbmp_xport_xe=0xffffffffffffffffffffffffffffffffe" in bcm, "缺少已验证的 IEEE 端口位图修复。")
    mapping = {int(n): (int(l), int(s)) for n, l, s in re.findall(r"^portmap_(\d+)\.0=(\d+):(\d+)\s*$", bcm, re.M)}
    expected_map = dict(zip(group["logicals"][:1 if old_mode == "1x100G" else 4], zip(group["lanes"], [100] if old_mode == "1x100G" else [25] * 4)))
    require(all(mapping.get(n) == expected_map.get(n) for n in group["logicals"]), "SDK 逻辑端口映射冲突或不是已适配的静态模式。")
    new_lines = ["portmap_{}.0={}:{}".format(group["logicals"][i], group["lanes"][i], 25 if count == 4 else 100) for i in range(count)]
    first = True
    lines = []
    for line in bcm.splitlines():
        m = re.match(r"^portmap_(\d+)\.0=", line)
        if m and int(m.group(1)) in group["logicals"]:
            if first:
                lines.extend(new_lines)
                first = False
            continue
        dm = re.match(r"^dport_map_port_(\d+)=(\d+)\s*$", line)
        if dm and int(dm.group(1)) in group["logicals"][1:]:
            require(int(dm.group(2)) == group["dports"][group["logicals"].index(int(dm.group(1)))], "SDK 显示端口编号已被外部更改。")
            continue
        lines.append(line)
    if count == 4:
        used = {int(m.group(1)) for line in lines for m in [re.match(r"^dport_map_port_\d+=(\d+)\s*$", line)] if m}
        require(not used.intersection(group["dports"][1:]), "SDK 显示编号已占用。")
        lines.extend("dport_map_port_{}={}".format(n, d) for n, d in zip(group["logicals"][1:], group["dports"][1:]))
    bcm = "\n".join(lines) + "\n"
    ini = source["port_config.ini"].decode().splitlines()
    columns = next(line.lstrip("# ").split() for line in ini if line.lstrip().startswith("#") and "lanes" in line and "name" in line)
    require(set(columns) >= {"name", "lanes", "alias", "index", "speed"}, "port_config.ini 表头不支持。")
    out, found = [], set()
    for line in ini:
        parts = line.split()
        if not parts or parts[0] not in names:
            out.append(line)
            continue
        require(len(parts) == len(columns), "port_config.ini 行格式异常。")
        found.add(parts[0])
        if parts[0] == parent:
            old = dict(zip(columns, parts))
            for i in range(count):
                row = {**old, **candidate["PORT"][names[i]], "name": names[i]}
                out.append(" ".join(str(row[key]) for key in columns))
    require(found == expected_names.intersection(names), "port_config.ini 的目标子口集合不一致。")
    after_lanes = _lanes(candidate)
    require(len(after_lanes) == len(set(after_lanes)) == 80 and set(after_lanes) == set(before_lanes), "候选配置改变了物理 lane 覆盖。")
    final_map = re.findall(r"^portmap_(\d+)\.0=(\d+):(\d+)\s*$", bcm, re.M)
    require(len(final_map) == len(candidate["PORT"]) and len({x[0] for x in final_map}) == len(final_map), "SDK 逻辑端口数量不一致。")
    require(len({x[1] for x in final_map}) == len(final_map), "SDK 物理起始端口冲突。")
    require(all(candidate.get(k) == config.get(k) for k in config if k not in ("PORT", "BREAKOUT_CFG")), "非目标配置被修改。")
    changed = {"platform.json": encoded(platform), "hwsku.json": encoded(hwsku), "port_config.ini": ("\n".join(out) + "\n").encode(), STATIC[0]: bcm.encode(), "config_db.json": encoded(candidate)}
    files = {name: {"path": TARGETS[name], "before": base64.b64encode(source[name]).decode(), "after": base64.b64encode(changed[name]).decode(), "before_sha256": digest(source[name]), "after_sha256": digest(changed[name])} for name in TARGETS}
    differences = {name: "".join(difflib.unified_diff(source[name].decode().splitlines(True), changed[name].decode().splitlines(True), fromfile="before/" + name, tofile="after/" + name)) for name in STATIC}
    changes = [{"target": "PORT|" + name, "before": config["PORT"].get(name), "after": candidate["PORT"].get(name)} for name in names if config["PORT"].get(name) != candidate["PORT"].get(name)]
    changes.append({"target": "BREAKOUT_CFG|" + parent, "before": config.get("BREAKOUT_CFG", {}).get(parent), "after": candidate["BREAKOUT_CFG"][parent]})
    return {"schema": 1, "method": "static-reboot", "values": dict(values), "build": BUILD,
            "boot_id": _boot_id(root), "revision": revision(config), "config_before": config, "config_after": candidate,
            "files": files, "static_diff": differences, "changes": changes,
            "summary": {"ports_before": len(config["PORT"]), "ports_after": len(candidate["PORT"]), "unique_lanes": 80, "parent": parent, "before": old_mode, "after": target, "requires_reboot": True, "preserves_parent_l3": True}}


def atomic_write(path, data, mode=0o600, owner=None):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stage(plan, operation_dir, *, current_config=None, root="/"):
    """Persist backups then atomically replace five files; running DB unchanged."""
    config = _read_config() if current_config is None else current_config
    require(revision(config) == plan["revision"], "运行配置已变化，拒绝执行旧计划。")
    regenerated = prepare(plan["values"], config, root=root)
    require(regenerated == plan, "镜像、启动会话或静态文件已变化，需重新生成计划。")
    directory = Path(operation_dir)
    require(directory.is_dir() and not directory.is_symlink() and directory.resolve() == directory.absolute(), "操作目录必须是可信普通目录。")
    require(not (directory / "manifest.json").exists(), "此操作已执行过，不重复写入。")
    metadata = {}
    for name, entry in plan["files"].items():
        target = _path(root, entry["path"])
        info = target.stat()
        metadata[name] = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        for section in ("original", "candidate"):
            (directory / section).mkdir(mode=0o700, exist_ok=True)
        atomic_write(directory / "original" / name, base64.b64decode(entry["before"]))
        atomic_write(directory / "candidate" / name, base64.b64decode(entry["after"]))
    atomic_write(directory / "running-before.json", encoded(config))
    manifest = {"schema": 1, "status": "applying", "build": BUILD, "boot_id_before": plan["boot_id"], "files": {n: {k: v for k, v in e.items() if k not in ("before", "after")} for n, e in plan["files"].items()}, "metadata": metadata, "summary": plan["summary"], "requires_reboot": True}
    atomic_write(directory / "manifest.json", encoded(manifest))
    written = []
    try:
        for name, entry in plan["files"].items():
            info = metadata[name]
            # Register before replace: directory fsync may fail after the new
            # file is already visible, so a returned exception is ambiguous.
            written.append(name)
            atomic_write(_path(root, entry["path"]), base64.b64decode(entry["after"]), info["mode"], (info["uid"], info["gid"]) if hasattr(os, "fchown") else None)
        if current_config is None:
            require(revision(_read_config()) == plan["revision"], "写文件期间发现外部运行配置变更。")
    except BaseException:
        restore_errors = []
        for name in reversed(written):
            entry, info = plan["files"][name], metadata[name]
            try:
                atomic_write(_path(root, entry["path"]), base64.b64decode(entry["before"]), info["mode"], (info["uid"], info["gid"]) if hasattr(os, "fchown") else None)
            except OSError as exc:
                restore_errors.append(name + ": " + str(exc))
        manifest["status"] = "stage_failed_restore_incomplete" if restore_errors else "stage_failed_original_files_restored"
        manifest["restore_errors"] = restore_errors
        atomic_write(directory / "manifest.json", encoded(manifest))
        raise
    manifest["status"] = "staged_pending_reboot"
    atomic_write(directory / "manifest.json", encoded(manifest))
    return manifest


def _runtime_tables():
    from swsscommon import swsscommon
    connector = swsscommon.SonicV2Connector(host="127.0.0.1")
    def table(database, prefix):
        dbid = getattr(connector, database)
        connector.connect(dbid, False)
        return {k[len(prefix):]: connector.get_all(dbid, k) or {} for k in connector.keys(dbid, prefix + "*") or []}
    return table("STATE_DB", "PORT_TABLE|"), table("APPL_DB", "PORT_TABLE:"), table("ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_PORT:")


def _runtime_asic_inventory():
    """Read discovered SAI hardware lanes and independently query SDK state.

    For statically created Broadcom ports HW_LANE_LIST is not recorded in each
    ASIC_STATE PORT hash. syncd stores the hardware-discovered lane -> real OID
    mapping in LANES. COUNTERS names use virtual OIDs, hence VIDTORID is required.
    """
    from swsscommon import swsscommon
    connector = swsscommon.SonicV2Connector(host="127.0.0.1")
    result = {}
    for database, key, label in (("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP", "port_names"),
                                 ("ASIC_DB", "VIDTORID", "vid_to_rid"), ("ASIC_DB", "LANES", "lanes")):
        dbid = getattr(connector, database)
        connector.connect(dbid, False)
        result[label] = connector.get_all(dbid, key) or {}
    for command, label in (("show portmap", "sdk_portmap"), ("ps", "sdk_status")):
        process = subprocess.run(["docker", "exec", "syncd", "bcmcmd", command], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=15, check=False)
        require(process.returncode == 0 and len(process.stdout) < 512000,
                "SDK 只读查询失败：" + command)
        result[label] = process.stdout
    return result


def _verify_asic(desired, asic_ports, inventory, bcm):
    """Require exact name/VID/RID/lane ownership AND actual SDK lane/speed."""
    errors = []
    names, translations, lanes_raw = [inventory.get(k, {}) for k in ("port_names", "vid_to_rid", "lanes")]
    require(all(isinstance(v, dict) for v in (names, translations, lanes_raw)), "ASIC 映射表格式无效。")
    if set(names) != set(desired):
        errors.append("COUNTERS_PORT_NAME_MAP 端口集合与计划不同。")
    vids = [names.get(name) for name in desired]
    rids = [translations.get(vid) for vid in vids]
    if None in vids or len(set(vids)) != len(desired):
        errors.append("SAI 端口 VID 缺失或重复。")
    if None in rids or len(set(rids)) != len(desired):
        errors.append("SAI 端口真实 RID 缺失或重复。")
    lanes = {int(k): v for k, v in lanes_raw.items()}
    wanted_lanes = {int(lane) for row in desired.values() for lane in row["lanes"].split(",")}
    if len(lanes) != len(lanes_raw) or set(lanes) != wanted_lanes or set(lanes.values()) != set(rids):
        errors.append("硬件 LANES 的 lane/RID 覆盖与计划不一致。")
    expected_sdk = {int(n): (int(p), int(s) * 1000) for n, p, s in re.findall(r"^portmap_(\d+)\.0=(\d+):(\d+)\s*$", bcm, re.M)}
    require(len(expected_sdk) == len(desired), "计划 SDK 端口映射数量不正确。")
    sdk_rows = re.findall(r"^\s*((?:xe|ce|hg)\d+)\s+([01])\s+(\d+)\s+(\d+)\s+", inventory.get("sdk_portmap", ""), re.M)
    sdk_map = {int(logical): int(physical) for name, pipe, logical, physical in sdk_rows}
    if len(sdk_rows) != len(sdk_map) or sdk_map != {n: row[0] for n, row in expected_sdk.items()}:
        errors.append("SDK show portmap 的逻辑/物理端口与计划不一致。")
    ps_rows = re.findall(r"^\s*((?:xe|ce|hg)\d+)\(\s*(\d+)\)\s+\S+\s+(\d+)\s+([0-9.]+)([GM])\b", inventory.get("sdk_status", ""), re.M)
    ps = {int(logical): (int(count), int(float(speed) * (1000 if unit == "G" else 1))) for name, logical, count, speed, unit in ps_rows}
    if len(ps_rows) != len(ps) or set(ps) != set(expected_sdk):
        errors.append("SDK ps 数据端口集合或诊断映射不完整。")
    for name, row in desired.items():
        wanted = sorted(map(int, row["lanes"].split(",")))
        vid = names.get(name)
        rid = translations.get(vid)
        actual_lanes = sorted(lane for lane, owner in lanes.items() if owner == rid)
        attrs = asic_ports.get(vid, {})
        if actual_lanes != wanted or str(attrs.get("SAI_PORT_ATTR_SPEED")) != str(row["speed"]):
            errors.append(name + " SAI VID/RID/硬件 lane/速率未确认。")
        expected_fec = {"rs": "SAI_PORT_FEC_MODE_RS", "fc": "SAI_PORT_FEC_MODE_FC", "none": "SAI_PORT_FEC_MODE_NONE"}.get(row.get("fec"))
        if expected_fec and attrs.get("SAI_PORT_ATTR_FEC_MODE") != expected_fec:
            errors.append(name + " SAI FEC 不一致。")
        if row.get("admin_status") in ("up", "down") and str(attrs.get("SAI_PORT_ATTR_ADMIN_STATE")).lower() != str(row["admin_status"] == "up").lower():
            errors.append(name + " SAI 管理状态不一致。")
        if "SAI_PORT_ATTR_HW_LANE_LIST" in attrs:
            count_text, lane_text = str(attrs["SAI_PORT_ATTR_HW_LANE_LIST"]).split(":", 1)
            listed = [int(x) for x in lane_text.split(",") if x]
            if int(count_text) != len(listed) or sorted(listed) != wanted:
                errors.append(name + " PORT HW_LANE_LIST 与硬件 LANES 不一致。")
        logical = next((n for n, (physical, speed) in expected_sdk.items() if physical == wanted[0]), None)
        if logical is None or expected_sdk[logical][1] != int(row["speed"]) or ps.get(logical) != (len(wanted), int(row["speed"])):
            errors.append(name + " SDK 实际 lane 数/速率不一致。")
    evidence = {"source": "COUNTERS_PORT_NAME_MAP -> VIDTORID -> ASIC_DB LANES + SDK show portmap/ps",
                "port_names": len(names), "unique_vids": len(set(vids)), "unique_rids": len(set(rids)),
                "hardware_lanes": len(lanes), "sdk_portmap_ports": len(sdk_map), "sdk_status_ports": len(ps)}
    return errors, evidence


def verify(operation_dir, *, config=None, state_ports=None, appl_ports=None, asic_ports=None, asic_inventory=None, root="/"):
    """Verify a later boot from real tables. Link-down alone is not a failure."""
    directory = Path(operation_dir)
    plan = json.loads((directory / "plan.json").read_bytes())
    manifest = json.loads((directory / "manifest.json").read_bytes())
    reverting = manifest.get("status") == "rollback_staged_pending_reboot"
    expected = plan["config_before"] if reverting else plan["config_after"]
    errors = []
    evidence = None
    if _boot_id(root) == manifest.get("rollback_boot_id", plan["boot_id"]):
        errors.append("尚未经过新的整机启动。")
    try:
        _build(root)
        config = _read_config() if config is None else config
        if revision(config) != revision(expected):
            errors.append("运行配置与计划完整配置不一致，包含非端口配置。")
        saved = json.loads(_path(root, TARGETS["config_db.json"]).read_bytes())
        if revision(saved) != revision(expected):
            errors.append("启动配置与计划完整配置不一致。")
        if state_ports is None or appl_ports is None or asic_ports is None:
            state_ports, appl_ports, asic_ports = _runtime_tables()
        if asic_inventory is None:
            asic_inventory = _runtime_asic_inventory()
        desired = expected["PORT"]
        if set(config.get("PORT", {})) != set(desired):
            errors.append("运行 PORT 集合与计划不一致。")
        bcm_entry = plan["files"][STATIC[0]]
        bcm = base64.b64decode(bcm_entry["before" if reverting else "after"]).decode()
        asic_errors, evidence = _verify_asic(desired, asic_ports, asic_inventory, bcm)
        errors.extend(asic_errors)
        for name, row in desired.items():
            keys = ("lanes", "speed", "index", "admin_status", "fec")
            if any(str(config.get("PORT", {}).get(name, {}).get(k)) != str(row.get(k)) for k in keys):
                errors.append(name + " CONFIG_DB 不一致。")
            if any(str(appl_ports.get(name, {}).get(k)) != str(row.get(k)) for k in ("lanes", "speed")):
                errors.append(name + " APPL_DB 未收敛。")
            # RC4 publishes speed in STATE_DB for link-up ports only. Every
            # port must be state=ok; ASIC and APPL_DB always prove configured
            # lane/speed, while a reported operational speed must also match.
            state = state_ports.get(name, {})
            if state.get("state") != "ok" or (state.get("speed") is not None and str(state["speed"]) != str(row["speed"])):
                errors.append(name + " STATE_DB 未就绪。")
        for name, entry in plan["files"].items():
            if name != "config_db.json" and digest(_path(root, entry["path"]).read_bytes()) != entry["before_sha256" if reverting else "after_sha256"]:
                errors.append(name + " 静态文件已变化。")
    except Exception as exc:
        errors.append("验收读取失败：" + str(exc))
    return {"success": not errors, "verified": not errors, "errors": errors, "asic_evidence": evidence,
            "requires_reboot": False, "rollback": reverting, "restored": reverting and not errors, "summary": plan["summary"]}


def rollback(operation_dir, *, current_config=None, root="/"):
    """Restore exact saved files only if no outside file/config changes occurred.

    The worker must reboot afterward. It must never silently call this on a
    failed verification: rollback is a separate operator-approved operation.
    """
    directory = Path(operation_dir)
    plan = json.loads((directory / "plan.json").read_bytes())
    manifest = json.loads((directory / "manifest.json").read_bytes())
    _build(root)
    observed = _read_config() if current_config is None else current_config
    require(revision(observed) in (plan["revision"], revision(plan["config_after"])), "运行配置存在后续更改，拒绝自动覆盖回退。")
    same_boot = _boot_id(root) == plan["boot_id"] and revision(observed) == plan["revision"]
    prior = {}
    for name, entry in plan["files"].items():
        observed_bytes = _path(root, entry["path"]).read_bytes()
        known_hashes = {entry["before_sha256"], entry["after_sha256"]}
        if name == "config_db.json":
            known_hashes.add(digest(encoded(plan["config_before"])))
        require(digest(observed_bytes) in known_hashes, "静态文件已被外部更改，拒绝覆盖：" + name)
        require(digest((directory / "original" / name).read_bytes()) == entry["before_sha256"], "备份校验失败。")
        prior[name] = observed_bytes
    manifest["status"] = "rollback_applying"
    atomic_write(directory / "manifest.json", encoded(manifest))
    attempted = []
    try:
        for name, entry in plan["files"].items():
            info = manifest["metadata"][name]
            data = encoded(plan["config_before"]) if name == "config_db.json" and not same_boot else (directory / "original" / name).read_bytes()
            attempted.append(name)
            atomic_write(_path(root, entry["path"]), data, info["mode"], (info["uid"], info["gid"]) if hasattr(os, "fchown") else None)
    except BaseException:
        restore_errors = []
        for name in reversed(attempted):
            entry, info = plan["files"][name], manifest["metadata"][name]
            try:
                atomic_write(_path(root, entry["path"]), prior[name], info["mode"], (info["uid"], info["gid"]) if hasattr(os, "fchown") else None)
            except OSError as exc:
                restore_errors.append(name + ": " + str(exc))
        # A retry is safe only while every file matches a known plan hash.
        manifest["status"] = "rollback_write_failed"
        manifest["restore_errors"] = restore_errors
        atomic_write(directory / "manifest.json", encoded(manifest))
        raise
    manifest["status"] = "rolled_back" if same_boot else "rollback_staged_pending_reboot"
    manifest["rollback_boot_id"] = _boot_id(root)
    atomic_write(directory / "manifest.json", encoded(manifest))
    return {"success": True, "verified": same_boot, "requires_reboot": not same_boot, "status": manifest["status"]}


def load_status(data_dir):
    """Only expose non-sensitive worker status; corrupt indexes fail closed."""
    base = Path(data_dir) / "breakout"
    try:
        current = json.loads((base / "current.json").read_bytes())
        identifier = current["id"]
        require(isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{32}", identifier), "无效拆分任务 ID。")
        status = json.loads((base / identifier / "status.json").read_bytes())
        manifest_path = base / identifier / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {}
        blocking = status.get("status") not in TERMINAL
        if status.get("status") == "failed":
            if status.get("files_restored") is True or not manifest or manifest.get("status") in ("rolled_back", "stage_failed_original_files_restored"):
                blocking = False
        if manifest.get("status") in ("applying", "stage_failed_restore_incomplete", "rollback_applying", "rollback_write_failed"):
            blocking = True  # Manifest wins over a stale terminal worker status.
        return {"id": identifier, "status": status.get("status", "unknown"), "message": str(status.get("message", "")), "verified": status.get("verified") is True,
                "blocking": blocking}
    except FileNotFoundError:
        if not (base / "current.json").exists():
            return None
        return {"id": None, "status": "unknown", "message": "拆分操作状态尚不可读；暂时禁止配置写入。", "verified": False, "blocking": True}
    except (ValueError, KeyError, OSError, BreakoutError):
        return {"id": None, "status": "unknown", "message": "拆分操作状态无效；请检查持久任务。", "verified": False, "blocking": True}
