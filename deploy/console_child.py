#!/usr/bin/env python3
"""Fresh single-threaded privilege-drop helper; fixed local shell or BMC SSH."""
import argparse
import fcntl
import os
from pathlib import Path
import sys
import termios

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.console import _validate_local_user, _load_bmc_target, _prepare_known_hosts, _ssh_argv


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--target", required=True, choices=("sonic", "bmc"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0 or not os.isatty(0):
            raise RuntimeError("requires privileged PTY helper")
        user = _validate_local_user(args.user)
        command = ["-" + Path(user.pw_shell).name]
        executable = user.pw_shell
        if args.target == "bmc":
            target = _load_bmc_target()
            known_hosts = _prepare_known_hosts(target)
            command = _ssh_argv(target, known_hosts)
            executable = command[0]
        if os.getsid(0) != os.getpid():
            os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        if os.getuid() != user.pw_uid or os.geteuid() != user.pw_uid or os.getgid() != user.pw_gid:
            raise RuntimeError("privilege drop not confirmed")
        try:
            os.chdir(user.pw_dir)
        except OSError:
            os.chdir("/")
        environment = {"HOME": user.pw_dir, "USER": user.pw_name, "LOGNAME": user.pw_name,
                       "SHELL": user.pw_shell, "TERM": "xterm-256color", "LANG": "C.UTF-8",
                       "HISTFILE": "/dev/null", "HISTSIZE": "0", "HISTFILESIZE": "0",
                       "HISTIGNORE": "*", "PROMPT_COMMAND": "HISTFILE=/dev/null; HISTSIZE=0; HISTFILESIZE=0; history -c; set +o history",
                       "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"}
        os.execve(executable, command, environment)
    except Exception:
        # No input, credentials, environment dump or detailed SSH configuration.
        print("Console startup failed; no privileged shell was started.", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
