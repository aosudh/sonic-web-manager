#!/usr/bin/env python3
"""Run ONLY inside a fresh `unshare --net` namespace on the development host.

Refuses any namespace with an interface other than loopback. Does not contact BMC.
"""
import json
import subprocess

from host_usb_link import LinkManager
from test_host_usb_link import PROFILE


def run(*args, ok=True):
    result = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=5)
    if ok and result.returncode:
        raise RuntimeError(result.stderr)
    return result


links = json.loads(run("-j", "link", "show").stdout)
assert [link["ifname"] for link in links] == ["lo"], "Not a fresh isolated network namespace"
run("link", "set", "dev", "lo", "up")
run("link", "add", "testusb", "type", "dummy")
run("link", "set", "dev", "testusb", "up")
run("address", "add", "169.254.100.2/32", "dev", "testusb", "noprefixroute")
run("link", "add", "testlan", "type", "dummy")
run("link", "set", "dev", "testlan", "up")
run("address", "add", "192.0.2.1/24", "dev", "testlan")
run("route", "add", "default", "dev", "testlan")
manager = LinkManager(PROFILE, "/run/not-used-by-policy-test")
run(*manager.rule_args("deny"))
run(*manager.rule_args("local"))
run(*manager.route_args("testusb"))
rules = json.loads(run("-4", "-j", "rule", "show").stdout)
routes = json.loads(run("-4", "-j", "route", "show", "table", "all").stdout)
assert any(manager.rule_matches(r, "local") for r in rules), rules
assert any(manager.rule_matches(r, "deny") for r in rules), rules
assert any(manager.route_matches(r, "testusb") for r in routes), routes
local = json.loads(run("-4", "-j", "route", "get", "169.254.100.1").stdout)
assert local[0]["dev"] == "testusb" and local[0]["prefsrc"] == "169.254.100.2", local
forward = run("-4", "route", "get", "169.254.100.1", "from", "192.0.2.2", "iif", "testlan", ok=False)
assert forward.returncode and any(s in forward.stderr.lower() for s in ("unreachable", "no route to host")), forward
run(*manager.route_args("testusb", "del"))
absent = run("-4", "route", "get", "169.254.100.1", ok=False)
assert absent.returncode and any(s in absent.stderr.lower() for s in ("unreachable", "no route to host")), absent
main = json.loads(run("-4", "-j", "route", "show", "table", "main").stdout)
assert not any(r.get("dev") == "testusb" for r in main), main
print(json.dumps({"result": "PASS", "rule_json": rules[-4:-2],
                  "local_usb_route": local, "forwarded_peer": "unreachable",
                  "missing_usb_route": "unreachable", "main_table_usb_routes": 0}, sort_keys=True))
