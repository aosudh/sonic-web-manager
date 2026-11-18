"""Isolated PTY/core and helper tests; never run a login shell or contact BMC."""
import base64
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from server import console
from server.console import ConsoleError, ConsoleManager


def b64(data):
    return base64.b64encode(data).decode()


def target_config():
    algorithm = b"ssh-ed25519"
    key = len(algorithm).to_bytes(4, "big") + algorithm + (32).to_bytes(4, "big") + b"x" * 32
    return {"verified": True, "host": "192.0.2.8", "port": 22, "username": "root", "host_key": "ssh-ed25519 " + b64(key)}


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.manager = ConsoleManager(demo=True, clock=lambda: self.clock[0])
        self.addCleanup(self.manager.close_all)

    def create(self, owner="owner-a", target="sonic"):
        return self.manager.create(owner, "admin", target)["id"]

    def error(self, code, callback):
        with self.assertRaises(ConsoleError) as caught:
            callback()
        self.assertEqual(code, caught.exception.status)

    def test_demo_is_explicit_and_never_forks_or_contacts_host(self):
        for value in ("true", "false", 1, None):
            with self.assertRaises(TypeError):
                ConsoleManager(demo=value)
        with patch("server.console.subprocess.Popen", side_effect=AssertionError("host fork")):
            for target in ("sonic", "bmc"):
                identifier = self.create(target=target)
                self.assertIn(b"DEMO", base64.b64decode(self.manager.read("owner-a", identifier)["data"]))
                self.manager.write("owner-a", identifier, b64(b"echo simulated\r"))

    def test_owner_bound_read_write_resize_and_close_never_leak_other_session(self):
        identifier = self.create()
        for callback in (lambda: self.manager.read("owner-b", identifier),
                         lambda: self.manager.write("owner-b", identifier, b64(b"x")),
                         lambda: self.manager.resize("owner-b", identifier, 30, 90),
                         lambda: self.manager.close("owner-b", identifier)):
            self.error(404, callback)
        self.assertFalse(self.manager.read("owner-a", identifier)["closed"])

    def test_global_and_owner_limits(self):
        first = self.create()
        self.create()
        self.error(429, self.create)
        self.create("owner-b")
        self.create("owner-b")
        self.error(429, lambda: self.create("owner-c"))
        self.manager.close("owner-a", first)
        self.create("owner-c")

    def test_byte_ring_and_cursor_detect_lost_output_without_decoding(self):
        identifier = self.create()
        session = self.manager._sessions[identifier]
        payload = ("中文".encode() + b"\x00\xff") * 70000
        self.manager._append(session, payload)
        result = self.manager.read("owner-a", identifier, 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.manager.RING_BYTES, len(session.buffer))
        self.assertEqual(self.manager.READ_BYTES, len(base64.b64decode(result["data"])))
        self.assertEqual(bytes(session.buffer[:self.manager.READ_BYTES]), base64.b64decode(result["data"]))
        self.assertEqual(result["cursor"] + self.manager.READ_BYTES, result["next_cursor"])
        self.error(400, lambda: self.manager.read("owner-a", identifier, session.end + 1))

    def test_input_length_base64_and_rate_are_bounded(self):
        identifier = self.create()
        self.error(400, lambda: self.manager.write("owner-a", identifier, "not base64!"))
        self.error(413, lambda: self.manager.write("owner-a", identifier, b64(b"x" * 4097)))
        for _ in range(4):
            self.assertEqual(4096, self.manager.write("owner-a", identifier, b64(b"x" * 4096))["accepted_bytes"])
        self.error(429, lambda: self.manager.write("owner-a", identifier, b64(b"x")))
        self.clock[0] += 1
        self.manager.write("owner-a", identifier, b64(b"x"))

    def test_poll_does_not_extend_idle_but_input_and_changed_size_do(self):
        identifier = self.create()
        self.clock[0] += 290
        self.manager.read("owner-a", identifier)
        self.manager.resize("owner-a", identifier, 24, 80)  # Unchanged polling size.
        self.clock[0] += 11
        result = self.manager.read("owner-a", identifier)
        self.assertTrue(result["closed"])
        self.assertEqual("idle_timeout", result["reason"])
        identifier = self.create()
        self.clock[0] += 290
        self.manager.write("owner-a", identifier, b64(b"x"))
        self.clock[0] += 290
        self.manager.resize("owner-a", identifier, 30, 90)
        self.clock[0] += 290
        self.assertFalse(self.manager.read("owner-a", identifier)["closed"])

    def test_maximum_lifetime_cannot_be_extended(self):
        identifier = self.create()
        for _ in range(5):
            self.clock[0] += 290
            self.manager.write("owner-a", identifier, b64(b"x"))
        self.clock[0] = 1900
        result = self.manager.read("owner-a", identifier)
        self.assertTrue(result["closed"])
        self.assertEqual("max_duration", result["reason"])

    def test_resize_and_target_validation(self):
        self.error(400, lambda: self.create(target="ssh 192.0.2.99"))
        identifier = self.create()
        for rows, cols in ((True, 80), (1, 80), (201, 80), (24, 401), (24, 9), ("24", 80)):
            self.error(400, lambda: self.manager.resize("owner-a", identifier, rows, cols))

    def test_logout_expiration_and_service_close_discard_output(self):
        identifier = self.create()
        self.manager.close_owner("owner-a")
        self.error(404, lambda: self.manager.read("owner-a", identifier))
        identifier = self.create()
        self.manager.close_all()
        self.assertFalse(self.manager.capabilities("admin")["sonic"]["available"])
        self.assertEqual({}, self.manager._sessions)

    def test_revoked_owner_is_rejected_and_cleaned_without_refreshing_session(self):
        alive = {"owner": True}
        manager = ConsoleManager(demo=True, owner_alive=lambda owner: alive.get(owner, False))
        self.addCleanup(manager.close_all)
        identifier = manager.create("owner", "admin", "sonic")["id"]
        alive["owner"] = False
        self.error(401, lambda: manager.read("owner", identifier))
        self.assertNotIn(identifier, manager._sessions)

    def test_close_is_idempotent_and_closed_records_are_bounded(self):
        identifier = self.create()
        self.assertTrue(self.manager.close("owner-a", identifier)["closed"])
        self.assertTrue(self.manager.close("owner-a", identifier)["closed"])
        for _ in range(15):
            identifier = self.create()
            self.manager.close("owner-a", identifier)
        self.assertLessEqual(len(self.manager._sessions), 9)

    def load_target(self, config):
        with patch("server.console._root_file", return_value=json.dumps(config).encode()):
            return console._load_bmc_target()

    def test_bmc_config_requires_verified_ip_pinned_key_and_no_password_or_arbitrary_options(self):
        self.assertEqual("192.0.2.8", self.load_target(target_config())["host"])
        for changes in ({"verified": False}, {"host": "example.org"}, {"host": "127.0.0.1"},
                        {"host": "fe80::1%eth0"}, {"port": 2222}, {"username": "-oProxyCommand=x"},
                        {"host_key": "ssh-ed25519 invalid"}, {"password": "secret"}, {"argv": ["sh"]}):
            config = {**target_config(), **changes}
            self.error(503, lambda: self.load_target(config))

    def test_ssh_argv_uses_pinned_host_and_disables_local_escape_and_forwarding(self):
        target = self.load_target(target_config())
        argv = console._ssh_argv(target, "/run/sonic-web-console/pinned.known_hosts")
        self.assertEqual(["/usr/bin/ssh", "-F", "/dev/null", "-tt", "-p", "22"], argv[:6])
        self.assertEqual(["-l", "root", "--", "192.0.2.8"], argv[-4:])
        for option in ("StrictHostKeyChecking=yes", "EscapeChar=none", "ClearAllForwardings=yes", "PermitLocalCommand=no", "PubkeyAuthentication=no", "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(option, argv)
        self.assertFalse(any("password=" in part.lower() for part in argv))

    def test_legacy_algorithms_require_separate_root_verification(self):
        data = target_config()
        data["compatibility"] = {"kex_algorithms": ["diffie-hellman-group14-sha1"], "host_key_algorithms": ["ssh-rsa"]}
        self.error(503, lambda: self.load_target(data))
        data["compatibility_verified"] = True
        argv = console._ssh_argv(self.load_target(data), "/run/pinned")
        self.assertIn("KexAlgorithms=+diffie-hellman-group14-sha1", argv)
        data["compatibility"]["kex_algorithms"] = ["none,ProxyCommand=evil"]
        self.error(503, lambda: self.load_target(data))

    @unittest.skipUnless(os.name == "posix", "POSIX PTY test")
    def test_root_or_system_local_user_is_never_accepted(self):
        import pwd
        for uid, shell in ((0, "/bin/bash"), (999, "/bin/bash"), (65534, "/bin/bash"), (1000, "/usr/sbin/nologin")):
            user = types.SimpleNamespace(pw_name="admin", pw_uid=uid, pw_gid=1000, pw_shell=shell, pw_dir="/home/admin")
            with patch.object(pwd, "getpwnam", return_value=user), patch("server.console.os.access", return_value=True):
                self.error(403, lambda: console._validate_local_user("admin"))

    @unittest.skipUnless(os.name == "posix", "POSIX file permission test")
    def test_config_symlink_or_world_readable_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bmc.json"
            path.write_text(json.dumps(target_config()))
            path.chmod(0o644)
            self.error(503, lambda: console._root_file(path))
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            self.error(503, lambda: console._root_file(link))

    @unittest.skipUnless(os.name == "posix", "POSIX PTY core")
    def test_real_pty_io_resize_and_close_with_harmless_echo_child(self):
        manager = ConsoleManager()
        self.addCleanup(manager.close_all)
        user = types.SimpleNamespace(pw_name="admin", pw_uid=1000, pw_gid=1000, pw_shell="/bin/bash", pw_dir="/home/admin")
        real_popen, calls = subprocess.Popen, []
        code = "import os,signal; signal.signal(signal.SIGWINCH,lambda *a:os.write(1,b'RESIZED\\n')); os.write(1,b'READY\\n');\nwhile True:\n d=os.read(0,4096)\n if not d: break\n os.write(1,b'ECHO:'+d)\n"
        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            return real_popen([sys.executable, "-c", code], **kwargs)
        with patch.object(manager, "_live_user", return_value=user), patch("server.console.subprocess.Popen", side_effect=spawn), patch.object(manager, "_stop_unit", return_value=True), patch.object(manager, "_await_started", side_effect=lambda s: setattr(s, "launch_confirmed", True)):
            identifier = manager.create("owner", "admin", "sonic")["id"]
            def wait_for(text):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    output = base64.b64decode(manager.read("owner", identifier)["data"])
                    if text in output:
                        return
                    time.sleep(0.02)
                self.fail("PTY output missing: " + repr(text))
            wait_for(b"READY")
            manager.write("owner", identifier, b64(b"hello\n"))
            wait_for(b"ECHO:hello")
            manager.resize("owner", identifier, 31, 100)
            wait_for(b"RESIZED")
            manager.close("owner", identifier)
            self.assertIsNotNone(manager._sessions[identifier].process.poll())
        argv, kwargs = calls[0]
        self.assertEqual("/usr/bin/systemd-run", argv[0])
        self.assertIn("--property=KillMode=control-group", argv)
        self.assertIn("--property=NoNewPrivileges=no", argv)
        self.assertIn("--property=BindsTo=sonic-web-manager.service", argv)
        self.assertIn("--property=After=sonic-web-manager.service", argv)
        self.assertIn("--property=MemoryMax=128M", argv)
        self.assertIn("--property=TasksMax=64", argv)
        self.assertIn("--property=CPUQuota=50%", argv)
        self.assertEqual(["--user", "admin", "--target", "sonic"], argv[-4:])
        self.assertNotIn("preexec_fn", kwargs)
        self.assertIs(kwargs["shell"], False)

    @unittest.skipUnless(os.name == "posix", "POSIX helper")
    def test_helper_drops_all_ids_before_exec_login_shell_and_clears_history_environment(self):
        helper = importlib.import_module("deploy.console_child")
        account = types.SimpleNamespace(pw_name="admin", pw_uid=1000, pw_gid=1000, pw_dir="/home/admin", pw_shell="/bin/bash")
        identity = {"uid": 0, "gid": 0}
        calls = []
        def uid(value):
            calls.append("setuid")
            identity["uid"] = value
        def gid(value):
            calls.append("setgid")
            identity["gid"] = value
        def execute(path, argv, env):
            calls.append("exec")
            self.assertEqual({"uid": 1000, "gid": 1000}, identity)
            self.assertEqual("/bin/bash", path)
            self.assertEqual(["-bash"], argv)
            self.assertEqual("/dev/null", env["HISTFILE"])
            self.assertEqual("0", env["HISTSIZE"])
            self.assertEqual("*", env["HISTIGNORE"])
            self.assertIn("set +o history", env["PROMPT_COMMAND"])
            self.assertNotIn("LD_PRELOAD", env)
        with patch.object(helper, "_validate_local_user", return_value=account), patch.object(helper.os, "geteuid", side_effect=lambda: identity["uid"]), patch.object(helper.os, "getuid", side_effect=lambda: identity["uid"]), patch.object(helper.os, "getgid", side_effect=lambda: identity["gid"]), patch.object(helper.os, "isatty", return_value=True), patch.object(helper.os, "getsid", return_value=-1), patch.object(helper.os, "setsid", side_effect=lambda: calls.append("setsid")), patch.object(helper.fcntl, "ioctl", side_effect=lambda *a: calls.append("ctty")), patch.object(helper.os, "initgroups", side_effect=lambda *a: calls.append("initgroups")), patch.object(helper.os, "setgid", side_effect=gid), patch.object(helper.os, "setuid", side_effect=uid), patch.object(helper.os, "chdir"), patch.object(helper.os, "execve", side_effect=execute):
            helper.main(["--user", "admin", "--target", "sonic"])
        self.assertEqual(["setsid", "ctty", "initgroups", "setgid", "setuid", "exec"], calls)

    def test_stop_unit_never_accepts_arbitrary_service_name(self):
        with patch("server.console.subprocess.run", side_effect=AssertionError("host command")):
            self.assertFalse(ConsoleManager._stop_unit("ssh.service"))
            self.assertFalse(ConsoleManager._stop_unit("sonic-web-console-x.service; reboot"))

    def test_systemd_availability_is_probed_and_failure_cached(self):
        manager = ConsoleManager(clock=lambda: self.clock[0])
        self.addCleanup(manager.close_all)
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="not available")
        success = subprocess.CompletedProcess([], 0, stdout="249", stderr="")
        with patch("server.console.subprocess.run", side_effect=[failure, success, success]) as run:
            self.assertFalse(manager._systemd_ready())
            self.assertFalse(manager._systemd_ready())
            self.assertEqual(1, run.call_count)
            self.clock[0] += 31
            self.assertTrue(manager._systemd_ready())
            self.assertEqual(3, run.call_count)
            self.assertEqual(["/usr/bin/systemctl", "is-active", "--quiet", "sonic-web-manager.service"], run.call_args.args[0])

    def test_unconfigured_bmc_does_not_disable_sonic_or_spawn_anything(self):
        manager = ConsoleManager()
        self.addCleanup(manager.close_all)
        with patch.object(manager, "_live_user"), patch("server.console._load_bmc_target", side_effect=ConsoleError("BMC 未验证", 503)), patch("server.console.subprocess.Popen", side_effect=AssertionError("host fork")):
            capabilities = manager.capabilities("admin")
            self.assertTrue(capabilities["sonic"]["available"])
            self.assertEqual("admin", capabilities["sonic"]["identity"])
            self.assertFalse(capabilities["bmc"]["available"])
            self.error(503, lambda: manager.create("owner", "admin", "bmc"))

    def test_unconfirmed_cgroup_cleanup_still_consumes_session_capacity(self):
        with patch.object(self.manager, "_stop_unit", return_value=False):
            for owner in ("owner-a", "owner-a", "owner-b", "owner-b"):
                identifier = self.create(owner)
                session = self.manager._sessions[identifier]
                session.closed, session.cleanup_confirmed, session.closed_at = True, False, self.clock[0]
            self.error(429, lambda: self.create("owner-c"))

    def test_stale_select_cannot_read_or_write_reused_descriptor(self):
        for closed, replacement in ((True, None), (False, 43)):
            session = console._Session("owner-a", "admin", "sonic", 24, 80, self.clock[0], False)
            session.fd = 42
            session.pending.extend(b"old owner input")
            def stale_wakeup(*args):
                # Another thread closed the old PTY while select was waiting;
                # fd 42 may now refer to owner B's newly created terminal.
                with session.lock:
                    session.closed, session.fd = closed, replacement
                return [42], [42], []
            with patch("server.console.select.select", side_effect=stale_wakeup), patch("server.console.os.read", return_value=b"owner B private output") as read, patch("server.console.os.write") as write, patch.object(self.manager, "_finish"):
                self.manager._pump(session)
            read.assert_not_called()
            write.assert_not_called()
            self.assertEqual(b"", session.buffer)

    def test_spawn_handshake_waits_until_unit_is_registered(self):
        session = console._Session("owner", "admin", "sonic", 24, 80, self.clock[0], False)
        session.process = types.SimpleNamespace(poll=lambda: None)
        missing = subprocess.CompletedProcess([], 0, stdout="LoadState=not-found\nActiveState=inactive\n")
        loaded = subprocess.CompletedProcess([], 0, stdout="LoadState=loaded\nActiveState=activating\n")
        with patch("server.console.subprocess.run", side_effect=[missing, loaded]) as run:
            self.manager._await_started(session)
        self.assertTrue(session.launch_confirmed)
        self.assertEqual(2, run.call_count)

    def test_launcher_is_reaped_before_stopping_late_registered_unit(self):
        session = console._Session("owner", "admin", "sonic", 24, 80, self.clock[0], False)
        session.launch_confirmed = True
        state, calls = {"exited": False, "registered": False}, []
        def wait(timeout):
            calls.append("wait")
            state.update(exited=True, registered=True)  # Last pending start request.
            return 0
        session.process = types.SimpleNamespace(pid=999999, poll=lambda: 0 if state["exited"] else None, wait=wait, kill=lambda: calls.append("kill"))
        def stop(unit, **kwargs):
            calls.append("stop-unit")
            self.assertTrue(state["exited"])
            self.assertTrue(state["registered"])
            return True
        with patch("server.console.os.killpg", side_effect=lambda *a: calls.append("kill-launcher")), patch.object(self.manager, "_stop_unit", side_effect=stop):
            self.manager._finish(session, "closed")
        self.assertEqual(["kill-launcher", "wait", "stop-unit"], calls)
        self.assertTrue(session.cleanup_confirmed)

    def test_unconfirmed_launch_not_found_is_not_cleanup_confirmation(self):
        missing = subprocess.CompletedProcess([], 5)
        unit = "sonic-web-console-" + "a" * 32 + ".service"
        with patch("server.console.subprocess.run", return_value=missing) as run:
            self.assertFalse(ConsoleManager._stop_unit(unit, allow_not_found=False))
        self.assertEqual(1, run.call_count)


if __name__ == "__main__":
    unittest.main()
