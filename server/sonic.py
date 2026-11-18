"""Bounded SONiC reads and server-owned, reviewed configuration plans.

No shell, direct Redis writes, automatic rollback, or live-to-demo fallback.
BMC reads use a separately bounded, cached monitor. The HTTP layer owns authentication; this module independently validates
all operations, revisions, lifetimes and execution limits.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import port_breakout
from .fan_control import DemoFanController, control_metadata
from .bmc import BmcMonitor
from .metrics import MetricsSampler, number, sample_timestamp, temperature_row


class AdapterError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _utc(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def _normal(value):
    if isinstance(value, dict):
        return {"|".join(map(str, k)) if isinstance(k, tuple) else (k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)): _normal(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sort(value):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(value))]


SECRET_FIELD = re.compile(r"password|passwd|secret|passkey|community|private.?key|shared.?key|token|credential|auth.?key|priv.?key|(?:^|_)cak$|(?:^|_)psk$|^key$", re.I)


def _sanitize(value, field=""):
    if SECRET_FIELD.search(field):
        return "[redacted]"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            # SNMP community strings are Redis keys, not merely field values.
            if key == "SNMP_COMMUNITY" and isinstance(item, dict):
                result[key] = {f"[redacted-{i + 1}]": _sanitize(row)
                               for i, row in enumerate(item.values())}
            elif key == "NTP_KEY" and isinstance(item, dict):
                result[key] = {entry: {name: "[redacted]" if name == "value" else _sanitize(part, name)
                                      for name, part in row.items()} if isinstance(row, dict) else "[redacted]"
                               for entry, row in item.items()}
            else:
                result[key] = _sanitize(item, str(key))
        return result
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


CONFIG_PAGES = {
    "setup.interfaces": ("MGMT_PORT", "MGMT_INTERFACE", "MGMT_VRF_CONFIG"),
    "setup.routing": ("MGMT_INTERFACE", "MGMT_VRF_CONFIG"),
    "setup.hostname": ("DEVICE_METADATA",), "setup.dns": ("DNS_NAMESERVER",),
    "setup.login-messages": ("BANNER",), "setup.switch-profile": ("DEVICE_METADATA",),
    "setup.snmp": ("SNMP", "SNMP_COMMUNITY", "SNMP_USER", "SNMP_CONTACT", "SNMP_LOCATION"),
    "setup.logging": ("SYSLOG_SERVER", "SYSLOG_CONFIG", "LOGGER"),
    "setup.ntp": ("NTP", "NTP_SERVER"),
    "security.ssh": ("SSH_SERVER",), "security.aaa": ("AAA",),
    "security.radius": ("RADIUS", "RADIUS_SERVER"),
    "security.tacacs": ("TACPLUS", "TACPLUS_SERVER"),
    "ports.mirror": ("MIRROR_SESSION",),
    "ports.telemetry": ("TELEMETRY", "GNMI", "FEATURE"),
    "ethernet.stp": ("STP", "STP_PORT", "STP_VLAN", "STP_VLAN_PORT"),
    "ethernet.lag": ("PORTCHANNEL", "PORTCHANNEL_MEMBER"),
    "ethernet.vlan": ("VLAN", "VLAN_MEMBER"),
    "ethernet.mlag": ("MCLAG_DOMAIN", "MCLAG_INTERFACE", "MCLAG_UNIQUE_IP"),
    "ethernet.igmp": ("IGMP_SNOOPING", "IGMP_SNOOPING_VLAN"),
    "ethernet.acl": ("ACL_TABLE", "ACL_RULE", "ACL_TABLE_TYPE"),
    "ethernet.pfc": ("PORT_QOS_MAP", "PFC_WD", "QUEUE", "SCHEDULER", "WRED_PROFILE", "BUFFER_PROFILE", "BUFFER_PG", "BUFFER_QUEUE"),
    "ethernet.bgp": ("BGP_GLOBALS", "BGP_NEIGHBOR", "BGP_PEER_RANGE", "BGP_MONITORS"),
    "routing.global": ("VRF", "DEVICE_METADATA"),
    "routing.interfaces": ("INTERFACE", "VLAN_INTERFACE", "PORTCHANNEL_INTERFACE", "LOOPBACK_INTERFACE", "VRF"),
}
UNSUPPORTED = {
    "setup.ipsec": "当前适配器未发现并验证 SONiC IPsec 管理接口。",
    "setup.virtualization": "Onyx 虚拟化管理不是通用 SONiC 能力；容器请见容器页。",
    "setup.email-alerts": "未配置或验证可供此服务管理的邮件告警后端。",
    "setup.xml-gateway": "Onyx XML Gateway 不属于此 SONiC 平台。",
    "setup.licensing": "没有发现适用于此开源 SONiC 镜像的许可证管理接口。",
    "security.admin-password": "账户/密码写入尚未实现；不通过浏览器直接执行用户管理命令。",
    "security.ldap": "此适配器尚未验证 LDAP 配置模型。",
    "security.certificates": "证书生命周期管理尚未实现；不读取或导出私钥。",
    "status.wjh": "What Just Happened 是厂商特定功能；此 Broadcom 平台没有已验证接口。",
    "status.virtualization": "未实现独立虚拟机管理后端；Docker 状态在容器页。",
    "system.reboot": "此版本不提供重启/断电写操作；请使用受控维护流程。",
}
STATE_PAGES = {
    "status.temperature": ("TEMPERATURE_INFO",),
    "status.fans": ("FAN_INFO", "FAN_DRAWER_INFO"),
    "status.power": ("PSU_INFO",), "system.power": ("PSU_INFO",),
    "system.inventory": ("CHASSIS_INFO", "EEPROM_INFO", "PHYSICAL_ENTITY_INFO", "TRANSCEIVER_INFO"),
    "status.alerts": ("SYSTEM_HEALTH_INFO",),
}
ACTION_PATHS = {
    "vlan.create": ("vlan", "add"), "vlan.delete": ("vlan", "del"),
    "vlan.member.add": ("vlan", "member", "add"), "vlan.member.delete": ("vlan", "member", "del"),
    "lag.create": ("portchannel", "add"), "lag.delete": ("portchannel", "del"),
    "lag.member.add": ("portchannel", "member", "add"), "lag.member.delete": ("portchannel", "member", "del"),
    "interface.address.add": ("interface", "ip", "add"),
    "interface.address.delete": ("interface", "ip", "remove"),
    "route.add": ("route", "add"), "route.delete": ("route", "del"),
    "system.hostname": ("hostname",), "ntp.add": ("ntp", "add"),
    "ntp.delete": ("ntp", "del"), "config.save": ("save",),
    "dns.add": ("dns", "nameserver", "add"), "dns.delete": ("dns", "nameserver", "del"),
    "bgp.neighbor.startup": ("bgp", "startup", "neighbor"),
    "bgp.neighbor.shutdown": ("bgp", "shutdown", "neighbor"),
    "pfc.priority": ("interface", "pfc", "priority"),
    "mirror.span.add": ("mirror_session", "span", "add"),
    "mirror.delete": ("mirror_session", "remove"),
}
PORT_PATHS = {"admin_status": (("interface", "startup"), ("interface", "shutdown")),
              "mtu": (("interface", "mtu"),), "fec": (("interface", "fec"),),
              "speed": (("interface", "speed"),), "description": (("interface", "description"),)}
PAGE_ACTIONS = {
    "status.fans": ("fan.control",),
    "ports.ports": ("port.configure", "port.breakout"), "ports.phy": ("port.configure", "port.breakout"),
    "ethernet.vlan": ("vlan.create", "vlan.delete", "vlan.member.add", "vlan.member.delete"),
    "ethernet.lag": ("lag.create", "lag.delete", "lag.member.add", "lag.member.delete"),
    "routing.interfaces": ("interface.address.add", "interface.address.delete"),
    "routing.routes": ("route.add", "route.delete"),
    "setup.hostname": ("system.hostname",), "setup.ntp": ("ntp.add", "ntp.delete"),
    "setup.configurations": ("config.save",),
    "setup.dns": ("dns.add", "dns.delete"),
    "ethernet.bgp": ("bgp.neighbor.startup", "bgp.neighbor.shutdown"),
    "ethernet.pfc": ("pfc.priority",),
    "ports.mirror": ("mirror.span.add", "mirror.delete"),
}


class SonicAdapter:
    CACHE_SECONDS = 10
    PLAN_SECONDS = 300
    APPLY_SECONDS = 120
    MAX_COMMANDS = 64

    def __init__(self, data_dir: str, demo: bool = False):
        self.data_dir = Path(data_dir)
        self.demo = bool(demo)
        self._cache = {}
        self._cache_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._plans_lock = threading.Lock()
        self._plans = {}
        self._counter_previous = {}
        self._metrics_sampler = MetricsSampler()
        self._db = None
        self._pending_save = False
        self._demo_config = self._seed_demo() if self.demo else None
        self._demo_saved = copy.deepcopy(self._demo_config)
        self._fan_control = DemoFanController(demo=True) if self.demo else None
        self._bmc = BmcMonitor(demo=self.demo)
        catalog_path = Path(__file__).resolve().parents[1] / "shared" / "catalog.json"
        self._catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self._pages = {p["id"]: p for g in self._catalog["groups"] for p in g["pages"]}
        self._profiles = [json.loads(p.read_text(encoding="utf-8"))
                          for p in sorted((catalog_path.parent / "platforms").glob("*.json"))]

    def _cached(self, key, loader, ttl=None, fresh=False):
        with self._cache_lock:
            now = time.monotonic()
            item = self._cache.get(key)
            if not fresh and item and now - item[0] < (self.CACHE_SECONDS if ttl is None else ttl):
                return copy.deepcopy(item[1])
            value = loader()
            self._cache[key] = (time.monotonic(), copy.deepcopy(value))
            return value

    def _invalidate(self):
        with self._cache_lock:
            self._cache.clear()

    def _run(self, argv, timeout=10):
        """Internal fixed argv only. Temporary files bound memory, timeout bounds work."""
        if self.demo:
            raise AdapterError("DEMO cannot execute a host command", 500)
        executable = shutil.which(argv[0])
        if not executable:
            raise AdapterError(f"命令不可用：{argv[0]}", 503)
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           shell=False, start_new_session=os.name == "posix",
                                           env={**os.environ, "LC_ALL": "C", "LANG": "C"})
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
                raise AdapterError("命令超时；若为写操作，执行结果可能不确定，需重新读取配置核对。", 504) from exc
            output.seek(0)
            raw = output.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise AdapterError("命令结果超过 4 MiB 上限。", 502)
        text = raw.decode("utf-8", "replace")
        if process.returncode:
            raise AdapterError(f"命令失败 ({process.returncode})：{text[:1200].strip()}", 502)
        return text

    def _load_config(self):
        if self.demo:
            return copy.deepcopy(self._demo_config)
        try:
            from swsscommon import swsscommon
            connection = swsscommon.ConfigDBConnector()
            connection.connect(wait_for_init=False)
            result = _normal(connection.get_config())
        except (ImportError, AttributeError, TypeError):
            try:
                result = json.loads(self._run(["sonic-cfggen", "-d", "--print-data"]))
            except (ValueError, AdapterError) as exc:
                raise AdapterError("无法读取真实 SONiC CONFIG_DB；live 模式不会回退到模拟数据。", 503) from exc
        except Exception as exc:
            raise AdapterError("SONiC CONFIG_DB 连接失败；未切换到模拟数据。", 503) from exc
        if not isinstance(result, dict):
            raise AdapterError("CONFIG_DB 返回格式不正确。", 502)
        return result

    def _config(self, fresh=False):
        return self._cached("config", self._load_config, fresh=fresh)

    def _startup_config(self):
        if self.demo:
            return copy.deepcopy(self._demo_saved)
        try:
            value = json.loads(Path("/etc/sonic/config_db.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return _normal(value)
        except (OSError, ValueError) as exc:
            raise AdapterError("启动配置无法读取或解析，保存状态未知。", 503) from exc

    def _save_status(self, config=None):
        operation = self._breakout_status()
        if operation and operation["blocking"]:
            return None, "拆分/合并操作尚未完成；启动配置可能已暂存，暂时禁止保存覆盖。"
        try:
            running = self._config() if config is None else config
            saved = self._cached("startup_config", self._startup_config)
            pending = _revision(_normal(running)) != _revision(_normal(saved))
            return pending, "运行配置与启动 JSON 的规范化比较；包含外部 CLI 和系统生成字段。"
        except AdapterError as exc:
            return None, str(exc)

    def _multi_asic(self):
        meta = self._config().get("DEVICE_METADATA", {}).get("localhost", {})
        if (_num(meta.get("num_asic")) or 1) > 1 or any(Path("/var/run/netns").glob("asic*")):
            return True
        try:
            text = Path("/etc/sonic/asic.conf").read_text()
            found = re.search(r"(?m)^\s*NUM_ASIC\s*=\s*(\d+)", text)
            return bool(found and int(found.group(1)) > 1)
        except OSError:
            return False

    def _hpe_profile(self, config):
        return self._profile_for(config) is not None

    def _profile_for(self, config):
        meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
        matches = [profile for profile in self._profiles
                   if meta.get("platform") == profile["exact_match"]["platform"]
                   and meta.get("hwsku") == profile["exact_match"]["hwsku"]]
        if not matches:
            return None
        if not self.demo:
            eeprom, error = self._optional_table("STATE_DB", "EEPROM_INFO")
            if error:
                return None
            matches = [p for p in matches if re.search(p["exact_match"]["eeprom_regex"], json.dumps(eeprom))]
        if len(matches) != 1:
            return None
        profile = matches[0]
        expected = {int(index): set(row["lanes"]) for index, row in profile["cages"].items()}
        actual = {}
        for row in config.get("PORT", {}).values():
            index = _num(row.get("index"))
            try:
                lane_list = [int(v) for v in str(row.get("lanes", "")).split(",")]
            except ValueError:
                return None
            lanes = set(lane_list)
            if index not in expected or not lanes or len(lanes) != len(lane_list) or not lanes <= expected[index] or actual.get(index, set()) & lanes:
                return None
            actual.setdefault(index, set()).update(lanes)
        return profile if actual == expected else None

    def _db_read(self, database, key):
        if self.demo:
            return {}
        try:
            from swsscommon import swsscommon
            if self._db is None:
                self._db = swsscommon.SonicV2Connector(host="127.0.0.1")
            db_id = getattr(self._db, database)
            self._db.connect(db_id, False)
            return _normal(self._db.get_all(db_id, key) or {})
        except Exception as exc:
            raise AdapterError(f"{database} 读取暂不可用；尚未获得实时状态。", 503) from exc

    def _db_table(self, database, table, separator="|"):
        def read():
            if self.demo:
                return self._demo_state(table)
            try:
                from swsscommon import swsscommon
                if self._db is None:
                    self._db = swsscommon.SonicV2Connector(host="127.0.0.1")
                db_id = getattr(self._db, database)
                self._db.connect(db_id, False)
                prefix = table + separator
                keys = self._db.keys(db_id, prefix + "*") or []
                if len(keys) > 5000:
                    raise AdapterError(f"{table} 超过 5000 行，请使用专项查询。", 413)
                return {str(_normal(key))[len(prefix):]: _normal(self._db.get_all(db_id, key) or {}) for key in keys}
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(f"{database}/{table} 暂不可读。", 503) from exc
        return self._cached((database, table), read)

    def _optional_table(self, database, table, separator="|"):
        try:
            return self._db_table(database, table, separator), None
        except AdapterError as exc:
            return {}, str(exc)

    def _command_read(self, argv, ttl=15):
        return self._cached(("cmd", *argv), lambda: self._run(argv), ttl=ttl)

    def _help(self, path):
        if self.demo:
            return "Usage: config " + " ".join(path) + " [OPTIONS] (DEMO)"
        return self._cached(("help", *path), lambda: self._run(["config", *path, "--help"], timeout=8), ttl=600)

    def _available(self, path):
        def probe():
            try:
                text = self._help(path)
                return "Usage:" in text and "No such command" not in text and "COMMAND [ARGS]" not in text
            except AdapterError:
                return False
        return self._cached(("available", *path), probe, ttl=600)

    def _port_fields(self):
        return [field for field, paths in PORT_PATHS.items() if all(self._available(p) for p in paths)]

    def _sudo_helper_available(self):
        """Stock IP deletion uses sudo for its fail-closed route protection.

        Test only a fixed no-op, never the configuration command. Keep the
        service's NoNewPrivileges/capability policy unchanged.
        """
        if self.demo:
            return True
        def probe():
            try:
                self._run(["sudo", "-n", "/usr/bin/true"], timeout=5)
                return True
            except (AdapterError, OSError):
                return False
        return self._cached("sudo_helper_available", probe, ttl=300)

    def _action_unavailable_reason(self, action):
        operation = self._breakout_status()
        if operation and operation["blocking"]:
            return "拆分/合并任务尚未完成或恢复未确认，暂时禁止其他配置写入。"
        if action == "port.breakout":
            return "当前没有经过本机镜像验证且可执行的拆分/合并模式。"
        if action == "fan.control":
            return self._fan_status()["reason"]
        if action == "interface.address.delete" and not self._sudo_helper_available():
            return "当前服务权限无法执行 SONiC CLI 的路由保护前置检查，暂不提供 IP 地址删除。"
        return "该操作的 SONiC CLI 尚未验证或当前不可用。"

    def _action_available(self, action):
        operation = self._breakout_status()
        if operation and operation["blocking"]:
            return False
        if not self.demo and self._multi_asic():
            return False  # Namespace-specific write plans are not implemented yet.
        if action == "port.configure":
            return bool(self._port_fields())
        if action == "fan.control":
            return self.demo and self._fan_status()["write_supported"]
        if action == "port.breakout":
            return any(mode["available"] for entry in self._breakout_catalog(self._config()).values() for mode in entry["modes"])
        if action == "interface.address.delete" and not self._sudo_helper_available():
            return False
        return action in ACTION_PATHS and self._available(ACTION_PATHS[action])

    def _breakout_status(self):
        return None if self.demo else port_breakout.load_status(self.data_dir)

    def _fan_status(self, config=None):
        config = self._config() if config is None else config
        return self._fan_control.metadata(config) if self._fan_control else control_metadata(config)

    def close(self):
        self._metrics_sampler.close()
        if self._fan_control:
            self._fan_control.close()

    def _breakout_verified(self, parent):
        if self.demo:
            return True
        try:
            marker = json.loads((self.data_dir / "breakout" / "verified.json").read_bytes())
            return (marker.get("schema") == 1 and marker.get("verified") is True
                    and marker.get("platform") == port_breakout.PLATFORM and marker.get("hwsku") == port_breakout.HWSKU
                    and marker.get("build") == self._version().get("build_version") == port_breakout.BUILD
                    and set(marker.get("modes", {}).get(parent, [])) >= {"1x100G", "4x25G"}
                    and Path("/host/sonic-web/current/deploy/breakout_worker.py").is_file()
                    and Path("/usr/bin/systemd-run").is_file())
        except (OSError, ValueError, TypeError):
            return False

    def _breakout_catalog(self, config):
        """Discover declared modes; activation is separate from mode discovery."""
        profile = self._profile_for(config)
        result = {}
        if profile:
            for parent, group in port_breakout.GROUPS.items():
                try:
                    current = port_breakout._mode(config, parent)
                    consistent = True
                except (port_breakout.BreakoutError, KeyError, ValueError):
                    current, consistent = "unknown", False
                verified = self._breakout_verified(parent)
                result[parent] = {"parent_interface": parent, "index": group["index"], "current_mode": current, "modes": []}
                for mode in ("1x100G", "4x25G"):
                    available = verified and consistent and mode != current
                    reason = ("当前已是此模式。" if mode == current else "需要整机重启，所有业务链路暂时中断。" if available
                              else "当前端口速率/lane 与静态拆分基线不同；请先恢复 100G。" if not consistent
                              else "静态拆分适配已准备；当前镜像的本机硬件验收尚未完成。")
                    result[parent]["modes"].append({"value": "breakout:" + mode, "label": "4 × 25G" if mode == "4x25G" else "合并为 1 × 100G",
                        "kind": "breakout", "breakout_mode": mode, "parent_interface": parent, "available": available,
                        "verified": verified, "reason": reason, "disruptive": True, "method": "static-reboot", "requires_reboot": True})
            return result
        # Other platforms retain their advertised capabilities without claiming
        # that a JSON declaration alone proves SAI/SDK dynamic-port support.
        meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", str(meta.get(k, ""))) for k in ("platform", "hwsku")):
            return result
        path = Path("/usr/share/sonic/device") / meta["platform"] / meta["hwsku"] / "platform.json"
        try:
            platform = self._cached(("breakout-platform", str(path)), lambda: json.loads(path.read_bytes()), ttl=30)
            for parent, data in platform.get("interfaces", {}).items():
                if not re.fullmatch(r"Ethernet\d+", parent):
                    continue
                modes = [m for m in data.get("breakout_modes", {}) if re.fullmatch(r"\d+x\d+G", m)]
                if len(modes) < 2:
                    continue
                result[parent] = {"parent_interface": parent, "index": str(data.get("index", "")).split(",")[0],
                    "current_mode": config.get("BREAKOUT_CFG", {}).get(parent, {}).get("brkout_mode", "unknown"),
                    "modes": [{"value": "breakout:" + m, "label": m, "kind": "breakout", "breakout_mode": m,
                        "parent_interface": parent, "available": False, "verified": False, "method": "dynamic", "requires_reboot": False,
                        "disruptive": True, "reason": "平台文件声明此模式；当前适配器尚未完成该平台的动态拆分执行验证。"} for m in modes]}
        except (OSError, ValueError, TypeError):
            pass
        return result

    def _port_modes(self, name, row, state, breakout):
        raw = state.get("supported_speeds") or row.get("supported_speeds") or row.get("speed", "")
        values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
        modes = [{"value": "speed:" + str(v), "label": f"{v / 1000:g}G", "kind": "speed", "speed": v, "available": True}
                 for v in sorted({_num(x) for x in values if _num(x) and 10 <= _num(x) <= 800000})]
        entry = next((v for v in breakout.values() if str(row.get("index")) == v["index"]), None)
        if entry:
            modes += copy.deepcopy(entry["modes"])
        return modes, entry

    def catalog(self):
        result = copy.deepcopy(self._catalog)
        for group in result["groups"]:
            for page in group["pages"]:
                reason = UNSUPPORTED.get(page["id"], "真实数据由当前 SONiC 能力决定；未配置的功能显示为空或不可用。")
                page["capability"] = {"read": page["id"] not in UNSUPPORTED, "write": False, "reason": reason}
        result["mode"] = "demo" if self.demo else "live"
        return result

    def _system_metrics(self):
        if self.demo:
            return {"cpu_percent": 21.0, "memory_percent": 14.0, "uptime": 86320, "simulated": True,
                    "cpu_state": "ok", "cpu_warning": None, "memory_warning": None,
                    "cpu_sample_seconds": 2.0, "metrics_sampled_at": _utc(), "metrics_age_seconds": 0,
                    "metrics_source": "DEMO 模拟样本", "cpu_definition": "DEMO 模拟主机 CPU 占用。"}
        self._metrics_sampler.start()
        return self._metrics_sampler.snapshot(wait=0.25)

    def _version(self):
        if self.demo:
            return {"build_version": "DEMO — simulated SONiC", "kernel_version": "simulated"}
        try:
            return {key.strip(): value.strip().strip("'\"") for line in Path("/etc/sonic/sonic_version.yml").read_text().splitlines()
                    if ":" in line and not line.lstrip().startswith("#") for key, value in [line.split(":", 1)]}
        except OSError:
            return {}

    def _health_state(self):
        def read():
            if self.demo:
                return self._demo_state("SYSTEM_HEALTH_INFO")
            # RC4 publishes one hash, not SYSTEM_HEALTH_INFO|<name> rows.
            row = self._db_read("STATE_DB", "SYSTEM_HEALTH_INFO")
            if row:
                return {"System": row}
            return self._db_table("STATE_DB", "SYSTEM_HEALTH_INFO")
        try:
            return self._cached("health_state", read), None
        except AdapterError as exc:
            return {}, str(exc)

    def overview(self):
        def read():
            config = self._config()
            meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
            health, error = self._health_state()
            health_rows = list(health.values())
            status = next((r.get("summary", r.get("status")) for r in health_rows if r.get("summary", r.get("status"))), "unknown")
            ports = self.ports()["items"]
            return {"device": {"hostname": meta.get("hostname", "unknown"), "model": meta.get("model", meta.get("hwsku", "unknown")),
                               "platform": meta.get("platform", "unknown"), "hwsku": meta.get("hwsku", "unknown"),
                               "version": self._version().get("build_version", "unknown")},
                    "health": {"status": status, "message": error or ("；".join(str(r.get("message") or r.get("reason") or ("系统健康正常" if r.get("summary", r.get("status")) == "OK" else r.get("summary", r.get("status", "状态已发布")))) for r in health_rows) if health_rows else "状态尚未发布，正在等待监控数据。")},
                    "summary": [{"label": "逻辑端口", "value": len(ports)}, {"label": "链路 Up", "value": sum(p["oper_status"] == "up" for p in ports)},
                                {"label": "VLAN", "value": len(config.get("VLAN", {}))}, {"label": "LAG", "value": len(config.get("PORTCHANNEL", {}))}],
                    "mode": "demo" if self.demo else "live", "sampled_at": _utc(), "revision": _revision(config)}
        result = self._cached("overview", read)
        # Host metrics have their own sampler and must not inherit the slower
        # configuration/overview cache, including its first unknown sample.
        result["device"].update(self._system_metrics())
        result["pending_save"], result["pending_save_reason"] = self._save_status()
        result["operation"] = self._breakout_status()
        return result

    def _port_counters(self):
        def read():
            if self.demo:
                return {name: {"rx_bps": 12500000 if name in ("Ethernet52", "Ethernet64") else 0,
                               "tx_bps": 12000000 if name in ("Ethernet52", "Ethernet64") else 0,
                               "rx_errors": 0, "tx_errors": 0, "raw": {"demo": "simulated"}}
                        for name in self._demo_config["PORT"]}
            mapping = self._db_read("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP")
            now, result = time.monotonic(), {}
            for name, oid in list(mapping.items())[:1024]:
                row = self._db_read("COUNTERS_DB", "COUNTERS:" + str(oid))
                rx, tx = _num(row.get("SAI_PORT_STAT_IF_IN_OCTETS")), _num(row.get("SAI_PORT_STAT_IF_OUT_OCTETS"))
                old = self._counter_previous.get(name)
                rates = [None, None]
                if old and now > old[0]:
                    for i, current in enumerate((rx, tx)):
                        if current is not None and old[i + 1] is not None and current >= old[i + 1]:
                            rates[i] = round(8 * (current - old[i + 1]) / (now - old[0]), 2)
                self._counter_previous[name] = (now, rx, tx)
                result[name] = {"rx_bps": rates[0], "tx_bps": rates[1],
                                "rx_errors": _num(row.get("SAI_PORT_STAT_IF_IN_ERRORS")), "tx_errors": _num(row.get("SAI_PORT_STAT_IF_OUT_ERRORS")), "raw": row}
            return result
        return self._cached("counters", read)

    def ports(self):
        def read():
            config = self._config()
            state, error = self._optional_table("STATE_DB", "PORT_TABLE")
            appl, appl_error = self._optional_table("APPL_DB", "PORT_TABLE", ":")
            try:
                counters = self._port_counters()
            except AdapterError:
                counters = {}
            meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
            profile = self._profile_for(config)
            breakout = self._breakout_catalog(config)
            items = []
            for name, row in sorted(config.get("PORT", {}).items(), key=lambda pair: _sort(pair[0])):
                lanes = [_num(v) for v in str(row.get("lanes", "")).split(",") if v.strip()]
                index = _num(row.get("index"))
                cage, connector = None, "unknown"
                # Verified logical module association, never row/column geometry.
                if profile and str(index) in profile["cages"]:
                    cage = profile["cages"][str(index)]["cage_id"]
                    connector = profile["cages"][str(index)]["connector"]
                elif index is not None:
                    cage = "module-index:" + str(index)
                live = {**state.get(name, {}), **appl.get(name, {})}
                speed = _num(row.get("speed"))
                c = counters.get(name, {})
                modes, breakout_info = self._port_modes(name, row, state.get(name, {}), breakout)
                items.append({"name": name, "alias": row.get("alias", name), "cage_id": cage, "index": index, "lanes": lanes,
                              "speed": speed, "admin_status": row.get("admin_status", "unknown"), "oper_status": live.get("oper_status", "unknown"),
                              "mtu": _num(row.get("mtu")), "fec": row.get("fec", "unknown"), "description": row.get("description", ""),
                              "connector": connector, "breakout_mode": "unknown", "port_modes": modes, "breakout": breakout_info,
                              **{k: c.get(k) for k in ("rx_bps", "tx_bps", "rx_errors", "tx_errors")}})
            groups = {}
            for row in items:
                if row["cage_id"]:
                    groups.setdefault(row["cage_id"], []).append(row)
            for members in groups.values():
                speeds = {r["speed"] for r in members}
                mode = f"{len(members)}x{next(iter(speeds)) // 1000}G" if len(speeds) == 1 and next(iter(speeds)) else "mixed/unknown"
                for row in members:
                    row["breakout_mode"] = mode
            multi = self._multi_asic()
            return {"items": items, "layout": {"kind": "logical", "verified": False, "binding_verified": bool(profile),
                    "profile_id": profile["id"] if profile else None,
                    "message": "逻辑模块示意；没有已验证的机身行列坐标。alias/index 不代表面板位置。" + ("机型身份与 lane/index 已按独立 profile 核对。" if profile else "精确 OEM/索引关联未验证，使用通用逻辑列表。")},
                    "sampled_at": _utc(), "revision": _revision(config), "state_warning": error or appl_error,
                    "mode": "demo" if self.demo else "live", "namespace_scope": "host/default only; multi-ASIC namespaces not aggregated" if multi else "default",
                    "multi_asic": multi}
        result = self._cached("ports", read)
        result["operation"] = self._breakout_status()
        return result

    def port_detail(self, name):
        data = self.ports()
        port = next((p for p in data["items"] if p["name"] == name), None)
        if port is None:
            raise AdapterError("未知端口。", 404)
        config = self._config()
        transceiver, error = self._optional_table("STATE_DB", "TRANSCEIVER_INFO")
        dom, dom_error = self._optional_table("STATE_DB", "TRANSCEIVER_DOM_SENSOR")
        try:
            counters = self._port_counters().get(name, {}).get("raw", {})
        except AdapterError:
            counters = {}
        related = []
        for table in ("VLAN_MEMBER", "PORTCHANNEL_MEMBER", "INTERFACE", "ACL_TABLE"):
            for key, value in config.get(table, {}).items():
                if name in key.split("|") or name in str(value.get("ports", "")).split(","):
                    related.append({"label": f"{table}:{key}", "value": _sanitize(value)})
        module = {**transceiver.get(name, {}), **dom.get(name, {})}
        return {"port": port, "properties": [{"label": k, "value": v} for k, v in port.items() if k not in ("port_modes", "breakout")],
                "counters": [{"label": k, "value": v} for k, v in counters.items()],
                "transceiver": [{"label": k, "value": v} for k, v in module.items()] or [{"label": "状态", "value": error or dom_error or "模块数据尚未发布；不等同于不存在。"}],
                "relationships": related, "capabilities": {"configure": self._action_available("port.configure"),
                "fields": self._port_fields(), "dynamic_breakout": any(m.get("available") and m.get("method") == "dynamic" for m in port["port_modes"]),
                "port_modes": port["port_modes"], "breakout": port["breakout"],
                "reason": "速率根据端口发布的能力筛选；拆分/合并同时标明执行方式和验证状态。"},
                "sampled_at": data["sampled_at"], "revision": data["revision"]}

    def export_config(self):
        return _sanitize(self._config())

    @staticmethod
    def _rows(tables, config):
        return [{"table": table, "key": key, **({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()} if isinstance(row, dict) else {"value": row})}
                for table in tables for key, row in config.get(table, {}).items()][:2000]

    def _bmc_sensors(self):
        """Reuse PMON samples; never add per-sensor IPMI calls from the Web UI."""
        rows, errors = [], []
        for table, category, field, unit in (("TEMPERATURE_INFO", "温度", "temperature", "°C"),
                                             ("FAN_INFO", "风扇转速占比", "speed", "%"),
                                             ("PSU_INFO", "电源功率", "power", "W")):
            values, error = self._optional_table("STATE_DB", table)
            if error:
                errors.append(error)
            for name, item in sorted(values.items(), key=lambda entry: _sort(entry[0])):
                timestamp = item.get("timestamp")
                display_timestamp, age, fresh = sample_timestamp(timestamp)
                if table == "TEMPERATURE_INFO":
                    normalized = temperature_row(name, item, simulated=self.demo)
                    rows.append({**normalized, "category": category, "value": normalized["temperature"]})
                    continue
                else:
                    presence, status = str(item.get("presence", "")).lower(), str(item.get("status", "")).lower()
                    state = "未在位" if presence == "false" else "异常" if status == "false" else "正常" if presence == status == "true" else "未知"
                if not fresh:
                    state = "数据过期或时间未知"
                rows.append({"category": category, "name": name, "value": number(item.get(field)), "unit": unit,
                             "status": state, "timestamp": display_timestamp, "raw_timestamp": timestamp,
                             "age_seconds": age, "fresh": fresh})
        return {"rows": rows[:1000], "error": "; ".join(errors) or None,
                "source": "DEMO 模拟样本" if self.demo else "PMON / STATE_DB 共享缓存"}

    def page(self, page_id):
        if page_id not in self._pages:
            raise AdapterError("未知页面。", 404)
        if page_id == "system.console":
            # Console availability comes from its own authenticated endpoint.
            # Do not require a healthy CONFIG_DB or a slow IPMI poll to open it.
            return {"id": page_id, "title": "网页终端", "description": ("DEMO：终端入口不连接真实设备。" if self.demo else "SONiC 网页终端与 BMC 自身系统终端。"),
                    "columns": [], "rows": [], "summary": [], "actions": [],
                    "capability": {"read": True, "write": False,
                                   "reason": "连接能力由终端服务独立检查；仅管理员可连接，命令按系统账户权限直接执行。"},
                    "sampled_at": _utc(), "revision": None, "pending_save": None}
        config = self._config()
        data = {"id": page_id, "title": self._pages[page_id]["label"], "description": "真实 SONiC 配置与运行状态" if not self.demo else "DEMO：以下为明确模拟的数据，所有修改仅在内存中模拟。",
                "columns": [], "rows": [], "summary": [], "actions": [],
                "capability": {"read": True, "write": False, "reason": "本页提供状态查看。"},
                "sampled_at": _utc(), "revision": _revision(config)}
        data["pending_save"], data["pending_save_reason"] = self._save_status(config)
        data["operation"] = self._breakout_status()
        try:
            if page_id in UNSUPPORTED:
                data["capability"] = {"read": False, "write": False, "reason": UNSUPPORTED[page_id]}
            elif page_id == "status.bmc":
                data["bmc"] = self._bmc.snapshot()
                data["bmc"]["sensors"] = self._bmc_sensors()
                data["sampled_at"] = data["bmc"]["collection"]["sampled_at"]
                data["description"] = ("DEMO：模拟 BMC 监控；不会访问本机 IPMI 或连接真实串口。" if self.demo else
                                       "BMC 身份、健康、网络与 SOL 配置、近期事件和平台传感器。")
                data["capability"]["reason"] = "BMC 状态只读并共享缓存；传感器复用 PMON 数据。历史事件不代表当前仍有故障。"
                data["capability"]["read"] = any(data["bmc"][section]["state"] in ("ok", "partial")
                                                for section in ("identity", "status", "network", "console", "events"))
            elif page_id in ("ports.ports", "ports.phy", "system.modules", "status.network"):
                data.update({k: v for k, v in self.ports().items() if k in ("layout", "sampled_at", "revision")})
                data["rows"] = self.ports()["items"]
                data["description"] += "；模块示意不代表已验证的机身行列。"
            elif page_id in ("setup.interfaces", "setup.routing"):
                data["rows"] = self._rows(CONFIG_PAGES[page_id], self.export_config())
                names = set(config.get("MGMT_PORT", {}))
                names.update(k.split("|")[0] for k in config.get("MGMT_INTERFACE", {}))
                if not names:
                    names = {"eth0"}
                for name in sorted(names):
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,31}", name):
                        continue
                    if self.demo:
                        data["rows"].append({"source": "DEMO", "interface": name, "state": "simulated DHCP"})
                        continue
                    args = ["ip", "-j", "address", "show", "dev", name] if page_id == "setup.interfaces" else ["ip", "-j", "route", "show", "dev", name]
                    try:
                        entries = json.loads(self._command_read(args))
                    except ValueError as exc:
                        raise AdapterError("管理接口运行状态 JSON 无效。", 502) from exc
                    data["rows"].extend({"source": "kernel/runtime", "interface": name, **entry} for entry in entries)
                    if page_id == "setup.routing":
                        entries = json.loads(self._command_read(["ip", "-j", "-6", "route", "show", "dev", name]))
                        data["rows"].extend({"source": "kernel/runtime IPv6", "interface": name, **entry} for entry in entries)
                data["capability"]["reason"] = "显示管理配置与内核实际地址/路由；DHCP 地址可能仅存在于运行状态。本页只读。"
            elif page_id == "status.alerts":
                health, error = self._health_state()
                data["rows"] = [{"component": name, **row} for name, row in health.items()]
                data["capability"]["reason"] = error or "系统健康摘要；只有监控实际发布的状态才会显示。"
                data["capability"]["read"] = bool(health)
            elif page_id in CONFIG_PAGES:
                tables = CONFIG_PAGES[page_id]
                data["rows"] = self._rows(tables, self.export_config())
                data["capability"]["reason"] = "读取 CONFIG_DB 的 " + ", ".join(tables) + "；配置存在不证明协议运行正常。"
                if not data["rows"]:
                    data["capability"]["reason"] += " 当前无配置；不据此推定平台支持。"
            elif page_id == "status.temperature":
                values, error = self._optional_table("STATE_DB", "TEMPERATURE_INFO")
                data["rows"] = [temperature_row(name, item, simulated=self.demo)
                                for name, item in sorted(values.items(), key=lambda entry: _sort(entry[0]))][:1000]
                data["description"] += "；温度及阈值单位均为摄氏度。ASIC External 是外部传感器，并非芯片内部结温。"
                data["capability"]["reason"] = error or "复用 PMON / STATE_DB；超过 180 秒或时间未知的样本标记过期，无效读数不会显示为 0 或正常。"
                data["capability"]["read"] = bool(data["rows"])
                if not data["rows"] and not error:
                    data["capability"]["reason"] = "PMON 温度状态尚未发布；不能判为硬件缺失。"
            elif page_id in STATE_PAGES:
                states = {}
                errors = []
                for table in STATE_PAGES[page_id]:
                    states[table], error = self._optional_table("STATE_DB", table)
                    if error:
                        errors.append(error)
                data["rows"] = self._rows(STATE_PAGES[page_id], states)
                data["capability"]["reason"] = "PMON/STATE_DB 共享缓存，未直接轮询 IPMI。" + "; ".join(errors)
                if not data["rows"]:
                    data["capability"].update(read=False, reason="状态尚未发布或当前功能不可读；不能判为硬件缺失。" + "; ".join(errors))
            elif page_id == "status.summary":
                overview = self.overview()
                data["summary"] = overview["summary"]
                data["rows"] = [{"property": k, "value": v} for k, v in overview["device"].items()]
                data["rows"].append({"property": "health", "value": overview["health"]})
            elif page_id in ("status.cpu", "status.memory"):
                metrics = self._system_metrics()
                data["rows"] = [{"property": k, "value": v} for k, v in metrics.items()]
                data["metrics"] = metrics
                data["sampled_at"] = metrics["metrics_sampled_at"]
                data["summary"] = [{"label": "CPU 占用 (%)", "value": metrics["cpu_percent"]},
                                   {"label": "内存占用 (%)", "value": metrics["memory_percent"]}]
                data["description"] += "；CPU 后台每 2 秒共享采样，首次短窗口后自动更新，不依赖页面刷新建立采样。idle/iowait 不计为忙碌。"
            elif page_id == "setup.configurations":
                data["rows"] = [{"table": table, "entries": len(rows)} for table, rows in config.items() if isinstance(rows, dict)]
                data["summary"] = [{"label": "配置修订", "value": _revision(config)}, {"label": "运行/启动配置差异", "value": data["pending_save"]}]
            elif page_id == "status.capabilities":
                meta = config.get("DEVICE_METADATA", {}).get("localhost", {})
                data["rows"] = [{"property": k, "value": v} for k, v in meta.items()]
                data["rows"] += [{"property": "面板几何", "value": "未验证；逻辑示意"}, {"property": "动态拆分", "value": "未验证，不开放写入"}]
            elif page_id in ("setup.web", "setup.json-api"):
                data["rows"] = [{"property": "mode", "value": "demo" if self.demo else "live"},
                                {"property": "API", "value": "本服务 /api；会话认证、CSRF、计划确认"},
                                {"property": "cache", "value": "10s shared / bounded commands"}]
            elif page_id == "security.users":
                if self.demo:
                    data["rows"] = [{"name": "demo", "uid": "simulated", "shell": "not a host account"}]
                else:
                    import pwd
                    data["rows"] = [{"name": p.pw_name, "uid": p.pw_uid, "gid": p.pw_gid, "shell": p.pw_shell}
                                    for p in pwd.getpwall() if p.pw_uid == 0 or 1000 <= p.pw_uid < 65534]
            else:
                data["rows"] = self._diagnostic_page(page_id)
        except AdapterError as exc:
            data["capability"] = {"read": False, "write": False, "reason": str(exc)}
        actions = [self._action_form(a, config) for a in PAGE_ACTIONS.get(page_id, ()) if self._action_available(a)]
        data["actions"] = actions
        data["capability"]["write"] = bool(actions)
        if actions:
            previous = data["capability"]["reason"]
            if previous == "本页提供状态查看。":
                previous = "本页提供状态查看与下列配置操作。"
            data["capability"]["reason"] = previous + " 写入须经计划预览/确认；CLI 接受后仍需验证运行状态。"
        elif PAGE_ACTIONS.get(page_id):
            data["capability"]["reason"] += " 写操作未开放：所需 CLI/权限未验证，或该多 ASIC 目标尚未实现命名空间写入。"
        if page_id == "routing.interfaces" and not self._sudo_helper_available():
            data["capability"]["reason"] += " " + self._action_unavailable_reason("interface.address.delete")
        keys = []
        for row in data["rows"][:100]:
            for key in row:
                if key not in keys and key not in ("port_modes", "breakout"):
                    keys.append(key)
        data["columns"] = [{"key": k, "label": k, **({"type": "number"} if k in ("speed", "mtu", "rx_bps", "tx_bps") else {})} for k in keys]
        if page_id == "status.temperature":
            labels = {"name": "传感器", "temperature": "温度", "low_threshold": "低温告警阈值",
                      "high_threshold": "高温告警阈值", "critical_low_threshold": "低温严重阈值",
                      "critical_high_threshold": "高温严重阈值", "status": "状态", "timestamp": "传感器采样时间",
                      "age_seconds": "样本年龄 (秒)", "source": "来源", "sensor_note": "说明"}
            data["columns"] = [{"key": key, "label": label,
                                **({"type": "temperature", "unit": "°C"} if key == "temperature" or key.endswith("threshold") else {})}
                               for key, label in labels.items()]
        if page_id == "status.fans":
            data["control"] = self._fan_status(config)
            data["capability"]["reason"] = data["control"]["reason"]
            for column in data["columns"]:
                if column["key"] in ("speed", "speed_target"):
                    column.update(type="percent", label="实际转速 (%)" if column["key"] == "speed" else "目标转速 (%)")
            if self.demo and data["control"]["mode"] == "fixed":
                for row in data["rows"]:
                    if row.get("table") == "FAN_INFO" and not str(row.get("key", "")).startswith("PSU"):
                        row.update(speed=data["control"]["duty_percent"], speed_target=data["control"]["duty_percent"])
        return data

    def _diagnostic_page(self, page_id):
        commands = {
            "setup.arp": ["ip", "-j", "neigh", "show"], "setup.neighbors": ["ip", "-j", "-6", "neigh", "show"],
            "routing.arp": ["ip", "-j", "neigh", "show"],
            "routing.routes": ["ip", "-j", "route", "show", "table", "all"],
            "setup.docker": ["docker", "ps", "--all", "--format", "{{json .}}"],
            "system.images": ["sonic-installer", "list"],
            "setup.datetime": ["chronyc", "tracking"],
            "status.logs": ["journalctl", "-n", "150", "--no-pager", "-o", "short-iso"],
            "security.login-attempts": ["journalctl", "-u", "ssh", "-n", "100", "--no-pager"],
            "status.maintenance": ["systemctl", "--failed", "--no-pager"],
            "ethernet.mac": ["fdbshow"],
            "ethernet.bgp-ipv4": ["vtysh", "-c", "show bgp ipv4 unicast summary json"],
            "ethernet.bgp-ipv6": ["vtysh", "-c", "show bgp ipv6 unicast summary json"],
            "routing.diagnostics": ["ip", "-j", "address", "show"],
        }
        if page_id not in commands:
            raise AdapterError("此页面尚无经过验证的只读适配器；不返回示例代替实机状态。", 501)
        if self.demo:
            return [{"source": "DEMO", "value": "模拟模式：此诊断不会在宿主执行。"}]
        if page_id == "routing.routes":
            result = []
            for family in ("-4", "-6"):
                text = self._command_read(["ip", "-j", family, "route", "show", "table", "all"])
                try:
                    rows = json.loads(text)
                except ValueError as exc:
                    raise AdapterError("内核路由 JSON 格式无效。", 502) from exc
                result.extend({"family": family[1:], "source": "kernel", **row} for row in rows[:1000])
            return result
        text = self._command_read(commands[page_id])
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [_sanitize(row) for row in parsed[:2000] if isinstance(row, dict)]
            if isinstance(parsed, dict):
                return [{"key": k, "value": _sanitize(v)} for k, v in parsed.items()]
        except ValueError:
            pass
        if page_id == "setup.docker":
            return [_sanitize(json.loads(line)) for line in text.splitlines()[:100] if line.strip()]
        return [{"line": i + 1, "text": re.sub(r"(?i)(password|secret|token|community)(\s*[=:]\s*)\S+", r"\1\2[redacted]", line)} for i, line in enumerate(text.splitlines()[:500])]

    def _action_form(self, action, config):
        if action == "fan.control":
            control = self._fan_status(config)
            return {"id": action, "label": "调节风扇", "description": control["reason"], "fields": [
                {"name": "mode", "label": "调速模式", "type": "select", "required": True,
                 "default": control["mode"], "options": [{"value": m, "label": "自动" if m == "auto" else "固定占空比"} for m in control["available_modes"]]},
                {"name": "duty_percent", "label": "占空比 (%)", "type": "number", "required": False,
                 "min": control["min_percent"], "max": control["max_percent"], "step": control["step_percent"],
                 "default": control["duty_percent"], "help": control["protection"]["description"]}]}
        def field(name, label, kind="text", required=True, options=None, help_text=None):
            row = {"name": name, "label": label, "type": kind, "required": required}
            if options is not None:
                row["options"] = [dict(v) if isinstance(v, dict) else {"label": str(v), "value": str(v)} for v in options]
            if help_text:
                row["help"] = help_text
            return row
        profile = self._profile_for(config)
        physical = []
        for name, row in sorted(config.get("PORT", {}).items(), key=lambda item: _sort(item[0])):
            cage = profile["cages"].get(str(row.get("index")), {}).get("cage_id") if profile else None
            label = " / ".join(dict.fromkeys(str(v) for v in (name, row.get("alias"), cage) if v))
            physical.append({"label": label, "value": name})
        lag_names = sorted(config.get("PORTCHANNEL", {}), key=_sort)
        vlan_names = sorted(config.get("VLAN", {}), key=_sort)
        bridge_members = physical + [{"label": n, "value": n} for n in lag_names]
        ip_interfaces = bridge_members + [{"label": n, "value": n} for n in vlan_names]
        loopbacks = sorted({k.split("|")[0] for k in config.get("LOOPBACK_INTERFACE", {})}, key=_sort)
        ip_interfaces += [{"label": n, "value": n} for n in loopbacks]
        vlans = [{"label": n, "value": n[4:]} for n in vlan_names if re.fullmatch(r"Vlan\d+", n)]
        neighbors = [{"label": f"{n} / AS {r.get('asn', '?')}", "value": n} for n, r in sorted(config.get("BGP_NEIGHBOR", {}).items())]
        forms = {
            "vlan.create": ("创建 VLAN", [field("vlan_id", "VLAN ID (1–4094)", "number")]),
            "vlan.delete": ("删除 VLAN", [field("vlan_id", "VLAN", "select", options=vlans)]),
            "vlan.member.add": ("添加 VLAN 成员", [field("vlan_id", "VLAN", "select", options=vlans), field("interface", "接口", "select", options=bridge_members), field("tagging_mode", "标记模式", "select", options=["tagged", "untagged"])]),
            "vlan.member.delete": ("删除 VLAN 成员", [field("vlan_id", "VLAN", "select", options=vlans), field("interface", "接口", "select", options=bridge_members)]),
            "lag.create": ("创建 LAG", [field("name", "PortChannel 名称")]),
            "lag.delete": ("删除 LAG", [field("name", "PortChannel", "select", options=lag_names)]),
            "lag.member.add": ("添加 LAG 成员", [field("name", "PortChannel", "select", options=lag_names), field("interface", "物理接口", "select", options=physical)]),
            "lag.member.delete": ("删除 LAG 成员", [field("name", "PortChannel", "select", options=lag_names), field("interface", "物理接口", "select", options=physical)]),
            "interface.address.add": ("添加接口 IP", [field("interface", "接口", "select", options=ip_interfaces), field("prefix", "地址/前缀长度")]),
            "interface.address.delete": ("删除接口 IP", [field("interface", "接口", "select", options=ip_interfaces), field("prefix", "地址/前缀长度")]),
            "route.add": ("添加静态路由", [field("prefix", "目的网络/前缀长度"), field("nexthop", "下一跳 IP")]),
            "route.delete": ("删除静态路由", [field("prefix", "目的网络/前缀长度"), field("nexthop", "下一跳 IP")]),
            "system.hostname": ("修改主机名", [field("hostname", "主机名")]),
            "ntp.add": ("添加 NTP 源", [field("server", "服务器 IP/域名")]),
            "ntp.delete": ("删除 NTP 源", [field("server", "服务器 IP/域名")]),
            "config.save": ("保存配置", []),
            "dns.add": ("添加 DNS 服务器", [field("server", "DNS 服务器 IP")]),
            "dns.delete": ("删除 DNS 服务器", [field("server", "DNS 服务器 IP")]),
            "bgp.neighbor.startup": ("启用已有 BGP 邻居", [field("neighbor", "已有邻居 IP", "select", options=neighbors)]),
            "bgp.neighbor.shutdown": ("关闭已有 BGP 邻居", [field("neighbor", "已有邻居 IP", "select", options=neighbors)]),
            "pfc.priority": ("设置端口 PFC 优先级", [field("interface", "物理接口", "select", options=physical), field("priority", "优先级 0–7", "select", options=list(range(8))), field("status", "状态", "select", options=["on", "off"])]),
            "mirror.span.add": ("创建本地 SPAN 会话", [field("name", "会话名称"), field("source", "源物理接口", "select", options=physical), field("destination", "目的物理接口", "select", options=physical), field("direction", "方向", "select", options=["rx", "tx", "both"])]),
            "mirror.delete": ("删除镜像会话", [field("name", "会话名称", "select", options=sorted(config.get("MIRROR_SESSION", {})))]),
        }
        if action == "port.configure":
            fields = [field("interfaces", "接口", "multiselect", options=physical)]
            available = self._port_fields()
            state, _ = self._optional_table("STATE_DB", "PORT_TABLE")
            fec_options = sorted({mode for name, row in config.get("PORT", {}).items()
                                  for mode in self._supported_fecs(row, state.get(name, {}))})
            for key, label, kind, opts in (("admin_status", "管理状态", "select", ["up", "down"]),
                ("mtu", "MTU", "number", None), ("fec", "FEC", "select", fec_options),
                ("speed", "速率 Mbps", "number", None), ("description", "描述", "text", None)):
                if key in available:
                    fields.append(field(key, label, kind, False, opts,
                                        "仅在平台提供 supported_speeds 时允许改速；否则仅接受现速率。" if key == "speed" else None))
            title = "修改端口配置"
        elif action == "port.breakout":
            entries = self._breakout_catalog(config)
            parents = [{"label": parent + " / " + str(port_breakout.GROUPS.get(parent, {}).get("alias", parent)), "value": parent}
                       for parent, entry in entries.items() if any(m["available"] for m in entry["modes"])]
            modes = sorted({m["breakout_mode"] for entry in entries.values() for m in entry["modes"] if m["available"]})
            title = "拆分/合并端口模式"
            fields = [field("interface", "物理接口组", "select", options=parents), field("mode", "目标模式", "select", options=modes)]
        else:
            title, fields = forms[action]
        return {"id": action, "label": title, "description": "先生成计划；确认后顺序执行 SONiC CLI，不保证原子回滚。", "dangerous": action != "config.save", "fields": fields}

    @staticmethod
    def _supported_fecs(configured, state):
        raw = state.get("supported_fecs")
        if raw is None or str(raw).strip().lower() in ("", "n/a", "unknown"):
            raw = configured.get("fec", "")
        values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
        return {str(v).lower().replace("sai_port_fec_mode_", "") for v in values
                if str(v).lower().replace("sai_port_fec_mode_", "") in ("rs", "fc", "none")}

    @staticmethod
    def _string(value, label, pattern=None, max_length=255):
        if not isinstance(value, str) or not value or len(value) > max_length or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AdapterError(f"{label} 格式无效。")
        if pattern and not re.fullmatch(pattern, value):
            raise AdapterError(f"{label} 格式无效。")
        return value

    @staticmethod
    def _integer(value, low, high, label):
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)) or not low <= int(value) <= high:
            raise AdapterError(f"{label} 必须是 {low}–{high} 的整数。")
        return int(value)

    @classmethod
    def _ip_text(cls, value, label):
        value = cls._string(value, label)
        if "%" in value or any(c.isspace() for c in value):
            raise AdapterError(f"{label} 不接受 IPv6 scope/zone 或空白字符。")
        return value

    @staticmethod
    def _interface_table(name):
        if name.startswith("Ethernet"):
            return "INTERFACE"
        if name.startswith("PortChannel"):
            return "PORTCHANNEL_INTERFACE"
        if name.startswith("Vlan"):
            return "VLAN_INTERFACE"
        if name.startswith("Loopback"):
            return "LOOPBACK_INTERFACE"
        raise AdapterError("只允许前面板、VLAN、PortChannel 或已有 Loopback；不修改管理接口。")

    def _interface(self, name, config, physical=False):
        name = self._string(name, "接口", r"(?:Ethernet\d+|PortChannel\d+|Vlan\d+|Loopback\d+)", 64)
        pools = ("PORT",) if physical else ("PORT", "PORTCHANNEL", "VLAN", "LOOPBACK_INTERFACE")
        if not any(name in config.get(table, {}) or any(k.split("|")[0] == name for k in config.get(table, {})) for table in pools):
            raise AdapterError(f"接口不存在：{name}", 404)
        return name

    @staticmethod
    def _refs(config, name, ignore=()):
        def mentions(value):
            if isinstance(value, dict):
                return any(mentions(k) or mentions(v) for k, v in value.items())
            if isinstance(value, (list, tuple, set)):
                return any(mentions(v) for v in value)
            return name in re.split(r"[,|\s]+", str(value))
        refs = []
        for table, entries in config.items():
            if table in ignore or not isinstance(entries, dict):
                continue
            for key, row in entries.items():
                if mentions(key) or mentions(row):
                    refs.append(f"{table}:{key}")
        return refs

    def _compile(self, action, values, config):
        candidate = copy.deepcopy(config)
        commands, changes, warnings = [], [], ["配置命令逐条执行，不是原子事务；执行失败可能留下部分变更。", "CLI 成功不代表链路、协议或硬件已完成收敛，需刷新状态确认。"]
        fields = {f["name"] for f in self._action_form(action, config)["fields"]}
        if set(values) - fields:
            raise AdapterError("包含未知或未开放字段：" + ", ".join(sorted(set(values) - fields)))

        def command(path, *args):
            commands.append(["config", *path, *map(str, args)])

        def change(table, key, after):
            before = candidate.get(table, {}).get(key)
            if before == after:
                return False
            changes.append({"target": f"{table}|{key}", "before": _sanitize(before), "after": _sanitize(after)})
            if after is None:
                candidate.setdefault(table, {}).pop(key, None)
            else:
                candidate.setdefault(table, {})[key] = after
            return True

        path = ACTION_PATHS.get(action)
        if action == "port.configure":
            selected = values.get("interfaces")
            if not isinstance(selected, list) or not 1 <= len(selected) <= 64 or len(set(map(str, selected))) != len(selected):
                raise AdapterError("请选择 1–64 个不重复接口。")
            settings = {k: v for k, v in values.items() if k != "interfaces" and v is not None and (v != "" or k == "description")}
            if not settings:
                raise AdapterError("没有端口变更字段。")
            for name in selected:
                name = self._interface(name, config, physical=True)
                before = config["PORT"][name]
                after = copy.deepcopy(before)
                for key, value in settings.items():
                    if key == "admin_status":
                        if value not in ("up", "down"):
                            raise AdapterError("管理状态必须是 up 或 down。")
                        cli = ("interface", "startup" if value == "up" else "shutdown")
                    elif key == "mtu":
                        value = str(self._integer(value, 576, 9216, "MTU"))
                        cli = ("interface", "mtu")
                    elif key == "fec":
                        if value not in ("rs", "fc", "none"):
                            raise AdapterError("FEC 必须是 rs、fc 或 none。")
                        state, _ = self._optional_table("STATE_DB", "PORT_TABLE")
                        if value not in self._supported_fecs(before, state.get(name, {})):
                            raise AdapterError(f"{name} 未声明支持 FEC {value}；能力未知时只允许当前值。")
                        cli = ("interface", "fec")
                    elif key == "speed":
                        value = str(self._integer(value, 10, 800000, "速率 Mbps"))
                        supported = re.split(r"[, ]+", str(before.get("supported_speeds", before.get("speed", ""))))
                        state, _ = self._optional_table("STATE_DB", "PORT_TABLE")
                        if state.get(name, {}).get("supported_speeds"):
                            supported = re.split(r"[, ]+", str(state[name]["supported_speeds"]))
                        if value not in supported:
                            raise AdapterError(f"{name} 未声明支持 {value} Mbps；不从连接器外观推断可用速率。")
                        cli = ("interface", "speed")
                    elif key == "description":
                        if not isinstance(value, str) or len(value) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in value) or value.startswith("-"):
                            raise AdapterError("描述最多 255 字符，不允许控制字符或选项前缀。")
                        cli = ("interface", "description")
                    else:
                        raise AdapterError("未开放端口字段。")
                    if str(before.get(key, "")) != str(value):
                        command(cli, name, *([] if key == "admin_status" else [value]))
                        after[key] = str(value)
                change("PORT", name, after)
            warnings.append("管理链路/业务可能中断；默认不自动保存到启动配置。")
        elif action.startswith("vlan."):
            vid = self._integer(values.get("vlan_id"), 1, 4094, "VLAN ID")
            vlan = f"Vlan{vid}"
            exists = vlan in config.get("VLAN", {})
            if action == "vlan.create":
                if exists:
                    raise AdapterError("VLAN 已存在。", 409)
                command(path, vid)
                change("VLAN", vlan, {"vlanid": str(vid)})
            else:
                if not exists:
                    raise AdapterError("VLAN 不存在。", 404)
                if action == "vlan.delete":
                    refs = self._refs(config, vlan, ("VLAN",))
                    if refs:
                        raise AdapterError("VLAN 仍被引用：" + ", ".join(refs[:10]), 409)
                    command(path, vid)
                    change("VLAN", vlan, None)
                else:
                    interface = self._interface(values.get("interface"), config)
                    if not (interface in config.get("PORT", {}) or interface in config.get("PORTCHANNEL", {})):
                        raise AdapterError("VLAN 成员必须是物理口或 LAG。")
                    key = f"{vlan}|{interface}"
                    member = config.get("VLAN_MEMBER", {}).get(key)
                    if action.endswith(".add"):
                        mode = values.get("tagging_mode")
                        if mode not in ("tagged", "untagged"):
                            raise AdapterError("tagging_mode 必须是 tagged 或 untagged。")
                        if member is not None:
                            raise AdapterError("VLAN 成员已存在。", 409)
                        table = self._interface_table(interface)
                        if any(k.startswith(interface + "|") for k in config.get(table, {})):
                            raise AdapterError("该接口已有三层地址，不能直接改成 VLAN 成员。", 409)
                        if any(k.endswith("|" + interface) for k in config.get("PORTCHANNEL_MEMBER", {})):
                            raise AdapterError("请操作所属 PortChannel，而不是它的物理成员。", 409)
                        if mode == "untagged" and any(k.endswith("|" + interface) and v.get("tagging_mode") == "untagged" for k, v in config.get("VLAN_MEMBER", {}).items()):
                            raise AdapterError("接口已有 untagged VLAN。", 409)
                        command(path, *(["--untagged"] if mode == "untagged" else []), vid, interface)
                        change("VLAN_MEMBER", key, {"tagging_mode": mode})
                    else:
                        if member is None:
                            raise AdapterError("VLAN 成员不存在。", 404)
                        command(path, vid, interface)
                        change("VLAN_MEMBER", key, None)
        elif action.startswith("lag."):
            name = self._string(values.get("name"), "LAG 名称", r"PortChannel\d+", 64)
            number = self._integer(name[len("PortChannel"):], 1, 9999, "LAG 编号")
            exists = name in config.get("PORTCHANNEL", {})
            if action == "lag.create":
                if exists:
                    raise AdapterError("LAG 已存在。", 409)
                command(path, name)
                change("PORTCHANNEL", name, {"admin_status": "up", "mtu": "9100", "lacp_key": "auto", "fast_rate": "false", "min_links": "1"})
            else:
                if not exists:
                    raise AdapterError("LAG 不存在。", 404)
                if action == "lag.delete":
                    refs = self._refs(config, name, ("PORTCHANNEL",))
                    if refs:
                        raise AdapterError("LAG 仍被引用：" + ", ".join(refs[:10]), 409)
                    command(path, name)
                    change("PORTCHANNEL", name, None)
                else:
                    interface = self._interface(values.get("interface"), config, True)
                    key = name + "|" + interface
                    if action.endswith(".add"):
                        refs = self._refs(config, interface, ("PORT", "PORT_QOS_MAP", "BUFFER_PG", "BUFFER_QUEUE", "QUEUE", "PFC_WD"))
                        if refs:
                            raise AdapterError("接口存在 L3/VLAN/LAG/ACL 等依赖：" + ", ".join(refs[:10]), 409)
                        command(path, name, interface)
                        change("PORTCHANNEL_MEMBER", key, {})
                    else:
                        if key not in config.get("PORTCHANNEL_MEMBER", {}):
                            raise AdapterError("LAG 成员不存在。", 404)
                        command(path, name, interface)
                        change("PORTCHANNEL_MEMBER", key, None)
        elif action.startswith("interface.address."):
            name = self._interface(values.get("interface"), config)
            table = self._interface_table(name)
            try:
                address = str(ipaddress.ip_interface(self._ip_text(values.get("prefix"), "IP 前缀")))
            except ValueError as exc:
                raise AdapterError("IP 地址/前缀长度无效。") from exc
            key = name + "|" + address
            exists = key in config.get(table, {})
            if action.endswith(".add"):
                if exists:
                    raise AdapterError("地址已存在。", 409)
                if any(k.endswith("|" + name) for t in ("VLAN_MEMBER", "PORTCHANNEL_MEMBER") for k in config.get(t, {})):
                    raise AdapterError("二层成员接口不能直接配置三层地址。", 409)
                change(table, key, {})
            else:
                if not exists:
                    raise AdapterError("地址不存在。", 404)
                change(table, key, None)
            command(path, name, address)
        elif action.startswith("route."):
            try:
                prefix = str(ipaddress.ip_network(self._ip_text(values.get("prefix"), "目的网络"), strict=True))
                nexthop = str(ipaddress.ip_address(self._ip_text(values.get("nexthop"), "下一跳")))
            except ValueError as exc:
                raise AdapterError("网络前缀或下一跳无效。") from exc
            if ipaddress.ip_network(prefix).version != ipaddress.ip_address(nexthop).version:
                raise AdapterError("目的网络与下一跳地址族不一致。")
            existing = config.get("STATIC_ROUTE", {}).get(prefix)
            if action.endswith(".add"):
                if existing is not None:
                    raise AdapterError("静态路由已存在；此版本不自动覆盖 ECMP/VRF 路由。", 409)
                change("STATIC_ROUTE", prefix, {"nexthop": nexthop})
            else:
                if existing is None or str(existing.get("nexthop")) != nexthop or any(existing.get(k) for k in ("ifname", "nexthop-vrf")):
                    raise AdapterError("仅可删除匹配下一跳的默认 VRF 单下一跳静态路由。", 409)
                change("STATIC_ROUTE", prefix, None)
            command(path, "prefix", prefix, "nexthop", nexthop)
        elif action == "system.hostname":
            hostname = self._string(values.get("hostname"), "主机名", r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", 63)
            meta = copy.deepcopy(config.get("DEVICE_METADATA", {}).get("localhost", {}))
            meta["hostname"] = hostname
            if change("DEVICE_METADATA", "localhost", meta):
                command(path, hostname)
        elif action in ("ntp.add", "ntp.delete"):
            server = self._ip_text(values.get("server"), "NTP 服务器")
            try:
                server = str(ipaddress.ip_address(server))
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", server) or any(not p or len(p) > 63 or p.startswith("-") or p.endswith("-") for p in server.split(".")):
                    raise AdapterError("NTP 服务器必须是 IP 或合法域名。")
            exists = server in config.get("NTP_SERVER", {})
            if action.endswith(".add") and exists:
                raise AdapterError("NTP 服务器已存在。", 409)
            if action.endswith(".delete") and not exists:
                raise AdapterError("NTP 服务器不存在。", 404)
            change("NTP_SERVER", server, {} if action.endswith(".add") else None)
            command(path, server)
        elif action in ("dns.add", "dns.delete"):
            try:
                server = str(ipaddress.ip_address(self._ip_text(values.get("server"), "DNS IP")))
            except ValueError as exc:
                raise AdapterError("DNS 服务器必须是 IP 地址。") from exc
            existing = config.get("DNS_NAMESERVER", {})
            if action.endswith(".add"):
                if server in existing:
                    raise AdapterError("DNS 服务器已存在。", 409)
                if len(existing) >= 3:
                    raise AdapterError("当前适配器最多允许 3 个静态 DNS 服务器。", 409)
                change("DNS_NAMESERVER", server, {})
            else:
                if server not in existing:
                    raise AdapterError("DNS 服务器不存在。", 404)
                change("DNS_NAMESERVER", server, None)
            command(path, server)
        elif action.startswith("bgp.neighbor."):
            try:
                neighbor = str(ipaddress.ip_address(self._ip_text(values.get("neighbor"), "邻居 IP")))
            except ValueError as exc:
                raise AdapterError("仅允许指定已有邻居的精确 IP，不支持 all/hostname 广播操作。") from exc
            before = config.get("BGP_NEIGHBOR", {}).get(neighbor)
            if before is None:
                raise AdapterError("默认 VRF 的 BGP_NEIGHBOR 中不存在该邻居。", 404)
            status = "up" if action.endswith("startup") else "down"
            after = {**before, "admin_status": status}
            if not change("BGP_NEIGHBOR", neighbor, after):
                raise AdapterError("邻居管理状态已符合目标。", 409)
            command(path, neighbor)
            warnings.append("BGP 会话及由它学习的路由可能撤销或重建；不自动新增邻居或更改路由策略。")
        elif action == "pfc.priority":
            interface = self._interface(values.get("interface"), config, True)
            priority = str(self._integer(values.get("priority"), 0, 7, "优先级"))
            status = values.get("status")
            if status not in ("on", "off"):
                raise AdapterError("PFC 状态必须是 on 或 off。")
            before = config.get("PORT_QOS_MAP", {}).get(interface)
            if before is None:
                raise AdapterError("该接口没有 PORT_QOS_MAP；不能让 CLI 空操作冒充成功。", 409)
            priorities = {p.strip() for p in str(before.get("pfc_enable", "")).split(",") if p.strip()}
            if (priority in priorities) == (status == "on"):
                raise AdapterError("该 PFC 优先级已符合目标。", 409)
            priorities.add(priority) if status == "on" else priorities.remove(priority)
            change("PORT_QOS_MAP", interface, {**before, "pfc_enable": ",".join(sorted(priorities))})
            command(path, interface, priority, status)
            warnings.append("PFC 需与对端及缓冲区/QoS 配置一致；此操作只改优先级使能，不承诺无损转发。")
        elif action.startswith("mirror."):
            name = self._string(values.get("name"), "会话名称", r"[A-Za-z][A-Za-z0-9_-]{0,31}", 32)
            existing = config.get("MIRROR_SESSION", {}).get(name)
            if action == "mirror.delete":
                if existing is None:
                    raise AdapterError("镜像会话不存在。", 404)
                refs = self._refs(config, name, ("MIRROR_SESSION",))
                if refs:
                    raise AdapterError("镜像会话仍被引用：" + ", ".join(refs[:10]), 409)
                change("MIRROR_SESSION", name, None)
                command(path, name)
            else:
                if existing is not None:
                    raise AdapterError("会话名称已存在。", 409)
                source = self._interface(values.get("source"), config, True)
                destination = self._interface(values.get("destination"), config, True)
                direction = values.get("direction")
                if source == destination or direction not in ("rx", "tx", "both"):
                    raise AdapterError("源与目的口必须不同，方向必须是 rx/tx/both。")
                refs = self._refs(config, destination, ("PORT", "PORT_QOS_MAP", "BUFFER_PG", "BUFFER_QUEUE", "QUEUE", "PFC_WD"))
                if refs:
                    raise AdapterError("目的口仍有业务依赖，拒绝用于 SPAN：" + ", ".join(refs[:10]), 409)
                change("MIRROR_SESSION", name, {"type": "SPAN", "dst_port": destination, "src_port": source, "direction": direction.upper()})
                command(path, name, destination, source, direction)
                warnings.append("本地 SPAN 复制业务流量到目的口；应将目的口用于受控监测设备。")
        elif action == "config.save":
            command(path, "-y")
            changes.append({"target": "startup configuration", "before": "previous saved configuration", "after": "current running CONFIG_DB"})
            warnings = ["保存整个当前 CONFIG_DB，不仅保存本服务的变更。"]
        else:
            raise AdapterError("操作未实现。", 501)
        if not commands:
            raise AdapterError("配置已经符合目标，没有需要执行的变更。", 409)
        if len(commands) > self.MAX_COMMANDS:
            raise AdapterError("计划超过 64 条命令，请分批操作。", 413)
        return commands, changes, warnings, candidate

    def plan(self, action, values, revision=None):
        if not isinstance(action, str) or action not in {*ACTION_PATHS, "port.configure", "port.breakout", "fan.control"}:
            raise AdapterError("未开放的操作。", 400)
        if not isinstance(values, dict):
            raise AdapterError("values 必须是对象。")
        if not self._action_available(action):
            raise AdapterError(self._action_unavailable_reason(action), 501)
        config = self._config(fresh=True)
        current_revision = _revision(config)
        if revision is not None and revision != current_revision:
            raise AdapterError("配置已变化，请刷新页面并重新生成计划。", 409)
        breakout = None
        fan_values, fan_before = None, None
        if action == "fan.control":
            if not self.demo or self._fan_control is None:
                raise AdapterError(self._fan_status(config)["reason"], 501)
            try:
                fan_values = self._fan_control.validate(values)
            except ValueError as error:
                raise AdapterError(str(error)) from error
            current_fan = self._fan_status(config)
            fan_before = {key: current_fan[key] for key in ("mode", "duty_percent")}
            if fan_values == fan_before:
                raise AdapterError("风扇模式已符合目标。", 409)
            commands, candidate = [], config
            changes = [{"target": "fan control (DEMO)", "before": fan_before, "after": fan_values}]
            warnings = ["DEMO：只模拟风扇设置，不执行硬件调速；演示范围不适用于实机。"]
        elif action == "port.breakout":
            breakout, candidate, changes, warnings = self._compile_breakout(values, config)
            commands = []
        else:
            commands, changes, warnings, candidate = self._compile(action, values, config)
        now = time.time()
        public = {"id": secrets.token_urlsafe(24), "action": action, "title": self._action_form(action, config)["label"],
                  "summary": f"{'DEMO 模拟' if self.demo else '执行'} {len(commands)} 条受控命令，{len(changes)} 项配置差异。",
                  "changes": changes, "warnings": warnings, "requires_confirmation": True,
                  "revision": current_revision, "expires_at": _utc(now + self.PLAN_SECONDS)}
        if breakout:
            public.update({"summary": f"将 {breakout['summary']['parent']} 切换为 {breakout['summary']['after']}；自动备份，重启后检查生效状态。",
                           "reboot_required": True, "method": "static-reboot", "static_diff": breakout.get("static_diff", {}),
                           "breakout_summary": breakout["summary"]})
        with self._plans_lock:
            self._plans = {k: p for k, p in self._plans.items() if p["expires"] > now}
            if len(self._plans) >= 128:
                raise AdapterError("待确认计划过多，请稍后再试。", 429)
            self._plans[public["id"]] = {"public": public, "commands": commands, "candidate": candidate,
                                           "expires": now + self.PLAN_SECONDS, "revision": current_revision, "action": action, "breakout": breakout,
                                           "fan_values": fan_values, "fan_before": fan_before}
        return copy.deepcopy(public)

    def _compile_breakout(self, values, config):
        if not isinstance(values, dict) or set(values) != {"interface", "mode"}:
            raise AdapterError("拆分参数只能包含 interface 和 mode。")
        parent = self._string(values["interface"], "接口", r"Ethernet\d+")
        mode = self._string(values["mode"], "模式", r"(?:1x100G|4x25G)")
        entry = self._breakout_catalog(config).get(parent, {})
        if not any(m.get("available") and m["breakout_mode"] == mode for m in entry.get("modes", [])):
            raise AdapterError("该接口当前没有经过验证且可执行的目标模式。", 409)
        refs = port_breakout.dependencies(config, parent)
        if refs:
            raise AdapterError("端口存在依赖，需先移除：" + ", ".join(refs[:20]), 409)
        if not self.demo:
            try:
                plan = port_breakout.prepare(values, config)
            except (port_breakout.BreakoutError, OSError, ValueError, KeyError) as exc:
                raise AdapterError("静态拆分计划验证失败：" + str(exc), 409) from exc
        else:
            group = port_breakout.GROUPS[parent]
            candidate = copy.deepcopy(config)
            names = ["Ethernet" + str(group["number"] + i) for i in range(4)]
            base = copy.deepcopy(config["PORT"][parent])
            for name in names:
                candidate["PORT"].pop(name, None)
            count = 4 if mode == "4x25G" else 1
            for i in range(count):
                row = {**base, "speed": "25000" if count == 4 else "100000", "lanes": str(group["lanes"][i]) if count == 4 else ",".join(map(str, group["lanes"])),
                       "alias": group["alias"] + ("/" + str(i + 1) if count == 4 else ""), "fec": "rs"}
                if count == 4:
                    row["subport"] = str(i + 1)
                else:
                    row.pop("subport", None)
                candidate["PORT"][names[i]] = row
            candidate.setdefault("BREAKOUT_CFG", {})[parent] = {"brkout_mode": mode}
            changes = [{"target": "PORT|" + n, "before": config["PORT"].get(n), "after": candidate["PORT"].get(n)} for n in names]
            changes.append({"target": "BREAKOUT_CFG|" + parent, "before": config.get("BREAKOUT_CFG", {}).get(parent), "after": {"brkout_mode": mode}})
            plan = {"config_after": candidate, "changes": changes, "summary": {"parent": parent, "after": mode, "ports_after": len(candidate["PORT"]), "requires_reboot": True}, "static_diff": {}}
        warnings = ["原接口 IP 保留在第一个子口，新增子口不复制 IP；合并前需移除其他子口上的业务配置。"]
        return plan, plan["config_after"], _sanitize(plan["changes"]), warnings

    def _launch_breakout(self, plan):
        base = self.data_dir / "breakout"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink() or base.resolve() != base.absolute():
            raise AdapterError("拆分任务目录不是可信普通目录。", 500)
        descriptor = None
        try:
            if os.name == "posix":
                import fcntl
                descriptor = os.open(base / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise AdapterError("应用安装或另一个拆分操作正在进行，请稍后重试。", 409) from exc
            operation = self._breakout_status()
            if operation and operation["blocking"]:
                raise AdapterError("现有拆分任务尚未结束，拒绝重复提交。", 409)
            return self._launch_breakout_locked(plan)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _launch_breakout_locked(self, plan):
        # All executable paths and options are server-owned constants. Only the
        # fresh random operation ID crosses the worker interface.
        base = self.data_dir / "breakout"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink() or base.resolve() != base.absolute():
            raise AdapterError("拆分任务目录不是可信普通目录。", 500)
        os.chmod(base, 0o700)
        identifier = secrets.token_hex(16)
        directory = base / identifier
        directory.mkdir(mode=0o700)
        port_breakout.atomic_write(directory / "plan.json", port_breakout.encoded(plan))
        queued = {"id": identifier, "status": "queued", "message": "已提交拆分任务；等待备份、整机重启及启动后验证。", "verified": False}
        port_breakout.atomic_write(directory / "status.json", port_breakout.encoded(queued))
        port_breakout.atomic_write(base / "current.json", port_breakout.encoded({"id": identifier}))
        try:
            output = self._run(["/usr/bin/systemd-run", "--unit", "sonic-web-breakout", "--collect", "/usr/bin/python3",
                                "/host/sonic-web/current/deploy/breakout_worker.py", "--operation", identifier], timeout=15)
        except (AdapterError, OSError) as exc:
            # A timeout can be ambiguous: preserve queued status and block writes
            # until the root worker/status is checked, never report completed.
            return {"success": False, "changed": False, "message": "后台任务启动未确认；请查看持久任务状态，避免重复提交。",
                    "pending_save": None, "operation": self._breakout_status(), "details": {"operation_id": identifier, "failure": str(exc), "hardware_verified": False}}
        return {"success": True, "changed": False, "message": "拆分任务已接受，尚未完成；整机将重启，请等待恢复后查看持久任务验收结果。",
                "pending_save": None, "operation": self._breakout_status(), "details": {"accepted": True, "completed": False, "reboot_required": True, "operation_id": identifier, "hardware_verified": False, "worker": output[:1000]}}

    def _backup_config(self, current):
        try:
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(backup_dir, 0o700)
            backup = backup_dir / (_utc().replace(":", "-") + "-" + secrets.token_hex(4) + ".json")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(current, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            return backup
        except OSError as exc:
            raise AdapterError("配置备份失败；拒绝开始写操作。", 500) from exc

    def apply(self, plan_id):
        if not isinstance(plan_id, str):
            raise AdapterError("计划 ID 无效。")
        if not self._write_lock.acquire(blocking=False):
            raise AdapterError("另一个配置操作正在执行。", 409)
        try:
            with self._plans_lock:
                plan = self._plans.pop(plan_id, None)
            if not plan:
                raise AdapterError("计划不存在或已使用。", 404)
            if plan["expires"] <= time.time():
                raise AdapterError("计划已过期，请重新生成。", 409)
            current = self._config(fresh=True)
            if _revision(current) != plan["revision"]:
                raise AdapterError("配置已在计划生成后改变，拒绝执行。", 409)
            if not self._action_available(plan["action"]):
                raise AdapterError("执行能力或 ASIC 命名空间已变化，请重新生成计划。", 409)
            if plan["action"] == "fan.control":
                if not self.demo or self._fan_control is None:
                    raise AdapterError("当前平台未开放风扇调速。", 501)
                state = self._fan_status(current)
                if {key: state[key] for key in ("mode", "duty_percent")} != plan["fan_before"]:
                    raise AdapterError("风扇设置已改变，请重新生成计划。", 409)
                control = self._fan_control.apply(plan["fan_values"])
                pending, reason = self._save_status(current)
                return {"success": True, "message": "DEMO：已模拟风扇设置，未调节任何硬件。", "changed": True,
                        "pending_save": pending, "pending_save_reason": reason, "details": {"simulated": True, "control": control}}
            if self.demo:
                self._demo_config = copy.deepcopy(plan["candidate"])
                if plan["action"] == "config.save":
                    self._demo_saved = copy.deepcopy(self._demo_config)
                self._invalidate()
                self._pending_save, save_reason = self._save_status()
                return {"success": True, "message": "DEMO：已在内存中模拟，未执行宿主命令。", "changed": True,
                        "pending_save": self._pending_save, "pending_save_reason": save_reason,
                        "details": {"simulated": True, "commands_count": len(plan["commands"])}}
            if plan.get("breakout"):
                return self._launch_breakout(plan["breakout"])
            deadline = time.monotonic() + self.APPLY_SECONDS
            backup = self._backup_config(current)
            completed, failure, uncertain = [], None, False
            expected_revision = _revision(current)
            for argv in plan["commands"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "达到整体执行时限；后续命令未执行。"
                    break
                # Catch external CLI changes between sequential commands. The adapter
                # serializes itself; SONiC does not provide a global transaction lock.
                try:
                    if _revision(self._config(fresh=True)) != expected_revision:
                        failure = "执行期间发现外部配置变化，剩余命令已停止。"
                        break
                    text = self._run(argv, timeout=min(20, remaining))
                    completed.append({"command": argv, "output": text[:2000]})
                    expected_revision = _revision(self._config(fresh=True))
                except (AdapterError, OSError) as exc:
                    failure, uncertain = str(exc), True
                    break
            self._invalidate()
            self._pending_save, save_reason = self._save_status()
            details = {"completed": completed, "backup": backup.name, "partial_or_uncertain": bool(failure),
                       "hardware_verified": False, "auto_rollback": False}
            try:
                observed = self._config(fresh=True)
                details["revision"] = _revision(observed)
                if not failure and plan["action"] == "config.save":
                    try:
                        saved = self._startup_config()  # Bypass cache for save confirmation.
                        details["startup_revision"] = _revision(saved)
                        self._pending_save = _revision(observed) != _revision(saved)
                        if self._pending_save:
                            failure = "CLI 返回成功，但启动配置与运行配置仍不一致，保存未确认。"
                    except AdapterError as exc:
                        self._pending_save = None
                        save_reason = str(exc)
                        failure = "CLI 返回成功，但启动配置无法读回，保存结果未知。"
                    details["partial_or_uncertain"] = bool(failure)
                if not failure and plan["action"] != "config.save":
                    mismatches = []
                    for expected in plan["public"]["changes"]:
                        table, key = expected["target"].split("|", 1)
                        desired = plan["candidate"].get(table, {}).get(key)
                        actual = observed.get(table, {}).get(key)
                        matches = actual is None if desired is None else isinstance(actual, dict) and all(actual.get(k) == v for k, v in desired.items())
                        if not matches:
                            mismatches.append(expected["target"])
                    if mismatches:
                        failure = "CLI 返回成功，但预期配置未读回：" + ", ".join(mismatches[:10])
                        details["partial_or_uncertain"] = True
            except AdapterError:
                details["readback"] = "配置读回失败；请检查实际状态。"
                if not failure:
                    failure = "CLI 已完成，但配置读回失败，不能确认写入结果。"
            if failure:
                details["failure"] = failure
                return {"success": False, "message": "执行未全部成功；请检查已完成命令和实际配置，备份已保留。", "changed": bool(completed) or uncertain,
                        "pending_save": self._pending_save, "pending_save_reason": save_reason, "details": details}
            return {"success": True, "message": "SONiC CLI 已完成；请刷新并验证实际链路/协议状态。", "changed": True,
                    "pending_save": self._pending_save, "pending_save_reason": save_reason, "details": details}
        finally:
            self._write_lock.release()

    @staticmethod
    def _seed_demo():
        # Explicit simulation only. Logical cage order is not physical geometry.
        config = {"DEVICE_METADATA": {"localhost": {"hostname": "sonic-demo", "platform": "x86_64-cel_questone_2-r0", "hwsku": "Questone_2", "model": "DEMO HPE R1N26A", "bgp_asn": "65100"}},
                  "PORT": {}, "VLAN": {"Vlan100": {"vlanid": "100"}}, "VLAN_MEMBER": {}, "PORTCHANNEL": {},
                  "PORTCHANNEL_MEMBER": {}, "NTP_SERVER": {"162.159.200.1": {}}, "NTP": {"global": {"enabled": "true"}},
                  "MGMT_PORT": {"eth0": {"admin_status": "up", "alias": "Management"}}, "INTERFACE": {}, "STATIC_ROUTE": {}}
        sfp_lanes = [49,50,51,52,57,58,59,60,61,62,63,64,77,78,79,80,85,86,87,88,93,94,95,96,13,14,15,16,21,22,23,24,29,30,31,32,97,98,99,100,105,106,107,108,113,114,115,116]
        qsfp_lanes = [65,69,121,125,1,33,5,41]
        for i in range(56):
            name = f"Ethernet{i}" if i < 48 else f"Ethernet{48 + (i - 48) * 4}"
            lanes = [sfp_lanes[i]] if i < 48 else list(range(qsfp_lanes[i - 48], qsfp_lanes[i - 48] + 4))
            config["PORT"][name] = {"alias": f"SFP{i+1}" if i < 48 else f"QSFP{i-47}", "index": str(i + 1), "lanes": ",".join(map(str, lanes)),
                                   "speed": "25000" if i < 48 else "100000", "mtu": "9100", "fec": "rs", "admin_status": "up", "description": "模拟端口"}
        return config

    def _demo_state(self, table):
        if table == "PORT_TABLE":
            return {name: {"oper_status": "up" if name in ("Ethernet52", "Ethernet64") and row.get("admin_status") == "up" else "down",
                           "supported_speeds": "10000,25000" if len(str(row.get("lanes", "")).split(",")) == 1 else "40000,100000"} for name, row in self._demo_config["PORT"].items()}
        if table == "SYSTEM_HEALTH_INFO":
            return {"System": {"status": "OK", "message": "DEMO simulated health"}}
        if table == "FAN_INFO":
            return {f"Fan {i}": {"presence": "True", "status": "True", "speed": "40", "timestamp": _utc(), "source": "DEMO"} for i in range(1, 11)}
        if table == "PSU_INFO":
            return {f"PSU {i}": {"presence": "True", "status": "True", "power": "50", "timestamp": _utc(), "source": "DEMO"} for i in (1, 2)}
        if table == "TEMPERATURE_INFO":
            return {"CPU": {"temperature": "34", "warning_status": "False", "timestamp": _utc(), "source": "DEMO"}}
        if table == "TRANSCEIVER_INFO":
            return {name: {"type": "QSFP28", "manufacturer": "DEMO", "model": "Simulated DAC"} for name in ("Ethernet52", "Ethernet64")}
        return {}
