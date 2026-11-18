"""Local PAM authentication and bounded, in-memory browser sessions."""
from __future__ import annotations
from collections import OrderedDict, deque
from dataclasses import dataclass
import ctypes
import ctypes.util
import hashlib
import multiprocessing
import re
import secrets
import threading
import time


class AuthError(Exception):
    def __init__(self, message="Invalid username or password", status=401):
        super().__init__(message)
        self.status = status


def validate_credentials(username, password):
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", username):
        raise AuthError()
    if not isinstance(password, str) or not password or len(password.encode("utf-8")) > 1024 or "\0" in password:
        raise AuthError()


def local_role(username, admin_groups=("sudo", "admin")):
    """Only an existing login account can authenticate; privilege is OS-derived."""
    try:
        import pwd
        import grp
        account = pwd.getpwnam(username)
        if account.pw_shell.rsplit("/", 1)[-1] in ("nologin", "false"):
            raise AuthError()
        if account.pw_uid == 0:
            return "admin"
        gids = set(__import__("os").getgrouplist(username, account.pw_gid))
        names = {grp.getgrgid(gid).gr_name for gid in gids}
        return "admin" if names.intersection(admin_groups) else "viewer"
    except (ImportError, KeyError, OSError):
        raise AuthError() from None


def _pam_check(username, password, service, pamlib=None, libc=None):
    """PAM owns calloc/strdup conversation replies; no Python buffer is freed by PAM."""
    class Message(ctypes.Structure):
        _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]

    class Response(ctypes.Structure):
        _fields_ = [("resp", ctypes.c_void_p), ("resp_retcode", ctypes.c_int)]

    conversation_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                                         ctypes.POINTER(ctypes.POINTER(Message)),
                                         ctypes.POINTER(ctypes.POINTER(Response)), ctypes.c_void_p)

    class Conversation(ctypes.Structure):
        _fields_ = [("conv", conversation_type), ("appdata_ptr", ctypes.c_void_p)]

    pam = pamlib or ctypes.CDLL(ctypes.util.find_library("pam") or "libpam.so.0")
    memory = libc or ctypes.CDLL(None)
    memory.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    memory.calloc.restype = ctypes.c_void_p
    memory.strdup.argtypes = [ctypes.c_char_p]
    memory.strdup.restype = ctypes.c_void_p
    memory.free.argtypes = [ctypes.c_void_p]
    memory.free.restype = None
    secret = bytearray(password.encode("utf-8"))

    @conversation_type
    def converse(count, messages, reply, _):
        if count <= 0 or count > 16:
            return 19  # PAM_CONV_ERR
        allocation = memory.calloc(count, ctypes.sizeof(Response))
        if not allocation:
            return 5  # PAM_BUF_ERR
        responses = ctypes.cast(allocation, ctypes.POINTER(Response))
        try:
            for i in range(count):
                style = messages[i].contents.msg_style
                if style == 1:  # PAM_PROMPT_ECHO_OFF
                    value = bytes(secret)
                elif style == 2:  # PAM_PROMPT_ECHO_ON
                    value = username.encode("utf-8")
                elif style in (3, 4):  # ERROR_MSG / TEXT_INFO: do not expose prompts
                    continue
                else:
                    raise ValueError("Unsupported PAM conversation")
                ptr = memory.strdup(value)
                if not ptr:
                    raise MemoryError()
                responses[i].resp = ptr
            reply[0] = responses
            return 0
        except BaseException:
            for i in range(count):
                if responses[i].resp:
                    memory.free(responses[i].resp)
            memory.free(allocation)
            return 19

    pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conversation), ctypes.POINTER(ctypes.c_void_p)]
    pam.pam_start.restype = ctypes.c_int
    for name in ("pam_authenticate", "pam_acct_mgmt", "pam_end"):
        getattr(pam, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        getattr(pam, name).restype = ctypes.c_int
    handle = ctypes.c_void_p()
    conversation = Conversation(converse, None)
    code = 4
    try:
        code = pam.pam_start(service.encode("ascii"), username.encode("utf-8"), ctypes.byref(conversation), ctypes.byref(handle))
        if code == 0:
            code = pam.pam_authenticate(handle, 1)  # PAM_DISALLOW_NULL_AUTHTOK
        if code == 0:
            code = pam.pam_acct_mgmt(handle, 0)  # reject expired/disabled accounts; never change passwords
        return code == 0
    finally:
        if handle.value:
            pam.pam_end(handle, code)
        secret[:] = b"\0" * len(secret)


def _pam_worker(connection, username, password, service):
    try:
        connection.send(bool(_pam_check(username, password, service)))
    except BaseException:
        connection.send(False)
    finally:
        connection.close()


class PamAuthenticator:
    def __init__(self, service="login", admin_groups=("sudo", "admin"), timeout=12):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", service):
            raise ValueError("Invalid PAM service")
        self.service, self.admin_groups, self.timeout = service, tuple(admin_groups), timeout
        self._slots = threading.BoundedSemaphore(2)

    def authenticate(self, username, password):
        validate_credentials(username, password)
        role = local_role(username, self.admin_groups)
        if not self._slots.acquire(blocking=False):
            raise AuthError("Authentication is busy; try again shortly", 429)
        parent = child = process = None
        try:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe(duplex=False)
            process = context.Process(target=_pam_worker, args=(child, username, password, self.service), daemon=True)
            process.start()
            child.close()
            accepted = parent.poll(self.timeout) and parent.recv() is True
            if not accepted:
                raise AuthError()
            return {"user": username, "role": role}
        except (EOFError, OSError):
            raise AuthError() from None
        finally:
            if process is not None and process.pid is not None:
                process.join(timeout=0.2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            self._slots.release()


class DemoAuthenticator:
    def authenticate(self, username, password):
        validate_credentials(username, password)
        if secrets.compare_digest(username, "demo") and secrets.compare_digest(password, "demo"):
            return {"user": "demo", "role": "admin"}
        raise AuthError()


@dataclass
class Session:
    key: str
    user: str
    role: str
    csrf: str
    created: float
    seen: float


class SessionStore:
    def __init__(self, idle_seconds=1800, lifetime_seconds=28800, limit=128, clock=time.monotonic):
        self.idle, self.lifetime, self.limit, self.clock = idle_seconds, lifetime_seconds, limit, clock
        self._sessions = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def key(token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            return ""
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def create(self, user, role):
        if role not in ("admin", "viewer"):
            raise ValueError("Invalid role")
        token = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(self.key(token), user, role, secrets.token_urlsafe(32), now, now)
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= self.limit:
                self._sessions.popitem(last=False)
            self._sessions[session.key] = session
        return token, session

    def _purge(self, now):
        for key, s in list(self._sessions.items()):
            if now - s.seen >= self.idle or now - s.created >= self.lifetime:
                del self._sessions[key]

    def get(self, token):
        with self._lock:
            now = self.clock()
            self._purge(now)
            s = self._sessions.get(self.key(token))
            if s is not None:
                s.seen = now
                self._sessions.move_to_end(s.key)
            return s

    def delete(self, token):
        with self._lock:
            self._sessions.pop(self.key(token), None)

    def is_active_key(self, key):
        """Check a server-held session owner without renewing its idle timer."""
        with self._lock:
            self._purge(self.clock())
            return key in self._sessions


class RateLimiter:
    def __init__(self, attempts=8, window=60, max_keys=2048, clock=time.monotonic):
        self.attempts, self.window, self.max_keys, self.clock = attempts, window, max_keys, clock
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        with self._lock:
            now = self.clock()
            events = self._items.setdefault(key, deque())
            while events and now-events[0] >= self.window:
                events.popleft()
            self._items.move_to_end(key)
            while len(self._items) > self.max_keys:
                self._items.popitem(last=False)
            if len(events) >= self.attempts:
                return False
            events.append(now)
            return True
