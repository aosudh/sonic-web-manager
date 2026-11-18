#!/usr/bin/env python3
"""Opt-in, host-only BMC USB IPv4 link. Draft: no BMC commands or discovery probes.

--check reads sysfs/iproute2 only. --run is the explicit network mutation mode.
Use an independently reviewed machine profile; addresses are never guessed.
"""
import argparse
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time


class Conflict(RuntimeError):
    pass


def validate_config(value):
    required = {"version", "vendor", "product", "serial", "usb_path", "drivers",
                "host_address", "peer_address", "route_table", "rule_priority",
                "poll_seconds"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"transport_recovery"} or value["version"] != 1:
        raise Conflict("Profile schema must be exactly version 1")
    c = dict(value)
    for key in ("vendor", "product"):
        if not isinstance(c[key], str) or not re.fullmatch(r"[0-9a-f]{4}", c[key]):
            raise Conflict("Invalid USB VID/PID")
    if not isinstance(c["serial"], str) or not 1 <= len(c["serial"]) <= 128 or any(ord(x) < 32 for x in c["serial"]):
        raise Conflict("A verified USB serial is required")
    if not isinstance(c["usb_path"], str) or not re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", c["usb_path"]):
        raise Conflict("A verified physical USB path is required")
    if not isinstance(c["drivers"], list) or not c["drivers"] or any(x not in {"cdc_ether", "rndis_host"} for x in c["drivers"]):
        raise Conflict("Unsupported USB network driver")
    for key in ("host_address", "peer_address"):
        try:
            addr = ipaddress.IPv4Address(c[key])
        except (ValueError, TypeError):
            raise Conflict("Profile requires plain IPv4 addresses") from None
        if not addr.is_link_local:
            raise Conflict("Version 1 supports verified IPv4 link-local addresses only")
        c[key] = str(addr)
    if c["host_address"] == c["peer_address"]:
        raise Conflict("Host and BMC addresses must differ")
    if type(c["route_table"]) is not int or not 1000 <= c["route_table"] <= 32000:
        raise Conflict("Use a dedicated numeric route table between 1000 and 32000")
    if type(c["rule_priority"]) is not int or not 100 <= c["rule_priority"] <= 32000:
        raise Conflict("Invalid reserved rule priority")
    if type(c["poll_seconds"]) is not int or not 2 <= c["poll_seconds"] <= 30:
        raise Conflict("Polling interval must be 2 to 30 seconds")
    recovery = c.get("transport_recovery")
    if recovery is not None:
        if recovery != "questone2-bmc-0.32-cdc" or any(c[k] != v for k, v in {
                "vendor": "046b", "product": "ffb0", "drivers": ["cdc_ether"]}.items()) or c["serial"].startswith("REPLACE_"):
            raise Conflict("Transport reset is available only for the explicitly verified Questone2 profile")
    return c


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Conflict("Configuration/state must be a root-owned, non-writable regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return json.load(stream)
    finally:
        if fd is not None:
            os.close(fd)


def emit(event, **fields):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "event": event, **fields}, sort_keys=True), flush=True)


def prefix(value):
    return str(ipaddress.ip_network(value, strict=False))


class LinkManager:
    SYSCTLS = {"ipv4/forwarding": "0", "ipv4/rp_filter": "0",
               "ipv4/accept_redirects": "0", "ipv4/send_redirects": "0",
               "ipv6/forwarding": "0", "ipv6/accept_ra": "0", "ipv6/autoconf": "0"}

    def __init__(self, config, state_dir, sys_root=Path("/sys"), proc_root=Path("/proc")):
        self.c = validate_config(config)
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        self.sys = Path(sys_root)
        self.proc = Path(proc_root)
        self.ip = next((p for p in ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip") if Path(p).is_file()), None)
        self.state = {"version": 1, "profile": self.c, "rules": [], "device": None}

    def command(self, args, json_result=False, allow_error=False):
        if self.ip is None:
            raise Conflict("iproute2 executable not found")
        r = subprocess.run([self.ip, *args], capture_output=True, text=True, timeout=5,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        if r.returncode and not allow_error:
            raise Conflict("iproute2 failed: " + " ".join(args[:5]) + ": " + r.stderr.strip()[:240])
        if json_result:
            return json.loads(r.stdout) if not r.returncode else []
        return r

    def save(self):
        raw = json.dumps(self.state, sort_keys=True).encode()
        temporary = self.state_dir / "state.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self):
        if self.state_path.exists():
            value = read_private_json(self.state_path)
            if value.get("version") != 1 or value.get("profile") != self.c:
                raise Conflict("Existing journal belongs to a different profile; review it before changing configuration")
            self.state = value

    def discover(self):
        matches = []
        for path in (self.sys / "class/net").iterdir():
            device = (path / "device").resolve()
            usb = next((x for x in (device, *device.parents) if (x / "idVendor").is_file()), None)
            if usb is None:
                continue
            if (usb / "idVendor").read_text().strip() != self.c["vendor"] or (usb / "idProduct").read_text().strip() != self.c["product"]:
                continue
            if usb.name != self.c["usb_path"] or (usb / "serial").read_text().strip() != self.c["serial"]:
                continue
            driver = (device / "driver").resolve().name
            if driver not in self.c["drivers"]:
                raise Conflict("Matched USB device has an unapproved network driver")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", path.name):
                raise Conflict("Unsupported interface name")
            identity = {"ifname": path.name, "ifindex": (path / "ifindex").read_text().strip(),
                        "mac": (path / "address").read_text().strip(), "usb_path": usb.name,
                        "devnum": (usb / "devnum").read_text().strip(),
                        "boot_id": (self.proc / "sys/kernel/random/boot_id").read_text().strip()}
            matches.append(identity)
        if len(matches) > 1:
            raise Conflict("USB identity matched multiple network interfaces")
        return matches[0] if matches else None

    def rule_args(self, kind, action="add"):
        c = self.c
        p = str(c["rule_priority"] + (kind == "deny"))
        result = ["-4", "rule", action, "priority", p, "to", c["peer_address"] + "/32"]
        return result + (["iif", "lo", "lookup", str(c["route_table"])] if kind == "local" else ["unreachable"])

    def rule_matches(self, value, kind):
        expected_keys = {"priority", "src", "dst", "flags", "action", "table", "iif"}
        if set(value) - expected_keys or value.get("flags") not in (None, []) or value.get("src", "all") != "all":
            return False
        try:
            if prefix(value.get("dst", "0.0.0.0/0")) != self.c["peer_address"] + "/32":
                return False
        except ValueError:
            return False
        if value.get("priority") != self.c["rule_priority"] + (kind == "deny"):
            return False
        if kind == "local":
            return str(value.get("table")) == str(self.c["route_table"]) and value.get("iif") == "lo" and value.get("action") in (None, "to_tbl", "unicast")
        return value.get("action") == "unreachable" and "table" not in value and "iif" not in value

    def route_args(self, ifname, action="add"):
        return ["-4", "route", action, "table", str(self.c["route_table"]),
                self.c["peer_address"] + "/32", "dev", ifname, "scope", "link",
                "src", self.c["host_address"], "proto", "static"]

    def route_matches(self, value, ifname):
        if set(value) - {"dst", "dev", "scope", "prefsrc", "protocol", "table", "flags", "type"}:
            return False
        try:
            return (prefix(value.get("dst", "0.0.0.0/0")) == self.c["peer_address"] + "/32"
                    and value.get("dev") == ifname and value.get("scope") == "link"
                    and value.get("prefsrc") == self.c["host_address"]
                    and value.get("protocol") == "static"
                    and value.get("type") in (None, "unicast")
                    and value.get("flags") in (None, [], ["linkdown"])
                    and str(value.get("table")) == str(self.c["route_table"]))
        except ValueError:
            return False

    def snapshot(self):
        return {"rules": self.command(["-4", "-j", "rule", "show"], True),
                "routes": self.command(["-4", "-j", "route", "show", "table", "all"], True),
                "links": self.command(["-j", "address", "show"], True)}

    @staticmethod
    def pure_local_rule(rule):
        """SONiC can renumber the unconditional kernel-local lookup to 1001."""
        return (not (set(rule) - {"priority", "src", "table", "flags"})
                and type(rule.get("priority")) is int and rule["priority"] >= 0
                and rule.get("src", "all") == "all"
                and str(rule.get("table")) in ("local", "255")
                and rule.get("flags") in (None, []))

    def preflight(self, device, snap):
        c = self.c
        owned = self.state.get("device")
        own_device = owned is not None and owned["identity"] == device
        for rule in snap["rules"]:
            p = rule.get("priority", -1)
            if p in (c["rule_priority"], c["rule_priority"] + 1):
                kind = "local" if p == c["rule_priority"] else "deny"
                if kind not in self.state["rules"] or not self.rule_matches(rule, kind):
                    raise Conflict("Reserved routing-rule priority is already occupied")
            elif str(rule.get("table")) == str(c["route_table"]):
                raise Conflict("Dedicated table is referenced by an unrelated rule")
            elif 0 <= p < c["rule_priority"] and not self.pure_local_rule(rule):
                raise Conflict("An earlier policy rule requires explicit review before using this profile")
        for kind in ("local", "deny"):
            if sum(self.rule_matches(r, kind) for r in snap["rules"]) > 1:
                raise Conflict("Duplicate owned routing rules")
        for route in snap["routes"]:
            if str(route.get("table")) in ("local", "255"):
                destination = route.get("dst", "default")
                network = ipaddress.IPv4Network("0.0.0.0/0" if destination == "default" else destination, strict=False)
                if ipaddress.IPv4Address(c["peer_address"]) in network:
                    raise Conflict("BMC peer has a matching local-table route")
            if str(route.get("table")) == str(c["route_table"]):
                if not own_device or not owned.get("route") or not self.route_matches(route, device["ifname"]):
                    raise Conflict("Dedicated route table contains an unowned route")
            elif route.get("dst") in (c["peer_address"], c["peer_address"] + "/32"):
                raise Conflict("BMC peer already has a route outside the dedicated table")
            elif device and route.get("dev") == device["ifname"] and route.get("table") not in ("local", 255):
                raise Conflict("USB interface already has an unrelated IPv4 route")
        for link in snap["links"]:
            for addr in link.get("addr_info", []):
                local = addr.get("local")
                if local == c["peer_address"]:
                    raise Conflict("BMC address is already assigned to the host")
                if local == c["host_address"] and (not device or link["ifname"] != device["ifname"]):
                    raise Conflict("Host USB address is in use on another interface")
                if not device or link["ifname"] != device["ifname"]:
                    continue
                if addr["family"] == "inet" and not (own_device and owned.get("address") and local == c["host_address"] and addr.get("prefixlen") == 32):
                    raise Conflict("USB interface has a pre-existing IPv4 address")
                if addr["family"] == "inet6" and addr.get("scope") != "link":
                    raise Conflict("USB interface has a non-link-local IPv6 address")
            if device and link["ifname"] == device["ifname"]:
                if link.get("master") is not None:
                    raise Conflict("USB interface is enslaved to another device")
        if device:
            global_rpf = (self.proc / "sys/net/ipv4/conf/all/rp_filter").read_text().strip()
            if global_rpf != "0":
                raise Conflict("Global rp_filter is nonzero; this local-only routing design needs separate validation")

    def sysctl_path(self, ifname, key):
        family, name = key.split("/")
        return self.proc / "sys/net" / family / "conf" / ifname / name

    def sysctl_read(self, ifname, key):
        path = self.sysctl_path(ifname, key)
        return path.read_text().strip() if path.exists() else None

    def sysctl_write(self, ifname, key, value):
        self.sysctl_path(ifname, key).write_text(value + "\n")

    def remember(self, key, value):
        self.state["device"][key] = value
        self.save()

    def ensure_rule(self, kind, snap):
        if not any(self.rule_matches(r, kind) for r in snap["rules"]):
            if kind not in self.state["rules"]:
                self.state["rules"].append(kind)
                self.save()  # Write-ahead ownership, including interrupted commands.
            self.command(self.rule_args(kind))

    def reconcile(self):
        device = self.discover()
        old = self.state["device"]
        if old and old["identity"] != device:
            self.cleanup_device()
        snap = self.snapshot()
        self.preflight(device, snap)
        # Deny is installed first; it also prevents default-route fallback while USB is absent.
        self.ensure_rule("deny", snap)
        self.ensure_rule("local", snap)
        if device is None:
            return {"state": "waiting_for_usb"}
        if self.state["device"] is None:
            link = next(x for x in snap["links"] if x["ifname"] == device["ifname"])
            values = {k: self.sysctl_read(device["ifname"], k) for k in self.SYSCTLS}
            if any(values[k] is None for k in self.SYSCTLS if k.startswith("ipv4/")):
                raise Conflict("Required per-interface IPv4 sysctls are unavailable")
            self.state["device"] = {"identity": device, "original_up": "UP" in link.get("flags", []),
                                    "original_sysctls": values, "sysctls": [], "link": False,
                                    "address": False, "route": False}
            self.save()
        state = self.state["device"]
        for key, expected in self.SYSCTLS.items():
            current = self.sysctl_read(device["ifname"], key)
            if current is None:
                continue  # IPv6 may be disabled in the kernel.
            if current not in (expected, state["original_sysctls"][key]):
                raise Conflict("An owned USB sysctl was changed externally")
            if current != expected:
                if key not in state["sysctls"]:
                    state["sysctls"].append(key)
                    self.save()
                self.sysctl_write(device["ifname"], key, expected)
        link = next(x for x in snap["links"] if x["ifname"] == device["ifname"])
        if "UP" not in link.get("flags", []):
            self.remember("link", True)
            self.command(["link", "set", "dev", device["ifname"], "up"])
        if not any(a.get("family") == "inet" and a.get("local") == self.c["host_address"] and a.get("prefixlen") == 32 for a in link.get("addr_info", [])):
            self.remember("address", True)
            self.command(["-4", "address", "add", self.c["host_address"] + "/32", "dev", device["ifname"], "noprefixroute"])
        if not any(self.route_matches(r, device["ifname"]) for r in snap["routes"]):
            self.remember("route", True)
            self.command(self.route_args(device["ifname"]))
        resolved = self.command(["-4", "-j", "route", "get", self.c["peer_address"]], True)
        if len(resolved) != 1 or resolved[0].get("dev") != device["ifname"] or resolved[0].get("prefsrc", resolved[0].get("src")) != self.c["host_address"]:
            raise Conflict("Effective local route did not resolve to the verified USB interface")
        return {"state": "configured", "interface": device["ifname"], "ifindex": device["ifindex"],
                "host": self.c["host_address"] + "/32", "peer": self.c["peer_address"] + "/32"}

    def cleanup_device(self):
        state = self.state["device"]
        if not state:
            return
        identity = state["identity"]
        snap = self.snapshot()
        # Only remove the exact owned route. A different interface with a reused name is never touched.
        current = self.discover()
        same = current == identity
        routes = [r for r in snap["routes"] if str(r.get("table")) == str(self.c["route_table"])]
        if routes:
            if not same or len(routes) != 1 or not state["route"] or not self.route_matches(routes[0], identity["ifname"]):
                raise Conflict("Cannot clean an altered or reassigned USB route")
            self.command(self.route_args(identity["ifname"], "del"))
        if same:
            ifname = identity["ifname"]
            link = next(x for x in snap["links"] if x["ifname"] == ifname)
            matching = [a for a in link.get("addr_info", []) if a.get("family") == "inet" and a.get("local") == self.c["host_address"]]
            if matching:
                if not state["address"] or len(matching) != 1 or matching[0].get("prefixlen") != 32:
                    raise Conflict("Cannot clean an altered USB address")
                self.command(["-4", "address", "del", self.c["host_address"] + "/32", "dev", ifname])
            unrelated = [a for a in link.get("addr_info", [])
                         if (a.get("family") == "inet" and a not in matching)
                         or (a.get("family") == "inet6" and a.get("scope") != "link")]
            if unrelated or any(r.get("dev") == ifname and str(r.get("table")) not in ("local", "255", str(self.c["route_table"])) for r in snap["routes"]):
                raise Conflict("USB now carries unrelated network state; leaving its link/sysctls unchanged")
            # Do not undo an interface's pre-existing administrative UP state.
            if state["link"] and not state["original_up"]:
                self.command(["link", "set", "dev", ifname, "down"])
            for key in reversed(state["sysctls"]):
                value = self.sysctl_read(ifname, key)
                if value == self.SYSCTLS[key]:
                    self.sysctl_write(ifname, key, state["original_sysctls"][key])
                elif value != state["original_sysctls"][key]:
                    raise Conflict("Cannot restore an externally modified USB sysctl")
        self.state["device"] = None
        self.save()

    def cleanup(self):
        # Keep the deny rule until the link/route are gone, so teardown cannot expose BMC forwarding.
        self.cleanup_device()
        for kind in ("local", "deny"):
            if kind not in self.state["rules"]:
                continue
            rules = self.command(["-4", "-j", "rule", "show"], True)
            at_priority = [r for r in rules if r.get("priority") == self.c["rule_priority"] + (kind == "deny")]
            if at_priority:
                if len(at_priority) != 1 or not self.rule_matches(at_priority[0], kind):
                    raise Conflict("Refusing to remove an externally modified policy rule")
                self.command(self.rule_args(kind, "del"))
            self.state["rules"].remove(kind)
            self.save()
        self.state_path.unlink(missing_ok=True)


class TransportHealth:
    """Only this opt-in old AMI profile may request a rate-limited USB reset."""
    INTERVAL, TIMEOUT, FAILURES, COOLDOWN, WINDOW, MAX_ATTEMPTS = 10, 1, 3, 300, 3600, 3

    def __init__(self, manager, clock=time.monotonic):
        self.manager = manager
        self.clock = clock
        self.enabled = manager.c.get("transport_recovery") == "questone2-bmc-0.32-cdc"
        self.last_probe = None
        self.failures = 0
        self.identity = None
        self.rate_path = manager.state_dir / "recovery.json"
        self.pending = None

    def probe(self):
        """Only connect timeouts count: a connected but silent SSH server is not a USB diagnosis."""
        connected = False
        deadline = self.clock() + self.TIMEOUT
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
                stream.settimeout(self.TIMEOUT)
                stream.bind((self.manager.c["host_address"], 0))
                stream.connect((self.manager.c["peer_address"], 22))
                connected = True
                banner = b""
                while len(banner) < 255 and b"\n" not in banner:
                    stream.settimeout(max(0.001, deadline - self.clock()))
                    chunk = stream.recv(255 - len(banner))
                    if not chunk:
                        return "service_closed"
                    banner += chunk
                    if self.clock() >= deadline and b"\n" not in banner:
                        return "banner_timeout"
                line = banner.split(b"\n", 1)[0].rstrip(b"\r")
                return "ssh_banner_verified" if line.startswith((b"SSH-2.0-", b"SSH-1.99-")) and all(32 <= x <= 126 for x in line) else "unexpected_banner"
        except (TimeoutError, socket.timeout):
            return "banner_timeout" if connected else "connect_timeout"
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                return "port_refused_network_alive"
            if exc.errno == errno.ETIMEDOUT and not connected:
                return "connect_timeout"
            return "socket_error_" + str(exc.errno)

    def boot_id(self):
        return (self.manager.proc / "sys/kernel/random/boot_id").read_text().strip()

    def read_rate(self, now):
        empty = {"version": 1, "boot_id": self.boot_id(), "attempts": [], "blocked": False}
        if not self.rate_path.exists():
            return empty
        record = read_private_json(self.rate_path)
        if (set(record) != set(empty) or record["version"] != 1 or type(record["blocked"]) is not bool
                or not isinstance(record["attempts"], list) or len(record["attempts"]) > self.MAX_ATTEMPTS):
            raise Conflict("Invalid reset rate journal; reset disabled")
        if record["boot_id"] != empty["boot_id"]:
            return empty
        if any(type(t) not in (int, float) or not 0 <= t <= now for t in record["attempts"]):
            raise Conflict("Invalid monotonic reset timestamps; reset disabled")
        record["attempts"] = [t for t in record["attempts"] if now - t < self.WINDOW]
        return record

    def save_rate(self, record):
        temporary = self.rate_path.with_suffix(".tmp")
        # Parent directory is root-only and the service's flock serializes writers.
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.rate_path)
        directory = os.open(self.manager.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def verified_node(self, expected):
        if not self.enabled or self.manager.discover() != expected:
            raise Conflict("USB identity changed before transport recovery")
        usb = self.manager.sys / "bus/usb/devices" / self.manager.c["usb_path"]
        checks = {"idVendor": "046b", "idProduct": "ffb0", "serial": self.manager.c["serial"], "bConfigurationValue": "2"}
        if any((usb / key).read_text().strip() != value for key, value in checks.items()):
            raise Conflict("USB identity/configuration is not the verified CDC configuration 2")
        bus, dev = int((usb / "busnum").read_text()), int((usb / "devnum").read_text())
        if not 1 <= bus <= 999 or not 1 <= dev <= 127 or str(dev) != expected["devnum"]:
            raise Conflict("USB bus/device number changed")
        major, minor = map(int, (usb / "dev").read_text().strip().split(":"))
        if major != 189 or minor != (bus - 1) * 128 + dev - 1:
            raise Conflict("Unexpected USB character-device identity")
        return Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}"), os.makedev(major, minor)

    def perform_reset(self, expected):
        node, device_number = self.verified_node(expected)
        fd = os.open(node, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_rdev != device_number:
                raise Conflict("Opened USB node is not the verified device")
            if self.verified_node(expected) != (node, device_number):
                raise Conflict("USB identity changed while opening the device")
            # Fixed helper inherits only this already-open verified device. No shell/commands from config.
            self.pending = subprocess.Popen([sys.executable, "-I", "-c",
                "import fcntl,sys; fcntl.ioctl(int(sys.argv[1]),0x5514,0)", str(fd)],
                pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = self.pending.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.pending.kill()
                try:
                    self.pending.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    return "reset_helper_stuck"
                return "reset_helper_timeout"
            finally:
                if self.pending.poll() is not None:
                    self.pending = None
            return "reset_requested" if code == 0 else "reset_failed"
        finally:
            os.close(fd)

    def tick(self, link_state):
        if not self.enabled:
            return None
        now = self.clock()
        if link_state.get("state") != "configured":
            self.failures, self.identity = 0, None
            return None
        current = self.manager.state["device"]["identity"]
        if current != self.identity:
            self.failures, self.identity = 0, dict(current)
        if self.last_probe is not None and now - self.last_probe < self.INTERVAL:
            return None
        self.last_probe = now
        outcome = self.probe()
        self.failures = min(self.FAILURES, self.failures + 1) if outcome == "connect_timeout" else 0
        result = {"state": outcome, "consecutive_connect_timeouts": self.failures}
        if self.failures < self.FAILURES:
            return result
        try:
            record = self.read_rate(now)
            if record["blocked"]:
                return {**result, "recovery": "blocked_previous_helper_stuck"}
            if len(record["attempts"]) >= self.MAX_ATTEMPTS:
                return {**result, "recovery": "hourly_limit"}
            if record["attempts"] and now - max(record["attempts"]) < self.COOLDOWN:
                return {**result, "recovery": "cooldown"}
            self.verified_node(current)  # Do not consume budget for a different device/configuration.
            record["attempts"].append(now)
            record["blocked"] = True  # A crash during an uncertain reset must not spawn another reset.
            self.save_rate(record)  # Before starting helper: failed attempts/restarts cannot evade limits.
            reset = self.perform_reset(current)
            record["blocked"] = reset == "reset_helper_stuck"
            self.save_rate(record)
            self.failures = 0
            return {**result, "recovery": reset, "attempts_last_hour": len(record["attempts"])}
        except (Conflict, OSError, ValueError) as exc:
            return {**result, "recovery": "refused", "reason": str(exc)[:200]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", default="/run/sonic-bmc-usb-link")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Read-only discovery/preflight; no file or network writes")
    mode.add_argument("--run", action="store_true", help="Explicitly apply and supervise the reviewed profile")
    mode.add_argument("--cleanup", action="store_true", help="Remove only resources owned by the saved journal")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise Conflict("Run as root for trustworthy configuration and sysfs reads")
    manager = LinkManager(read_private_json(args.config), args.state_dir)
    if args.check:
        manager.load()
        device = manager.discover()
        manager.preflight(device, manager.snapshot())
        emit("preflight", result="ready" if device else "waiting_for_usb", device=device,
             route_table=manager.c["route_table"], rule_priorities=[manager.c["rule_priority"], manager.c["rule_priority"] + 1])
        return 0
    directory = Path(args.state_dir)
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise Conflict("State directory must be root-owned mode 0700 and not a symlink")
    lock = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A stale temporary file can only exist inside this root-only directory.
        (directory / "state.tmp").unlink(missing_ok=True)
        manager.load()
        if args.cleanup:
            manager.cleanup()
            emit("cleanup", result="complete")
            return 0
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        last = None
        health, last_health = TransportHealth(manager), None
        try:
            while not stop.is_set():
                result = manager.reconcile()
                if result != last:
                    emit("link", **result)
                    last = result
                health_result = health.tick(result)
                if health_result is not None and health_result != last_health:
                    emit("transport", **health_result)
                    last_health = health_result
                delay = manager.c["poll_seconds"]
                if health.enabled and result.get("state") == "configured" and health.last_probe is not None:
                    delay = min(delay, max(0.05, health.last_probe + health.INTERVAL - time.monotonic()))
                stop.wait(delay)
        finally:
            manager.cleanup()
            emit("cleanup", result="complete")
    finally:
        os.close(lock)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Conflict, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        emit("error", reason=str(exc)[:400])
        sys.exit(1)
