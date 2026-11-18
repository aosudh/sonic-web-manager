"""Local Linux /bin/sh tests; substitutes paths, never accesses BMC/network."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("sonic-web-usb.sh")
GOOD = """usb0      Link encap:Ethernet  HWaddr 02:00:00:00:00:02
          inet addr:169.254.100.1  Bcast:169.254.100.3  Mask:255.255.255.252
          UP BROADCAST RUNNING  MTU:1500  Metric:1
"""
DOWN = "usb0      Link encap:Ethernet  HWaddr 02:00:00:00:00:02\n          BROADCAST  MTU:1500\n"
LISTEN = "tcp 0 0 169.254.100.1:22 0.0.0.0:* LISTEN\n"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.device = self.base / "usb0"
        self.device.mkdir()
        self.state = self.base / "state"
        self.state.write_text(GOOD)
        (self.base / "good").write_text(GOOD)
        self.calls = self.base / "calls"
        self.logs = self.base / "logs"
        self.ssh_calls = self.base / "ssh-calls"
        self.listeners = self.base / "listeners"
        self.listeners.write_text(LISTEN)
        (self.base / "good-listeners").write_text(LISTEN)
        self.effective = self.base / "effective"
        self.effective.write_text("port 22\nlistenaddress 169.254.100.1:22\n")
        self.config = self.base / "ssh_server_config"
        self.config.write_text("ListenAddress 169.254.100.1\n")
        self.config_link = self.base / "sshd_config_link"
        self.config_link.symlink_to(self.config)
        self.old_config = self.base / "sshd_config_old"
        self.defaults = self.base / "ssh-defaults"
        self.defaults.write_text("# vendor defaults\nSSHD_OPTS=\n")
        self.service_config = self.base / "ncml.conf"
        self.service_config.write_text("[ssh]\ncurrent_state=1\nsecure_port=22\nnonscecure_port=4294967295\n")
        for name, text in {
            "ifconfig": """#!/bin/sh
if [ "$#" = 1 ] && [ "$1" = usb0 ]; then
    [ -e "$FIXTURE/read-fail" ] && exit 1
    cat "$FIXTURE/state"
    exit 0
fi
printf '%s\\n' "$*" >> "$FIXTURE/calls"
[ "$*" = 'usb0 169.254.100.1 netmask 255.255.255.252 up' ] || exit 98
[ -e "$FIXTURE/set-fail" ] && exit 1
[ -e "$FIXTURE/no-change" ] || cp "$FIXTURE/good" "$FIXTURE/state"
exit 0
""",
            "logger": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE/logs"\n',
            "netstat": """#!/bin/sh
[ "$*" = '-lnt' ] || exit 98
[ -e "$FIXTURE/netstat-fail" ] && exit 1
cat "$FIXTURE/listeners"
""",
            "sshd": """#!/bin/sh
printf '%s\\n' "$*" >> "$FIXTURE/ssh-calls"
[ "$#" = 3 ] && [ "$2" = '-f' ] && [ "$3" = "$FIXTURE/ssh_server_config" ] || exit 98
case "$1" in
  -t) [ ! -e "$FIXTURE/sshd-test-fail" ] ;;
  -T) [ ! -e "$FIXTURE/sshd-effective-fail" ] || exit 1; cat "$FIXTURE/effective" ;;
  *) exit 98 ;;
esac
""",
            "ssh-init": """#!/bin/sh
printf 'init %s\\n' "$*" >> "$FIXTURE/ssh-calls"
[ "$*" = start ] || exit 98
[ -e "$FIXTURE/ssh-start-fail" ] && exit 1
[ -e "$FIXTURE/ssh-no-listen" ] || cp "$FIXTURE/good-listeners" "$FIXTURE/listeners"
exit 0
""",
        }.items():
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)
        source = SOURCE.read_text()
        path_line = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        self.assertEqual(source.count(path_line), 1)
        self.assertEqual(source.count("[ -d /sys/class/net/usb0 ]"), 1)
        source = source.replace(path_line, "PATH='" + str(self.bin) + ":/usr/bin:/bin'")
        source = source.replace("[ -d /sys/class/net/usb0 ]", "[ -d '" + str(self.device) + "' ]")
        self.assertEqual(source.count("/usr/sbin/sshd"), 2)
        self.assertEqual(source.count("/etc/init.d/ssh start"), 1)
        source = source.replace("/usr/sbin/sshd", str(self.bin / "sshd"))
        source = source.replace("/etc/init.d/ssh start", str(self.bin / "ssh-init") + " start")
        for declaration, target in {
            "SSH_CONFIG=/conf/ssh_server_config": self.config,
            "SSH_CONFIG_LINK=/etc/ssh/sshd_config": self.config_link,
            "SSH_OLD_CONFIG=/conf/sshd_config": self.old_config,
            "SSH_DEFAULTS=/etc/default/ssh": self.defaults,
            "SSH_SERVICE_CONFIG=/conf/ncml.conf": self.service_config,
        }.items():
            self.assertEqual(source.count(declaration + "\n"), 1)
            source = source.replace(declaration + "\n", declaration.split("=", 1)[0] + "='" + str(target) + "'\n")
        self.script = self.base / "guard.sh"
        self.script.write_text(source)

    def run_guard(self):
        return subprocess.run(["/bin/sh", str(self.script)], capture_output=True, text=True,
                              env={**os.environ, "FIXTURE": str(self.base), "BMC_USB_ADAPTER_REVIEWED": "1"}, timeout=3)

    def assert_no_set(self):
        self.assertFalse(self.calls.exists())

    def test_public_example_requires_explicit_review_before_any_operation(self):
        result = subprocess.run(["/bin/sh", str(self.script)], capture_output=True, text=True,
                                env={**os.environ, "FIXTURE": str(self.base), "BMC_USB_ADAPTER_REVIEWED": "0"}, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Example adapter disabled", result.stderr)
        self.assert_no_set()
        self.assertFalse(self.ssh_calls.exists())

    def test_correct_state_is_noop(self):
        self.assertEqual(self.run_guard().returncode, 0)
        self.assert_no_set()
        self.assertFalse(self.logs.exists())
        self.assertFalse(self.ssh_calls.exists())

    def test_missing_interface_is_noop(self):
        self.device.rmdir()
        self.assertEqual(self.run_guard().returncode, 0)
        self.assert_no_set()

    def test_missing_address_restored_once_then_noop(self):
        self.state.write_text(DOWN)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(),
                         ["usb0 169.254.100.1 netmask 255.255.255.252 up"])

    def test_down_with_own_address_restored(self):
        self.state.write_text(GOOD.replace("UP BROADCAST RUNNING", "BROADCAST"))
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.state.read_text(), GOOD)

    def test_wrong_mask_on_owned_address_corrected(self):
        self.state.write_text(GOOD.replace("255.255.255.252", "255.255.0.0"))
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.state.read_text(), GOOD)

    def test_unrelated_address_conflicts(self):
        self.state.write_text(GOOD.replace("169.254.100.1", "192.0.2.1"))
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assert_no_set()

    def test_unexpected_ipv4_output_fails_closed(self):
        self.state.write_text("usb0: flags=UP\n inet 192.0.2.1 netmask 255.255.255.0\n")
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assert_no_set()

    def test_multiple_ipv4_addresses_conflict(self):
        self.state.write_text(GOOD + " inet addr:192.0.2.1 Mask:255.255.255.0\n")
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assert_no_set()

    def test_read_failure_does_not_write(self):
        (self.base / "read-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assert_no_set()

    def test_set_failure_is_error(self):
        self.state.write_text(DOWN)
        (self.base / "set-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)

    def test_exit_zero_without_state_change_is_error(self):
        self.state.write_text(DOWN)
        (self.base / "no-change").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertIn("readback", self.logs.read_text())

    def test_missing_ssh_started_after_validation_and_second_run_noop(self):
        self.listeners.write_text("")
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.run_guard().returncode, 0)
        self.assert_no_set()
        self.assertEqual(self.ssh_calls.read_text().splitlines(),
                         ["-t -f " + str(self.config), "-T -f " + str(self.config), "init start"])

    def test_late_usb_recovers_address_then_ssh(self):
        self.state.write_text(DOWN)
        self.listeners.write_text("")
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertEqual(self.state.read_text(), GOOD)
        self.assertEqual(self.listeners.read_text(), LISTEN)

    def test_any_other_port22_listener_conflicts_without_service_write(self):
        for lines in ("tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN\n",
                      "tcp 0 0 :::22 :::* LISTEN\n",
                      LISTEN + "tcp 0 0 127.0.0.1:22 0.0.0.0:* LISTEN\n"):
            with self.subTest(lines=lines):
                self.listeners.write_text(lines)
                self.assertNotEqual(self.run_guard().returncode, 0)
                self.assertFalse(self.ssh_calls.exists())

    def test_unrelated_ports_do_not_mask_missing_ssh(self):
        self.listeners.write_text("tcp 0 0 0.0.0.0:2222 0.0.0.0:* LISTEN\n")
        self.assertEqual(self.run_guard().returncode, 0)
        self.assertIn("init start", self.ssh_calls.read_text())

    def test_netstat_failure_does_not_start_service(self):
        (self.base / "netstat-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertFalse(self.ssh_calls.exists())

    def test_sshd_syntax_failure_does_not_start(self):
        self.listeners.write_text("")
        (self.base / "sshd-test-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertNotIn("init start", self.ssh_calls.read_text())

    def test_sshd_effective_failure_does_not_start(self):
        self.listeners.write_text("")
        (self.base / "sshd-effective-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertNotIn("init start", self.ssh_calls.read_text())

    def test_effective_nonprivate_or_multiple_bindings_refused(self):
        self.listeners.write_text("")
        for binding in ("listenaddress 0.0.0.0:22\n", "listenaddress [::]:22\n",
                        "listenaddress 169.254.100.1:22\nlistenaddress 127.0.0.1:22\n", ""):
            with self.subTest(binding=binding):
                self.effective.write_text(binding)
                self.assertNotEqual(self.run_guard().returncode, 0)
                self.assertNotIn("init start", self.ssh_calls.read_text())

    def test_existing_service_start_failure_is_error(self):
        self.listeners.write_text("")
        (self.base / "ssh-start-fail").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)

    def test_service_success_without_listener_is_error(self):
        self.listeners.write_text("")
        (self.base / "ssh-no-listen").touch()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertIn("SSH readback", self.logs.read_text())

    def test_vendor_migration_trigger_refused(self):
        self.listeners.write_text("")
        self.old_config.symlink_to(self.config)
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertFalse(self.ssh_calls.exists())

    def test_missing_or_symlinked_service_config_refused(self):
        self.listeners.write_text("")
        self.config.unlink()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertFalse(self.ssh_calls.exists())
        self.config.symlink_to(self.defaults)
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertFalse(self.ssh_calls.exists())

    def test_changed_startup_config_link_refused(self):
        self.listeners.write_text("")
        self.config_link.unlink()
        self.config_link.symlink_to(self.defaults)
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertFalse(self.ssh_calls.exists())

    def test_nonempty_or_executable_defaults_refused(self):
        self.listeners.write_text("")
        for value in ('SSHD_OPTS="-oListenAddress=0.0.0.0"\n', 'SSHD_OPTS=\nSSHD_CONF=/elsewhere\n'):
            with self.subTest(value=value):
                self.defaults.write_text(value)
                self.assertNotEqual(self.run_guard().returncode, 0)
                self.assertFalse(self.ssh_calls.exists())

    def test_changed_disabled_or_absent_ncml_service_refused(self):
        self.listeners.write_text("")
        for value in ("[ssh]\nsecure_port=2222\ncurrent_state=1\n",
                      "[ssh]\nsecure_port=22\ncurrent_state=0\n",
                      "[web]\nsecure_port=22\ncurrent_state=1\n",
                      "[ssh]\nsecure_port=22\nsecure_port=23\ncurrent_state=1\n"):
            with self.subTest(value=value):
                self.service_config.write_text(value)
                self.assertNotEqual(self.run_guard().returncode, 0)
                self.assertFalse(self.ssh_calls.exists())


if __name__ == "__main__":
    unittest.main()
