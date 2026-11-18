"""Cached, read-only local IPMI monitoring. No remote login or console writes.

Section ``state`` describes collection success, never hardware health. Raw
stdout/stderr is private, bounded and never included in snapshot results.
Commands have a five-second deadline, except the last 30 SEL events (20 seconds).
Both remain bounded by the 25-second collection deadline and 30-second cache.
SEL uses ordinary ``list`` output with raw sensor type/ID, without SDR lookup.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import ipaddress
import os
import re
import signal
import subprocess
import threading
import time


COMMANDS = {
    "identity": ("identity", ("mc", "info")),
    "selftest": ("status", ("mc", "selftest")),
    "chassis": ("status", ("chassis", "status")),
    "sel_info": ("status", ("sel", "info")),
    "network": ("network", ("lan", "print", "1")),
    "sol_info": ("console", ("sol", "info", "1")),
    "events": ("events", ("sel", "list", "last", "30")),
}
ERRORS = {
    "timeout": "本地 IPMI 读取超时。",
    "missing": "未找到本地 ipmitool。",
    "failed": "本地 IPMI 命令不可用或读取失败。",
    "output_limit": "IPMI 输出超过安全长度限制。",
    "budget": "本轮采集已达到总时限，未执行此项读取。",
    "parse": "未识别到此项的预期字段，状态未知。",
    "unexpected": "本地 IPMI 采集发生错误，状态未知。",
}


class BmcReadError(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else "unexpected"
        super().__init__(ERRORS[self.code])


def _safe(value, maximum=180):
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    value = re.sub(r"(?i)\b(password|passwd|secret|community|token|auth.?key|priv.?key)\s*(?:[:=]\s*|\s+)\S+", r"\1=[redacted]", value)
    return value[:maximum]


def _pairs(text):
    result = {}
    for line in text.splitlines()[:512]:
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


def _rows(values, fields):
    return [{"field": label, "value": _safe(values[key.lower()])} for key, label in fields
            if key.lower() in values and values[key.lower()] != ""]


def _identity(text):
    values = _pairs(text)
    rows = _rows(values, (("Manufacturer Name", "制造商"), ("Manufacturer ID", "制造商 ID"),
                         ("Product ID", "产品 ID"), ("Product Name", "产品名称"),
                         ("Device ID", "设备 ID"), ("Device Revision", "设备版本"),
                         ("Firmware Revision", "BMC 固件版本"), ("IPMI Version", "IPMI 版本"),
                         ("Device Available", "设备报告可用")))
    if not rows:
        raise BmcReadError("parse")
    return {"rows": rows}


def _selftest(text):
    values = _pairs(text)
    if not values.get("selftest"):
        raise BmcReadError("parse")
    return {"rows": [{"field": "BMC 自检结果", "value": _safe(values["selftest"])}]}


def _chassis(text):
    rows = _rows(_pairs(text), (("System Power", "系统电源"), ("Power Overload", "电源过载"),
        ("Power Interlock", "电源联锁"), ("Main Power Fault", "主电源故障"),
        ("Power Control Fault", "电源控制故障"), ("Power Restore Policy", "供电恢复策略"),
        ("Last Power Event", "最近电源事件"), ("Chassis Intrusion", "机箱入侵状态"),
        ("Front-Panel Lockout", "前面板锁定"), ("Drive Fault", "驱动器故障"),
        ("Cooling/Fan Fault", "散热/风扇故障")))
    if not rows:
        raise BmcReadError("parse")
    return {"rows": rows}


def _sel_info(text):
    rows = _rows(_pairs(text), (("Version", "SEL 版本"), ("Entries", "SEL 记录总数"),
        ("Free Space", "SEL 剩余空间"), ("Percent Used", "SEL 空间使用率"),
        ("Last Add Time", "SEL 最近写入时间"), ("Last Del Time", "SEL 最近清除时间"),
        ("Overflow", "SEL 溢出标志")))
    if not rows:
        raise BmcReadError("parse")
    return {"rows": rows}


def _network(text):
    """Only selected validated fields; community/auth/user data never returned."""
    values, rows = _pairs(text), []
    ipv4, configured = None, None
    for key, label in (("ip address", "BMC IPv4 地址"), ("subnet mask", "子网掩码"), ("default gateway ip", "默认网关")):
        if key not in values:
            continue
        try:
            value = str(ipaddress.IPv4Address(values[key]))
        except ipaddress.AddressValueError:
            continue
        rows.append({"field": label, "value": value})
        if key == "ip address":
            ipv4, configured = value, value != "0.0.0.0"
    source = values.get("ip address source")
    if source in ("Unspecified", "Static Address", "DHCP Address", "BIOS Assigned Address", "Other"):
        rows.append({"field": "地址来源", "value": source})
    for key, label in (("mac address", "BMC MAC 地址"), ("default gateway mac", "网关 MAC 地址")):
        value = values.get(key, "")
        if re.fullmatch(r"(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}", value):
            rows.append({"field": label, "value": value.lower()})
    vlan = values.get("802.1q vlan id", "")
    if vlan.lower() == "disabled" or (vlan.isdigit() and 0 <= int(vlan) <= 4094):
        rows.append({"field": "BMC VLAN", "value": vlan})
    if not rows:
        raise BmcReadError("parse")
    return {"rows": rows, "channel": 1, "ipv4_address": ipv4, "configured": configured,
            "reachability": "not_tested", "reason": "未配置 BMC IPv4 地址。" if configured is False else "仅读取本地 LAN 配置；未测试远程网络可达性。"}


def _console(text):
    values = _pairs(text)
    enabled = values.get("enabled", "").lower()
    if enabled not in ("true", "false"):
        raise BmcReadError("parse")
    rows = _rows(values, (("Enabled", "SOL 配置启用"), ("Privilege Level", "SOL 权限级别"),
        ("Volatile Bit Rate (kbps)", "当前波特率 kbps"), ("Non-Volatile Bit Rate (kbps)", "持久波特率 kbps"),
        ("Payload Channel", "SOL 通道"), ("Payload Port", "SOL UDP 端口")))
    for key, label, note in (("force encryption", "强制加密", "未强制；不表示不支持加密"),
                             ("force authentication", "强制认证", "未强制；不表示不支持认证")):
        value = values.get(key, "").lower()
        if value in ("true", "false"):
            rows.append({"field": label, "value": value + ("（" + note + "）" if value == "false" else "")})
    return {"rows": rows, "supported": True, "configuration_readable": True, "enabled": enabled == "true",
            "available": False, "session_verified": False, "protocol": "IPMI SOL",
            "reason": "SOL 配置可读；交互会话未验证。此页面只读，不建立 SOL 会话；SOL 不等同于 BMC 自身 shell。"}


def _events(text):
    if re.search(r"(?i)\bSEL has no entries\b", text):
        return {"rows": [], "limit": 30}
    rows, rejected = [], 0
    for line in text.splitlines()[:128]:
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6 or not re.fullmatch(r"[a-fA-F0-9]+", parts[0]):
            rejected += 1
            continue
        rows.append({"id": _safe(parts[0], 16), "timestamp": _safe(parts[1] + " " + parts[2], 80),
                     "sensor": _safe(parts[3]), "event": _safe(parts[4]) or "未提供事件描述",
                     "direction": _safe(parts[5], 32), "detail": _safe(" | ".join(parts[6:]))})
    if not rows:
        raise BmcReadError("parse")
    return {"rows": rows[-30:], "limit": 30, "unparsed_lines": rejected,
            "reason": "历史 SEL 记录；Asserted/Deasserted 不直接代表当前仍有故障，时间来自 BMC。传感器使用 SEL 原始类型/ID，未额外解析 SDR 名称。"}


PARSERS = {"identity": _identity, "selftest": _selftest, "chassis": _chassis,
           "sel_info": _sel_info, "network": _network, "sol_info": _console, "events": _events}


class BmcMonitor:
    MAX_OUTPUT = 65536
    COMMAND_SECONDS = 5
    EVENT_SECONDS = 20
    COLLECTION_SECONDS = 25

    def __init__(self, demo=False, cache_seconds=30):
        if type(demo) is not bool:
            raise TypeError("demo must be an explicit boolean")
        if isinstance(cache_seconds, bool) or not isinstance(cache_seconds, (int, float)) or not 5 <= cache_seconds <= 300:
            raise ValueError("cache_seconds must be between 5 and 300")
        self.demo, self.cache_seconds = demo, cache_seconds
        self._lock = threading.Lock()
        self._cache = None
        self._cached_at = 0

    @staticmethod
    def _terminate(process):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def _execute(self, command_id, timeout):
        """Only fixed argv; bounded in-memory drain, process-group timeout."""
        if command_id not in COMMANDS:
            raise BmcReadError("failed")
        if self.demo:
            raise BmcReadError("failed")  # Demo collection must never call host tools.
        argv = ["ipmitool", "-I", "open", *COMMANDS[command_id][1]]
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, shell=False, start_new_session=os.name == "posix")
        except FileNotFoundError as exc:
            raise BmcReadError("missing") from exc
        except OSError as exc:
            raise BmcReadError("failed") from exc
        data, exceeded, done, read_error = bytearray(), threading.Event(), threading.Event(), threading.Event()
        def drain():
            try:
                while True:
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        break
                    if len(data) + len(chunk) > self.MAX_OUTPUT:
                        exceeded.set()
                        break
                    data.extend(chunk)
            except (OSError, ValueError):
                read_error.set()
            finally:
                done.set()
        reader = threading.Thread(target=drain, daemon=True, name="bmc-output")
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while not (done.is_set() and process.poll() is not None):
                if exceeded.is_set():
                    raise BmcReadError("output_limit")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BmcReadError("timeout")
                if done.is_set():
                    time.sleep(min(remaining, 0.025))
                else:
                    done.wait(min(remaining, 0.025))
            if exceeded.is_set():
                raise BmcReadError("output_limit")
            if read_error.is_set():
                raise BmcReadError("failed")
            if process.returncode:
                raise BmcReadError("failed")
            return bytes(data).decode("utf-8", "replace")
        finally:
            if process.poll() is None or not done.is_set():
                self._terminate(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._terminate(process)
            reader.join(timeout=1)
            if done.is_set():
                process.stdout.close()

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cached_at < self.cache_seconds:
                result = copy.deepcopy(self._cache)
                result["collection"]["cached"] = True
                return result
            result = self._collect()
            self._cached_at = time.monotonic()
            self._cache = copy.deepcopy(result)
            return result

    def _collect(self):
        start = time.monotonic()
        result = {"mode": "demo" if self.demo else "live"}
        sections = ("identity", "status", "network", "console", "events")
        for section in sections:
            result[section] = {"state": "unknown", "rows": [], "error": None}
        result["console"].update({"supported": None, "available": False, "session_verified": False,
                                  "configuration_readable": False, "protocol": "IPMI SOL",
                                  "reason": "SOL 能力/交互会话尚未确认；此页面不建立连接，也不提供 BMC shell。"})
        records, errors, successes = [], {s: [] for s in sections}, {s: 0 for s in sections}
        for command_id, (section, _) in COMMANDS.items():
            command_start = time.monotonic()
            remaining = self.COLLECTION_SECONDS - (command_start - start)
            error = None
            try:
                if remaining <= 0:
                    raise BmcReadError("budget")
                command_seconds = self.EVENT_SECONDS if command_id == "events" else self.COMMAND_SECONDS
                text = DEMO[command_id] if self.demo else self._execute(command_id, min(command_seconds, remaining))
                if not isinstance(text, str) or len(text.encode("utf-8")) > self.MAX_OUTPUT:
                    raise BmcReadError("output_limit")
                parsed = PARSERS[command_id](text)
                result[section]["rows"].extend(parsed.pop("rows"))
                result[section].update(parsed)
                successes[section] += 1
                if parsed.get("unparsed_lines", 0):
                    error = "部分事件行格式未识别；已显示可解析记录。"
            except BmcReadError as exc:
                error = ERRORS[exc.code]
            except Exception:
                # Never forward exception/CLI text which may contain credentials.
                error = ERRORS["unexpected"]
            if error:
                errors[section].append(error)
            records.append({"section": section, "command_id": command_id, "ok": error is None,
                            "elapsed_ms": round((time.monotonic() - command_start) * 1000), "error": error})
        for section in sections:
            result[section]["state"] = "partial" if successes[section] and errors[section] else "ok" if successes[section] else "unknown"
            result[section]["error"] = " ".join(dict.fromkeys(errors[section])) or None
        if result["console"].get("configuration_readable"):
            if result["network"].get("configured") is False:
                result["console"]["reason"] = "SOL 配置可读，但 BMC IPv4 未配置；交互会话未验证。本页不建立 SOL 连接，SOL 也不等同于 BMC 自身 shell。"
            elif not result["console"].get("enabled"):
                result["console"]["reason"] = "SOL 配置当前未启用；本页只读，不更改配置或建立会话。"
        result["collection"] = {"sampled_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.monotonic() - start) * 1000), "cache_seconds": self.cache_seconds,
            "cached": False, "state_semantics": "state/ok 仅表示读取成功；硬件状态以各项实际值为准。",
            "commands": records, "errors": {s: e for s, e in errors.items() if e}, "simulated": self.demo}
        if self.demo:
            result["collection"]["notice"] = "DEMO：以下 BMC 数据全部为模拟，没有访问硬件。"
        return result


DEMO = {
    "identity": "Manufacturer Name: DEMO Celestica\nManufacturer ID: 12290\nProduct ID: 1156 (0x0484)\nFirmware Revision: 0.32\nIPMI Version: 2.0\nDevice Available: yes\n",
    "selftest": "Selftest: passed\n",
    "chassis": "System Power: on\nPower Overload: false\nMain Power Fault: false\nPower Control Fault: false\nCooling/Fan Fault: false\n",
    "sel_info": "Entries: 2\nPercent Used: 0%\nOverflow: false\n",
    "network": "IP Address Source: Unspecified\nIP Address: 0.0.0.0\nSubnet Mask: 0.0.0.0\nMAC Address: 02:00:00:00:00:01\n802.1q VLAN ID: Disabled\n",
    "sol_info": "Enabled: true\nForce Encryption: false\nForce Authentication: false\nPrivilege Level: USER\nVolatile Bit Rate (kbps): 115.2\nNon-Volatile Bit Rate (kbps): 115.2\nPayload Channel: 1 (0x01)\nPayload Port: 623\n",
    "events": "1 | DEMO date | DEMO time | Power Supply | Power Supply AC lost | Asserted\n2 | DEMO date | DEMO time | Power Supply | Power Supply AC lost | Deasserted\n",
}
