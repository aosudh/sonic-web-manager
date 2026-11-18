"""Owner-bound PTY sessions for local SONiC and explicitly pinned BMC SSH.

There is no browser-supplied host, username, executable or shell command. The
HTTP layer authenticates administrators and supplies its private session key
and authenticated local username. Input/output are never logged or persisted.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import stat
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone

BMC_CONFIG = Path("/host/sonic-web/state/bmc-console.json")
KNOWN_HOSTS_DIR = Path("/run/sonic-web-console")
HELPER = Path(__file__).resolve().parents[1] / "deploy" / "console_child.py"
PARENT_UNIT = "sonic-web-manager.service"
USERNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
COMPATIBILITY = {
    "kex_algorithms": ("KexAlgorithms", {"diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"}),
    "host_key_algorithms": ("HostKeyAlgorithms", {"ssh-rsa", "ssh-dss"}),
    "ciphers": ("Ciphers", {"aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc"}),
    "macs": ("MACs", {"hmac-sha1", "hmac-md5"}),
}


class ConsoleError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _utc(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def _dimensions(rows, cols):
    if type(rows) is not int or type(cols) is not int or not 2 <= rows <= 200 or not 10 <= cols <= 400:
        raise ConsoleError("终端尺寸必须为 2–200 行、10–400 列。")
    return rows, cols


def _validate_local_user(username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ConsoleError("本地终端用户无效。", 403)
    import pwd
    try:
        user = pwd.getpwnam(username)
    except KeyError as exc:
        raise ConsoleError("当前认证用户不存在于本机。", 403) from exc
    if user.pw_uid < 1000 or user.pw_uid >= 65534 or user.pw_name == "root":
        raise ConsoleError("不允许以 root 或系统账户启动网页终端。", 403)
    shell = user.pw_shell
    if not isinstance(shell, str) or not shell.startswith("/") or Path(shell).name in ("false", "nologin") or not os.access(shell, os.X_OK):
        raise ConsoleError("当前用户没有可用的登录 shell。", 403)
    if not isinstance(user.pw_dir, str) or not user.pw_dir.startswith("/"):
        raise ConsoleError("当前用户的主目录无效。", 403)
    return user


def _root_file(path, maximum=16384):
    """Open without following a symlink and validate the actual open inode."""
    path = Path(path)
    if path.resolve() != path or path.is_symlink():
        raise ConsoleError("终端配置路径不安全。", 503)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size > maximum:
            raise ConsoleError("BMC 终端配置必须是 root 独占读取的普通文件。", 503)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ConsoleError("BMC 终端配置过大。", 503)
        return data
    finally:
        os.close(fd)


def _load_bmc_target():
    try:
        data = json.loads(_root_file(BMC_CONFIG))
    except (OSError, ValueError) as exc:
        raise ConsoleError("BMC 系统 SSH 尚未配置并验证。", 503) from exc
    allowed = {"verified", "host", "port", "username", "host_key", "compatibility", "compatibility_verified"}
    if not isinstance(data, dict) or set(data) - allowed or data.get("verified") is not True:
        raise ConsoleError("BMC 系统 SSH 尚未配置并验证。", 503)
    host, username, host_key = data.get("host"), data.get("username"), data.get("host_key")
    try:
        if not isinstance(host, str) or "%" in host or any(c.isspace() for c in host):
            raise ValueError()
        address = ipaddress.ip_address(host)
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError()
    except ValueError as exc:
        raise ConsoleError("BMC 目标必须是已验证的固定 IP 地址。", 503) from exc
    if data.get("port", 22) != 22 or type(data.get("port", 22)) is not int:
        raise ConsoleError("BMC 系统终端只允许固定 SSH 端口 22。", 503)
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ConsoleError("BMC SSH 用户名配置无效。", 503)
    try:
        parts = host_key.split() if isinstance(host_key, str) else []
        if len(parts) != 2 or parts[0] not in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa", "ssh-dss"}:
            raise ValueError()
        key = base64.b64decode(parts[1], validate=True)
        size = int.from_bytes(key[:4], "big")
        if not 4 < len(key) <= 8192 or not 1 <= size <= 64 or key[4:4 + size].decode("ascii") != parts[0] or len(key) <= 4 + size:
            raise ValueError()
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ConsoleError("BMC SSH 主机公钥未有效固定。", 503) from exc
    compatibility = data.get("compatibility", {})
    if not isinstance(compatibility, dict) or set(compatibility) - set(COMPATIBILITY):
        raise ConsoleError("BMC SSH 兼容选项配置无效。", 503)
    if compatibility and data.get("compatibility_verified") is not True:
        raise ConsoleError("旧 SSH 算法尚未经过实机验证。", 503)
    for field, values in compatibility.items():
        if not isinstance(values, list) or not values or len(values) > 4 or any(type(v) is not str or v not in COMPATIBILITY[field][1] for v in values):
            raise ConsoleError("BMC SSH 兼容选项超出固定白名单。", 503)
    return {"host": str(address), "port": 22, "username": username, "host_key": " ".join(parts), "compatibility": compatibility}


def _prepare_known_hosts(target):
    """Only a public pinned host key becomes readable after dropping uid."""
    directory = KNOWN_HOSTS_DIR
    if directory.is_symlink() or directory.resolve() != directory:
        raise ConsoleError("SSH 主机公钥目录不安全。", 503)
    directory.mkdir(mode=0o755, exist_ok=True)
    info = directory.stat()
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ConsoleError("SSH 主机公钥目录权限不安全。", 503)
    os.chmod(directory, 0o755)  # Public keys only; service umask is otherwise 0077.
    content = (target["host"] + " " + target["host_key"] + "\n").encode("ascii")
    path = directory / (hashlib.sha256(content).hexdigest() + ".known_hosts")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or os.read(descriptor, 16385) != content:
                raise ConsoleError("固定 SSH 公钥文件不匹配。", 503)
        finally:
            os.close(descriptor)
    else:
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o644)
        finally:
            os.close(descriptor)
    return str(path)


def _ssh_argv(target, known_hosts):
    options = ["StrictHostKeyChecking=yes", "UserKnownHostsFile=" + known_hosts,
        "GlobalKnownHostsFile=/dev/null", "UpdateHostKeys=no", "VerifyHostKeyDNS=no",
        "CheckHostIP=yes", "CanonicalizeHostname=no", "ProxyCommand=none", "ProxyJump=none",
        "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
        "EscapeChar=none", "ControlMaster=no", "ControlPath=none", "IdentityAgent=none",
        "IdentitiesOnly=yes", "IdentityFile=none", "PubkeyAuthentication=no",
        "PreferredAuthentications=keyboard-interactive,password", "NumberOfPasswordPrompts=3",
        "BatchMode=no", "ConnectTimeout=10", "ServerAliveInterval=30", "ServerAliveCountMax=2", "LogLevel=ERROR"]
    for field, values in target["compatibility"].items():
        options.append(COMPATIBILITY[field][0] + "=+" + ",".join(values))
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-tt", "-p", "22"]
    for option in options:
        argv.extend(["-o", option])
    return [*argv, "-l", target["username"], "--", target["host"]]


class _Session:
    def __init__(self, owner, user, target, rows, cols, now, demo):
        self.id = secrets.token_hex(16)
        self.owner, self.user, self.target, self.demo = owner, user, target, demo
        self.rows, self.cols = rows, cols
        self.created = self.last_active = self.token_time = now
        self.created_at = _utc()
        self.expires_at = _utc(time.time() + ConsoleManager.MAX_SECONDS)
        self.buffer, self.pending = bytearray(), bytearray()
        self.oldest = self.end = 0
        self.tokens = ConsoleManager.INPUT_RATE
        self.closed, self.reason, self.exit_code, self.closed_at = False, None, None, None
        self.fd, self.process, self.reader = None, None, None
        self.unit = "sonic-web-console-" + self.id + ".service"
        self.cleanup_confirmed = demo
        self.launch_confirmed = demo
        self.lock = threading.RLock()


class ConsoleManager:
    MAX_GLOBAL = 4
    MAX_OWNER = 2
    RING_BYTES = 256 * 1024
    READ_BYTES = 64 * 1024
    INPUT_BYTES = 4096
    INPUT_RATE = 16 * 1024
    PENDING_BYTES = 16 * 1024
    IDLE_SECONDS = 300
    MAX_SECONDS = 1800
    CLOSED_SECONDS = 60

    def __init__(self, demo=False, owner_alive=None, clock=None):
        if type(demo) is not bool:
            raise TypeError("demo must be an explicit boolean")
        self.demo, self.owner_alive = demo, owner_alive
        self._clock = clock or time.monotonic
        self._sessions = {}
        self._lock = threading.RLock()
        self._closed = False
        self._stop = threading.Event()
        self._reaper = None
        self._systemd_check = None

    def _systemd_ready(self):
        with self._lock:
            now = self._clock()
            if self._systemd_check and now - self._systemd_check[0] < 30:
                return self._systemd_check[1]
            try:
                result = subprocess.run(["/usr/bin/systemctl", "show", "--property=Version", "--value"],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3, check=False)
                ready = result.returncode == 0 and bool(result.stdout.strip())
                if ready and PARENT_UNIT:
                    parent = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", PARENT_UNIT],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            timeout=3, check=False)
                    ready = parent.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ready = False
            self._systemd_check = (self._clock(), ready)
            return ready

    def _live_user(self, user):
        if os.name != "posix" or os.geteuid() != 0:
            raise ConsoleError("本服务环境不能安全启动本地用户 PTY。", 503)
        account = _validate_local_user(user)
        info = HELPER.stat()
        if HELPER.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ConsoleError("终端 helper 未以安全权限安装。", 503)
        if not Path("/usr/bin/systemd-run").is_file() or not Path("/usr/bin/systemctl").is_file():
            raise ConsoleError("本机缺少受控终端服务管理器。", 503)
        if not self._systemd_ready():
            raise ConsoleError("受控终端服务管理器或父服务当前不可用。", 503)
        return account

    def capabilities(self, user):
        result = {"mode": "demo" if self.demo else "live", "sonic": {"available": False, "identity": user, "reason": "本地用户终端尚不可用。"},
                  "bmc": {"available": False, "identity": None, "reason": "BMC 自身系统 SSH 尚未配置并验证；不是 SOL。"}}
        if self._closed:
            result["sonic"]["reason"] = result["bmc"]["reason"] = "终端服务已关闭。"
            return result
        if self.demo:
            for target in ("sonic", "bmc"):
                result[target].update(available=True, identity="DEMO " + target, reason="仅内存模拟，不连接宿主或 BMC。")
            return result
        try:
            self._live_user(user)
        except (ConsoleError, OSError) as exc:
            result["sonic"]["reason"] = str(exc) if isinstance(exc, ConsoleError) else "终端 helper 尚不可用。"
            return result
        result["sonic"].update(available=True, reason="以当前已认证的本地用户启动登录 shell，不固定使用 root。")
        try:
            target = _load_bmc_target()
            if not Path("/usr/bin/ssh").is_file():
                raise ConsoleError("本机 SSH 客户端不可用。", 503)
            result["bmc"].update(available=True, identity=target["username"] + "@" + target["host"],
                                 reason="固定并验证主机公钥的 BMC 自身系统 SSH；密码仅在终端交互中输入。")
        except (ConsoleError, OSError) as exc:
            result["bmc"]["reason"] = str(exc) if isinstance(exc, ConsoleError) else "BMC SSH 配置不可读。"
        return result

    def _owner_valid(self, owner):
        if not isinstance(owner, str) or not 1 <= len(owner) <= 256:
            raise ConsoleError("终端会话所有者无效。", 401)
        if self.owner_alive is not None:
            try:
                valid = self.owner_alive(owner)
            except Exception:
                valid = False
            if valid is not True:
                self.close_owner(owner, reason="session_expired")
                raise ConsoleError("网页登录会话已失效。", 401)

    def _get(self, owner, identifier):
        self._owner_valid(owner)
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ConsoleError("终端不存在。", 404)
        with self._lock:
            session = self._sessions.get(identifier)
            if session is None or session.owner != owner:
                raise ConsoleError("终端不存在。", 404)
        self._expire(session)
        return session

    def _append(self, session, data):
        with session.lock:
            session.buffer.extend(data)
            session.end += len(data)
            if len(session.buffer) > self.RING_BYTES:
                del session.buffer[:-self.RING_BYTES]
            session.oldest = session.end - len(session.buffer)

    def create(self, owner, user, target, rows=24, cols=80):
        self._owner_valid(owner)
        _dimensions(rows, cols)
        if target not in ("sonic", "bmc"):
            raise ConsoleError("仅允许 SONiC 或 BMC 两个固定目标。")
        capability = self.capabilities(user)[target]
        if not capability["available"]:
            raise ConsoleError(capability["reason"], 503)
        with self._lock:
            if self._closed:
                raise ConsoleError("终端服务已关闭。", 503)
            self._expire_all()
            active = [s for s in self._sessions.values() if not s.closed or not s.cleanup_confirmed]
            if len(active) >= self.MAX_GLOBAL or sum(s.owner == owner for s in active) >= self.MAX_OWNER:
                raise ConsoleError("终端会话达到数量上限。", 429)
            session = _Session(owner, user, target, rows, cols, self._clock(), self.demo)
            self._sessions[session.id] = session
            if not self.demo:
                try:
                    self._spawn(session)
                except Exception:
                    self._finish(session, "startup_failed")
                    if session.cleanup_confirmed:
                        self._discard(session)
                    if self._reaper is None:
                        self._reaper = threading.Thread(target=self._reap, daemon=True, name="console-reaper")
                        self._reaper.start()
                    raise
            if self.demo:
                self._append(session, ("DEMO " + target + " terminal (memory simulation; no host execution)\r\n$ ").encode())
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, daemon=True, name="console-reaper")
                self._reaper.start()
            # Bound retained closed-session output as well as live processes.
            closed = sorted((s for s in self._sessions.values() if s.closed and s.cleanup_confirmed), key=lambda s: s.closed_at or 0)
            for stale in closed[:-8]:
                self._discard(stale)
        return {"id": session.id, "target": target, "mode": "demo" if self.demo else "live", "cursor": 0,
                "created_at": session.created_at, "expires_at": session.expires_at, "identity": capability["identity"]}

    def _spawn(self, session):
        import fcntl
        import pty
        import termios
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", session.rows, session.cols, 0, 0))
            os.set_blocking(master, False)
            argv = ["/usr/bin/systemd-run", "--pty", "--wait", "--collect", "--quiet", "--unit=" + session.unit,
                    "--property=Type=exec", "--property=User=root", "--property=NoNewPrivileges=no",
                    "--property=KillMode=control-group", "--property=TimeoutStopSec=2", "--property=RuntimeMaxSec=1800",
                    "--property=MemoryMax=128M", "--property=TasksMax=64", "--property=CPUQuota=50%",
                    "--property=UMask=0077", "--property=WorkingDirectory=/"]
            if PARENT_UNIT:
                argv.extend(["--property=BindsTo=" + PARENT_UNIT, "--property=PartOf=" + PARENT_UNIT,
                             "--property=After=" + PARENT_UNIT])
            argv.extend(["/usr/bin/python3", "-I", str(HELPER), "--user", session.user, "--target", session.target])
            environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "TERM": "xterm-256color", "LANG": "C.UTF-8"}
            session.process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                               env=environment, shell=False, start_new_session=True)
            session.fd = master
            # Do not return a browser-visible ID while systemd-run could still
            # submit its first StartTransientUnit request after an early close.
            self._await_started(session)
        except (OSError, ValueError) as exc:
            if session.fd is None:
                os.close(master)
            raise ConsoleError("终端子进程启动失败。", 503) from exc
        finally:
            os.close(slave)
        session.reader = threading.Thread(target=self._pump, args=(session,), daemon=True, name="console-pty")
        session.reader.start()

    @staticmethod
    def _await_started(session):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if session.process.poll() is not None:
                raise ConsoleError("终端服务在启动期间退出。", 503)
            try:
                result = subprocess.run(["/usr/bin/systemctl", "show", "--property=LoadState", "--property=ActiveState", session.unit],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=min(1, max(.05, deadline - time.monotonic())), check=False)
                values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                if result.returncode == 0 and values.get("LoadState") == "loaded" and values.get("ActiveState") in ("active", "activating"):
                    session.launch_confirmed = True
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(.05)
        raise ConsoleError("终端服务启动尚未确认；后台清理正在进行。", 503)

    def _pump(self, session):
        while not session.closed:
            with session.lock:
                descriptor = session.fd
                wants_write = bool(session.pending)
            if descriptor is None:
                break
            try:
                ready, writable, _ = select.select([descriptor], [descriptor] if wants_write else [], [], 0.1)
                # select may wake after close() and the OS may have reused the
                # integer for another owner's PTY. Revalidate under the same
                # lock used to close the fd, and keep each nonblocking I/O plus
                # buffer update within that lock.
                with session.lock:
                    if session.closed or session.fd != descriptor:
                        break
                    if ready:
                        data = os.read(descriptor, 65536)
                        if not data:
                            break
                        self._append(session, data)
                    if writable:
                        count = os.write(descriptor, session.pending[:self.INPUT_BYTES])
                        del session.pending[:count]
            except BlockingIOError:
                continue
            except (OSError, ValueError):
                break
        self._finish(session, "process_exit")

    def read(self, owner, identifier, cursor=0):
        if type(cursor) is not int or cursor < 0:
            raise ConsoleError("终端读取游标无效。")
        session = self._get(owner, identifier)
        with session.lock:
            if cursor > session.end:
                raise ConsoleError("终端读取游标超出输出范围。")
            start = max(cursor, session.oldest)
            end = min(session.end, start + self.READ_BYTES)
            data = bytes(session.buffer[start - session.oldest:end - session.oldest])
            return {"id": identifier, "data": base64.b64encode(data).decode(), "cursor": start, "next_cursor": end,
                    "oldest_cursor": session.oldest, "truncated": cursor < session.oldest, "closed": session.closed,
                    "exit_code": session.exit_code, "reason": session.reason, "cleanup_confirmed": session.cleanup_confirmed,
                    "mode": "demo" if session.demo else "live"}

    def write(self, owner, identifier, data_b64):
        if not isinstance(data_b64, str) or len(data_b64) > 5464:
            raise ConsoleError("单次终端输入最多 4 KiB。", 413)
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ConsoleError("终端输入必须是有效 base64。") from exc
        if not 1 <= len(data) <= self.INPUT_BYTES:
            raise ConsoleError("单次终端输入必须为 1–4096 字节。", 413)
        session = self._get(owner, identifier)
        with session.lock:
            if session.closed:
                raise ConsoleError("终端已关闭。", 409)
            now = self._clock()
            session.tokens = min(self.INPUT_RATE, session.tokens + max(0, now - session.token_time) * self.INPUT_RATE)
            session.token_time = now
            if len(data) > session.tokens or len(session.pending) + len(data) > self.PENDING_BYTES:
                raise ConsoleError("终端输入过快，请稍后重试。", 429)
            session.tokens -= len(data)
            session.last_active = now
            if session.demo:
                self._append(session, data.replace(b"\r", b"\r\n") + (b"$ " if b"\r" in data or b"\n" in data else b""))
            else:
                session.pending.extend(data)
        return {"accepted_bytes": len(data)}

    def resize(self, owner, identifier, rows, cols):
        _dimensions(rows, cols)
        session = self._get(owner, identifier)
        with session.lock:
            if session.closed:
                raise ConsoleError("终端已关闭。", 409)
            if (rows, cols) != (session.rows, session.cols):
                if not session.demo:
                    import fcntl
                    import termios
                    try:
                        fcntl.ioctl(session.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                        os.killpg(session.process.pid, signal.SIGWINCH)
                    except OSError as exc:
                        raise ConsoleError("终端尺寸更新失败。", 409) from exc
                session.rows, session.cols = rows, cols
                session.last_active = self._clock()
        return {"rows": rows, "cols": cols}

    def _finish(self, session, reason):
        with session.lock:
            if session.closed:
                return
            session.closed, session.reason, session.closed_at = True, reason, self._clock()
            descriptor, session.fd = session.fd, None
            session.pending.clear()
            process = session.process
            if process is not None:
                session.exit_code = process.poll()
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            # Stop/reap the launcher BEFORE querying/removing its unit, so it
            # cannot submit a new service request after a not-found result.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if process.poll() is None:
                    process.kill()  # Helper may not have reached setsid yet.
            except OSError:
                if process.poll() is None:
                    process.kill()
            try:
                code = process.wait(timeout=1)
                with session.lock:
                    if session.exit_code is None:
                        session.exit_code = code
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            # The transient service owns a cgroup: detached/double-forked jobs
            # cannot escape cleanup merely by changing their process group.
            # A timed-out, unregistered launch stays tracked; not-found alone
            # cannot prove that an in-flight D-Bus request will never arrive.
            session.cleanup_confirmed = self._stop_unit(session.unit, allow_not_found=session.launch_confirmed)
        else:
            session.cleanup_confirmed = True  # No launcher was ever created.

    @staticmethod
    def _stop_unit(unit, allow_not_found=True):
        if not re.fullmatch(r"sonic-web-console-[a-f0-9]{32}\.service", unit):
            return False
        try:
            result = subprocess.run(["/usr/bin/systemctl", "stop", unit], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
            if result.returncode == 0:
                return True
            if not allow_not_found:
                return False
            # --collect removes finished transient services, which is also a
            # confirmed terminal state. Do not forward arbitrary stderr.
            state = subprocess.run(["/usr/bin/systemctl", "show", "--property=LoadState", "--value", unit],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, check=False)
            return state.stdout.strip() == "not-found"
        except (OSError, subprocess.SubprocessError):
            return False

    def close(self, owner, identifier):
        session = self._get(owner, identifier)
        self._finish(session, "closed")
        if not session.cleanup_confirmed:
            raise ConsoleError("终端已断开，但后台服务清理尚未确认，正在重试。", 503)
        return {"id": identifier, "closed": True, "cleanup_confirmed": True}

    def _discard(self, session):
        self._sessions.pop(session.id, None)
        with session.lock:
            session.buffer.clear()
            session.pending.clear()

    def close_owner(self, owner, reason="logout"):
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.owner == owner]
            for session in sessions:
                self._finish(session, reason)
                if session.cleanup_confirmed:
                    self._discard(session)
                else:
                    session.buffer.clear()

    def _expire(self, session):
        now = self._clock()
        if not session.closed:
            if now - session.created >= self.MAX_SECONDS:
                self._finish(session, "max_duration")
            elif now - session.last_active >= self.IDLE_SECONDS:
                self._finish(session, "idle_timeout")

    def _expire_all(self):
        with self._lock:
            for session in list(self._sessions.values()):
                self._expire(session)
                if session.closed and not session.cleanup_confirmed:
                    session.cleanup_confirmed = self._stop_unit(session.unit, allow_not_found=session.launch_confirmed)
                if session.closed and session.cleanup_confirmed and self._clock() - session.closed_at >= self.CLOSED_SECONDS:
                    self._discard(session)

    def _reap(self):
        while not self._stop.wait(1):
            self._expire_all()
            if self.owner_alive is not None:
                with self._lock:
                    owners = {s.owner for s in self._sessions.values()}
                for owner in owners:
                    try:
                        self._owner_valid(owner)
                    except ConsoleError:
                        pass

    def close_all(self):
        self._stop.set()
        with self._lock:
            self._closed = True
            for session in list(self._sessions.values()):
                self._finish(session, "service_close")
                self._discard(session)
        if self._reaper is not None and self._reaper is not threading.current_thread():
            self._reaper.join(timeout=2)
