#!/usr/bin/env python3
"""Install only this independently packaged service; never change switch ports."""
import argparse
import atexit
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request

PREFIX = Path("/host/sonic-web")
UNIT = Path("/etc/systemd/system/sonic-web-manager.service")
VERIFY_UNIT = Path("/etc/systemd/system/sonic-web-breakout-verify.service")


def ensure_no_breakout_in_progress():
    """Never replace the worker binary while it owns a reboot transaction."""
    directory = PREFIX / "state" / "data" / "breakout"
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Unsafe breakout state directory")
    current = directory / "current.json"
    if current.exists():
        plain_file(current)
        identifier = json.loads(current.read_text(encoding="utf-8")).get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid current breakout operation")
        if not (directory / identifier / "status.json").is_file():
            raise ValueError("Current breakout operation status is missing")
    for path in directory.glob("*/status.json"):
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Unsafe breakout operation state")
        record = json.loads(path.read_text(encoding="utf-8"))
        manifest_path = path.parent / "manifest.json"
        plain_file(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        unresolved = record.get("status") not in {"succeeded", "rolled_back", "cancelled"}
        if record.get("status") == "failed" and (record.get("files_restored") is True or not manifest or manifest.get("status") in {"rolled_back", "stage_failed_original_files_restored"}):
            unresolved = False
        if manifest.get("status") in {"applying", "rollback_applying", "rollback_write_failed", "stage_failed_restore_incomplete"}:
            unresolved = True
        if unresolved:
            raise RuntimeError("A port-mode operation is pending; finish or recover it before changing the Web release")


def lock_breakout_state():
    import fcntl
    directory = PREFIX / "state" / "data" / "breakout"
    managed_directory(directory, 0o700)
    fd = os.open(directory / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError("A port-mode worker owns the release; retry after it completes") from None
    # This CLI owns the lock until process exit, including rollback/error paths.
    atexit.register(os.close, fd)


def run(arguments, timeout=40):
    return subprocess.run(arguments, check=True, text=True, capture_output=True, timeout=timeout)


def plain_file(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Expected a regular file: {path}")


def managed_directory(path, mode):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Expected a managed directory: {path}")
    path.mkdir(mode=mode, exist_ok=True)
    if path.stat().st_uid != 0:
        raise ValueError(f"Managed directory is not owned by root: {path}")
    path.chmod(mode)


def atomic_write(path, content, mode=0o600):
    plain_file(path)
    temporary = path.with_name(path.name + ".new")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f"Unexpected temporary file: {temporary}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_bundle(source):
    plain_file(source / "MANIFEST.json")
    if (source / "MANIFEST.json").stat().st_size > 1024 * 1024:
        raise ValueError("Bundle manifest is too large")
    expected = json.loads((source / "MANIFEST.json").read_text(encoding="utf-8"))
    if expected.get("product") != "sonic-web-manager":
        raise ValueError("Bundle product mismatch")
    files = expected.get("files", {})
    required = {"server/app.py", "server/auth.py", "server/sonic.py", "deploy/launch.py", "ui/dist/index.html", "shared/catalog.json"}
    version = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[-+])", str(expected.get("version", "")))
    # Keep older verified releases installable for rollback.
    if version and tuple(map(int, version.groups())) >= (0, 4, 0):
        required.update({"server/metrics.py", "server/console.py", "deploy/console_child.py"})
    if not isinstance(files, dict) or len(files) > 5000 or not required.issubset(files):
        raise ValueError("Incomplete bundle")
    for name, digest in files.items():
        if not isinstance(name, str):
            raise ValueError("Unsafe bundle path")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or relative.as_posix() != name
                or "\\" in name or ":" in name or any(part in ("", ".", "..") for part in relative.parts)
                or name == "MANIFEST.json"):
            raise ValueError("Unsafe bundle path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid bundle digest")
        path = source / name
        if any((source / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)) or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Unsafe bundle path: {name}")
        if path.is_file() and path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError(f"Bundle file is too large: {name}")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Bundle integrity failure: {name}")
    return expected


def address():
    data = json.loads(run(["ip", "-j", "-4", "address", "show", "dev", "eth0"]).stdout)
    candidates = [item["local"] for device in data for item in device.get("addr_info", [])
                  if item.get("scope") == "global"]
    for candidate in candidates:
        parsed = ipaddress.ip_address(candidate)
        if parsed.version == 4 and not (parsed.is_loopback or parsed.is_global or parsed.is_unspecified or parsed.is_multicast):
            return str(parsed)
    raise RuntimeError("Cannot bind: eth0 has no usable IPv4 address")


def safe_release(path):
    if (PREFIX / "releases").is_symlink():
        raise ValueError("Managed releases directory must not be a symlink")
    resolved = path.resolve()
    if resolved.parent != (PREFIX / "releases").resolve() or not resolved.is_dir():
        raise ValueError("Release target is outside managed release directory")
    validate_bundle(resolved)
    return resolved


def swap_link(name, target):
    temporary = PREFIX / (name + ".new")
    if temporary.is_symlink():
        temporary.unlink()
    elif temporary.exists():
        raise ValueError("Unexpected temporary link path")
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(PREFIX / name)


def health_check(bind_address, port, certificate):
    context = ssl.create_default_context(cafile=str(certificate))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    for attempt in range(10):
        try:
            with opener.open(f"https://{bind_address}:{port}/api/health", timeout=3) as response:
                result = json.load(response)
            if result.get("mode") != "live" or result.get("status") != "ok":
                raise ValueError("Unexpected service health response")
            return
        except (OSError, ValueError):
            if attempt == 9:
                raise
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run as root on the target SONiC switch")
    if not Path("/etc/sonic/sonic_version.yml").is_file():
        raise SystemExit("Target is not identified as SONiC")
    if PREFIX.is_symlink():
        raise SystemExit("Managed prefix must not be a symlink")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Choose an unprivileged TCP port between 1024 and 65535")
    os.umask(0o077)
    managed_directory(PREFIX, 0o755)
    managed_directory(PREFIX / "releases", 0o755)
    managed_directory(PREFIX / "state", 0o700)
    managed_directory(PREFIX / "state" / "tls", 0o700)
    managed_directory(PREFIX / "state" / "data", 0o700)
    plain_file(UNIT)
    plain_file(VERIFY_UNIT)
    lock_breakout_state()
    ensure_no_breakout_in_progress()
    if args.rollback:
        previous = PREFIX / "previous"
        if not previous.is_symlink():
            raise SystemExit("No previous Web Manager release exists")
        target = safe_release(previous)
        old = safe_release(PREFIX / "current")
        swap_link("current", target)
        swap_link("previous", old)
        run(["systemctl", "restart", "sonic-web-manager.service"], timeout=150)
        time.sleep(2)
        run(["systemctl", "is-active", "--quiet", "sonic-web-manager.service"])
        config_file = PREFIX / "state" / "service.json"
        plain_file(config_file)
        port = json.loads(config_file.read_text())["port"]
        health_check(address(), port, PREFIX / "state" / "tls" / "cert.pem")
        print("Rolled back the Web Manager service only; SONiC image/config unchanged")
        return

    source = args.source.resolve()
    manifest = validate_bundle(source)
    bind_address = address()
    current = PREFIX / "current"
    had_current = current.is_symlink()
    if current.exists() and not had_current:
        raise SystemExit("Unexpected non-symlink current release path")
    config_file = PREFIX / "state" / "service.json"
    plain_file(config_file)
    previous_config = config_file.read_bytes() if config_file.exists() else None
    previous_unit = UNIT.read_bytes() if UNIT.exists() else None
    previous_verify_unit = VERIFY_UNIT.read_bytes() if VERIFY_UNIT.exists() else None
    old_port = json.loads(previous_config).get("port") if previous_config else None
    service_running = subprocess.run(["systemctl", "is-active", "--quiet", "sonic-web-manager.service"], timeout=10).returncode == 0
    # Only the current active manager may already occupy the same port. A changed
    # port or a stopped service must pass a real bind test before any replacement.
    if not (had_current and service_running and old_port == args.port):
        with socket.socket() as probe:
            probe.bind((bind_address, args.port))
    state = PREFIX / "state"
    tls = state / "tls"
    certificate = tls / "cert.pem"
    key = tls / "key.pem"
    plain_file(certificate)
    plain_file(key)
    if certificate.exists() != key.exists():
        raise SystemExit("Incomplete TLS identity; refusing to silently replace it")
    if not certificate.exists():
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
             "-days", "825", "-subj", "/CN=SONiC Web Manager",
             "-addext", f"subjectAltName=IP:{bind_address},DNS:sonic",
             "-keyout", str(key), "-out", str(certificate)])
    key.chmod(0o600)
    certificate.chmod(0o644)
    # Preserve the existing identity; fail before replacing the release if it
    # no longer matches the management IP or its private key.
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(certificate, key)
    run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkip", bind_address])
    run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0"])
    release_id = hashlib.sha256((source / "MANIFEST.json").read_bytes()).hexdigest()[:12]
    version = str(manifest.get("version", "unknown"))
    if not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for c in version):
        raise ValueError("Unsafe version identifier")
    release = PREFIX / "releases" / (version + "-" + release_id)
    if release.is_symlink():
        raise ValueError("Release directory must not be a symlink")
    if not release.exists():
        release.mkdir(parents=True, mode=0o755)
        for name in manifest["files"]:
            destination = release / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copy2(source / name, destination)
            destination.chmod(0o644)
        shutil.copy2(source / "MANIFEST.json", release / "MANIFEST.json")
    safe_release(release)
    old = safe_release(current) if had_current else None
    unit = """[Unit]
Description=SONiC Web Manager
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/host/sonic-web/current
ExecStart=/usr/bin/python3 /host/sonic-web/current/deploy/launch.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=180
KillMode=mixed
UMask=0077
Nice=10
CPUQuota=50%
MemoryMax=384M
TasksMax=64
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
"""
    verify_unit = """[Unit]
Description=Verify SONiC Web port mode after cold boot
After=network-online.target docker.service swss.service
Wants=network-online.target
ConditionPathExists=/host/sonic-web/current/deploy/breakout_worker.py

[Service]
Type=oneshot
User=root
WorkingDirectory=/host/sonic-web/current
ExecStart=/usr/bin/python3 /host/sonic-web/current/deploy/breakout_worker.py --verify-pending
Environment=PYTHONDONTWRITEBYTECODE=1
TimeoutStartSec=600
UMask=0077
Nice=10
CPUQuota=50%
MemoryMax=384M
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""
    try:
        atomic_write(config_file, json.dumps({"port": args.port}, indent=2) + "\n")
        atomic_write(UNIT, unit, 0o644)
        atomic_write(VERIFY_UNIT, verify_unit, 0o644)
        if old and old != release:
            swap_link("previous", old)
        swap_link("current", release)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "sonic-web-manager.service"])
        run(["systemctl", "enable", "sonic-web-breakout-verify.service"])
        run(["systemctl", "restart", "sonic-web-manager.service"], timeout=150)
        time.sleep(2)
        run(["systemctl", "is-active", "--quiet", "sonic-web-manager.service"])
        health_check(bind_address, args.port, certificate)
    except (subprocess.SubprocessError, OSError, ValueError):
        if previous_config is not None:
            atomic_write(config_file, previous_config)
        if previous_unit is not None:
            atomic_write(UNIT, previous_unit, 0o644)
        if previous_verify_unit is not None:
            atomic_write(VERIFY_UNIT, previous_verify_unit, 0o644)
        elif VERIFY_UNIT.is_file() and not VERIFY_UNIT.is_symlink():
            run(["systemctl", "disable", "sonic-web-breakout-verify.service"])
            VERIFY_UNIT.unlink()
        run(["systemctl", "daemon-reload"])
        if old:
            swap_link("current", old)
            run(["systemctl", "restart", "sonic-web-manager.service"], timeout=150)
        else:
            # First installation has no old runtime to recover. Preserve its
            # evidence and TLS identity but do not leave a failed service enabled.
            run(["systemctl", "disable", "--now", "sonic-web-manager.service"], timeout=150)
        raise
    print(json.dumps({"service": "sonic-web-manager.service", "version": version,
                      "url": f"https://{bind_address}:{args.port}",
                      "release": str(release), "previous": str(old) if old else None,
                      "note": "Self-signed local TLS certificate; existing SONiC accounts use PAM. No switch configuration changed."}, indent=2))


if __name__ == "__main__":
    main()
