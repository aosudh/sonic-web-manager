#!/usr/bin/env python3
"""Start the manager on the current management address, never all interfaces."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def management_address():
    result = subprocess.run(
        ["ip", "-j", "-4", "address", "show", "dev", "eth0"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    for device in json.loads(result.stdout):
        for address in device.get("addr_info", []):
            if address.get("scope") == "global":
                candidate = ipaddress.ip_address(address["local"])
                if candidate.version == 4 and not (candidate.is_loopback or candidate.is_global or candidate.is_unspecified or candidate.is_multicast):
                    return str(candidate)
    raise RuntimeError("eth0 has no usable IPv4 management address")


def main():
    root = Path(__file__).resolve().parents[1]
    state = Path("/host/sonic-web/state")
    for path in (state, state / "tls", state / "data", state / "service.json", state / "tls" / "cert.pem", state / "tls" / "key.pem"):
        if path.is_symlink():
            raise RuntimeError("Managed service state must not contain symlinks")
    config = json.loads((state / "service.json").read_text())
    if type(config.get("port")) is not int or not 1024 <= config["port"] <= 65535:
        raise ValueError("Invalid management service port")
    address = None
    for attempt in range(30):
        try:
            address = management_address()
            break
        except (RuntimeError, subprocess.SubprocessError, ValueError) as error:
            if attempt == 29:
                raise
            print(f"Waiting for eth0 management address: {error}", flush=True)
            time.sleep(2)
    os.chdir(root)
    arguments = [
        sys.executable, "-m", "server.app", "--bind", address,
        "--port", str(config["port"]), "--data-dir", str(state / "data"),
        "--static-dir", str(root / "ui" / "dist"),
        "--cert", str(state / "tls" / "cert.pem"),
        "--key", str(state / "tls" / "key.pem"),
    ]
    print(f"Starting SONiC Web Manager on https://{address}:{config['port']}", flush=True)
    os.execv(sys.executable, arguments)


if __name__ == "__main__":
    main()
