"""Bounded stdlib HTTPS service for SONiC Web Manager."""
from __future__ import annotations
import argparse
from collections import OrderedDict
import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit

from .auth import AuthError, DemoAuthenticator, PamAuthenticator, RateLimiter, SessionStore
from .console import ConsoleManager

MAX_BODY = 65536
MAX_RESPONSE = 4 * 1024 * 1024
MAX_STATIC = 8 * 1024 * 1024
CONSOLE_ID = r"[a-f0-9]{32}"


class HTTPError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logger = logging.Logger("sonic-web-audit-" + secrets.token_hex(4))
        class PrivateRotatingHandler(RotatingFileHandler):
            def _open(self):
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.baseFilename, flags, 0o600)
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                return os.fdopen(fd, "a", encoding="utf-8")
        handler = PrivateRotatingHandler(directory / "audit.jsonl", maxBytes=1024*1024, backupCount=2, encoding="utf-8")
        if os.name == "posix":
            os.chmod(directory / "audit.jsonl", 0o600)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)

    def event(self, event, **fields):
        # Never log bodies, passwords, cookies, CSRF values, command strings or config contents.
        allowed = {"user", "role", "action", "plan_id", "job_id", "status", "address", "error_type"}
        record = {"at": utc_now(), "event": event}
        record.update({k: v for k, v in fields.items() if k in allowed and isinstance(v, (str, int, bool))})
        self.logger.info(json.dumps(record, ensure_ascii=True))

    def close(self):
        for handler in self.logger.handlers:
            handler.close()


class Application:
    def __init__(self, adapter, data_dir, static_dir, demo=False, tls=False, authenticator=None):
        if not demo and not tls:
            raise ValueError("Live mode requires HTTPS")
        self.adapter, self.demo, self.tls = adapter, demo, tls
        self.mode = "demo" if demo else "live"
        self.static_dir = Path(static_dir).resolve()
        self.auth = authenticator or (DemoAuthenticator() if demo else PamAuthenticator())
        if not demo and isinstance(self.auth, DemoAuthenticator):
            raise ValueError("Demo authentication cannot be used in live mode")
        self.sessions = SessionStore()
        self.consoles = ConsoleManager(demo=demo, owner_alive=self.sessions.is_active_key)
        self.rate = RateLimiter()
        self.cookie_name = "__Host-swm_session" if tls else "swm_session"
        self.audit = AuditLog(data_dir)
        self.lock = threading.RLock()
        self.plans = OrderedDict()
        self.jobs = OrderedDict()
        self.pending = queue.Queue(maxsize=8)
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._worker, name="sonic-web-apply", daemon=True)
        self.worker.start()

    def close(self):
        self.closed.set()
        self.consoles.close_all()
        with self.lock:
            for job in self.jobs.values():
                if job["status"] == "queued":
                    job.update(status="failed", error="Service stopped before execution; review a new plan", finished_at=utc_now())
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            pass
        self.worker.join(timeout=125)
        # A running bounded adapter write is not falsely reported as cancelled.
        if not self.worker.is_alive():
            close_adapter = getattr(self.adapter, "close", None)
            if close_adapter:
                close_adapter()
            self.audit.close()

    def _worker(self):
        while not self.closed.is_set():
            try:
                job_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                return
            with self.lock:
                job = self.jobs[job_id]
                if self.closed.is_set() or job["status"] != "queued":
                    self.pending.task_done()
                    return
                job["status"], job["started_at"] = "running", utc_now()
            try:
                # Adapter guarantees serialized writes, per-command timeout and 120s total deadline.
                result = self.adapter.apply(job["plan_id"])
                if not isinstance(result, dict):
                    raise TypeError("Adapter returned an invalid apply result")
                with self.lock:
                    job["result"] = result
                    job["status"] = "succeeded" if result.get("success") is True else "failed"
                    if job["status"] == "failed":
                        job["error"] = str(result.get("message") or "Change did not complete")[:2000]
            except Exception as error:
                with self.lock:
                    job["status"] = "failed"
                    job["error"] = str(error)[:2000] if hasattr(error, "status") else "Backend could not complete the change"
            finally:
                with self.lock:
                    job["finished_at"] = utc_now()
                self.audit.event("apply_finished", user=job["user"], job_id=job_id, action=job["action"], status=job["status"])
                self.pending.task_done()

    def remember_plan(self, session, plan):
        if not isinstance(plan, dict) or not isinstance(plan.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", plan["id"]):
            raise HTTPError(502, "invalid_backend_result", "Backend returned an invalid plan")
        now = time.monotonic()
        expiry = 300.0
        try:
            raw = plan.get("expires_at")
            if isinstance(raw, (int, float)):
                expiry = min(expiry, raw-time.time())
            elif isinstance(raw, str):
                stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                expiry = min(expiry, stamp.timestamp()-time.time())
        except (ValueError, TypeError, OverflowError):
            raise HTTPError(502, "invalid_backend_result", "Backend returned an invalid plan expiry") from None
        with self.lock:
            self.plans = OrderedDict((k, p) for k, p in self.plans.items() if p["deadline"] > now)
            while len(self.plans) >= 128:
                self.plans.popitem(last=False)
            self.plans[plan["id"]] = {"owner": session.key, "deadline": now+max(0, expiry),
                                      "action": str(plan.get("action", ""))[:128]}

    def enqueue(self, session, plan_id):
        with self.lock:
            if self.closed.is_set():
                raise HTTPError(503, "stopping", "Service is stopping; review a new plan after restart")
            plan = self.plans.get(plan_id)
            if plan is None or plan["owner"] != session.key:
                raise HTTPError(404, "plan_not_found", "Plan is unavailable for this session")
            if plan["deadline"] <= time.monotonic():
                del self.plans[plan_id]
                raise HTTPError(409, "plan_expired", "Plan expired; review a new plan")
            if self.pending.full():
                raise HTTPError(429, "queue_full", "Change queue is full; try again shortly")
            while len(self.jobs) >= 32:
                finished = next((k for k, j in self.jobs.items() if j["status"] in ("succeeded", "failed")), None)
                if finished is None:
                    raise HTTPError(429, "queue_full", "Too many active jobs")
                del self.jobs[finished]
            job_id = secrets.token_urlsafe(24)
            self.jobs[job_id] = {"id": job_id, "status": "queued", "user": session.user,
                                 "plan_id": plan_id, "action": plan["action"], "created_at": utc_now()}
            self.pending.put_nowait(job_id)
            del self.plans[plan_id]
            self.audit.event("apply_queued", user=session.user, role=session.role, plan_id=plan_id,
                             job_id=job_id, action=plan["action"])
            return job_id


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, app, tls_context=None, allowed_hosts=()):
        if app.demo and not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError("Demo server may bind only to loopback")
        if bool(tls_context) != bool(app.tls):
            raise ValueError("TLS context must match application transport mode")
        self.app, self.tls_context = app, tls_context
        self._slots = threading.BoundedSemaphore(12)
        super().__init__(address, RequestHandler)
        self.allowed_hosts = {address[0].lower(), *[h.lower() for h in allowed_hosts]}
        if ipaddress.ip_address(address[0]).is_loopback:
            self.allowed_hosts.update(("localhost", "127.0.0.1", "::1"))

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        if self.tls_context:
            connection = self.tls_context.wrap_socket(connection, server_side=True, do_handshake_on_connect=False)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                body = b'{"error":"busy","message":"Server is busy; try again shortly"}'
                # Never perform an untrusted TLS handshake on the accepting thread.
                if not self.tls_context:
                    request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SONiCWebManager"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # URL/query/body logging could disclose credentials.

    @property
    def app(self):
        return self.server.app

    def _headers(self, content_type, size, cookie=None):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), serial=(self)")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if self.app.tls:
            self.send_header("Strict-Transport-Security", "max-age=86400")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _json(self, status, value, cookie=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_RESPONSE:
            raise HTTPError(502, "response_too_large", "Backend response exceeds the size limit")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(payload), cookie)
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._json(code, {"error": "http_error", "message": self.responses.get(code, ("Request rejected",))[0]})

    def _host(self):
        if len(self.headers.get_all("Host", [])) != 1:
            raise HTTPError(400, "invalid_host", "A single Host header is required")
        host = self.headers["Host"]
        try:
            parsed = urlsplit("//" + host)
            port = parsed.port or (443 if self.app.tls else 80)
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError()
            if parsed.hostname not in self.server.allowed_hosts or port != self.server.server_port:
                raise ValueError()
        except ValueError:
            raise HTTPError(403, "invalid_host", "Host is not an allowed management address") from None
        return host.lower()

    def _origin(self, host):
        origins = self.headers.get_all("Origin", [])
        expected = ("https://" if self.app.tls else "http://") + host
        if len(origins) != 1 or origins[0].lower() != expected:
            raise HTTPError(403, "origin_rejected", "A same-origin request is required")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise HTTPError(403, "origin_rejected", "Cross-site requests are not allowed")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise HTTPError(400, "invalid_body", "Chunked request bodies are not supported")
        if len(self.headers.get_all("Content-Length", [])) != 1:
            self.close_connection = True
            raise HTTPError(411, "length_required", "A single Content-Length is required")
        try:
            length = int(self.headers["Content-Length"])
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise HTTPError(413, "body_too_large", "Request body exceeds the size limit")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.close_connection = True
            raise HTTPError(415, "content_type", "Use application/json")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            def invalid_constant(_):
                raise ValueError("Non-finite JSON value")
            value = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPError(400, "invalid_json", "Expected a JSON object") from None

    def _cookie_token(self):
        try:
            raw = self.headers.get("Cookie", "")
            if len(raw) > 4096:
                return ""
            parsed = cookies.SimpleCookie()
            parsed.load(raw)
            return parsed[self.app.cookie_name].value if self.app.cookie_name in parsed else ""
        except cookies.CookieError:
            return ""

    def _cookie(self, token, clear=False):
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s%s" % (
            self.app.cookie_name, token, 0 if clear else self.app.sessions.lifetime,
            "; Secure" if self.app.tls else "")

    def _session(self, required=True, admin=False):
        session = self.app.sessions.get(self._cookie_token())
        if session is None and required:
            raise HTTPError(401, "unauthorized", "Sign in to continue")
        if session is not None and admin and session.role != "admin":
            raise HTTPError(403, "forbidden", "Administrator privileges are required")
        return session

    def _csrf(self, session):
        token = self.headers.get("X-CSRF-Token", "")
        if (len(self.headers.get_all("X-CSRF-Token", [])) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
                or not secrets.compare_digest(session.csrf, token)):
            raise HTTPError(403, "csrf_rejected", "Invalid CSRF token")

    def _session_json(self, session):
        if session is None:
            return {"authenticated": False, "mode": self.app.mode}
        return {"authenticated": True, "user": session.user, "role": session.role,
                "csrf": session.csrf, "mode": self.app.mode}

    def _route(self):
        host = self._host()
        if len(self.path) > 4096:
            raise HTTPError(414, "uri_too_long", "Request path is too long")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise HTTPError(400, "invalid_path", "Use an origin-form path")
        try:
            path = unquote(parsed.path, encoding="utf-8", errors="strict")
        except UnicodeError:
            raise HTTPError(400, "invalid_path", "Invalid path encoding") from None
        if "\0" in path or "\\" in path or any(p == ".." for p in path.split("/")):
            raise HTTPError(403, "invalid_path", "Path is not allowed")
        cursor = 0
        if parsed.query and (path == "/api" or path.startswith("/api/")):
            if (self.command not in ("GET", "HEAD")
                    or not re.fullmatch(r"/api/consoles/" + CONSOLE_ID, path)
                    or not re.fullmatch(r"cursor=(0|[1-9][0-9]{0,15})", parsed.query)):
                raise HTTPError(400, "query_not_supported", "Only terminal output cursors may appear in API URLs")
            cursor = int(parsed.query.split("=", 1)[1])
        if self.command in ("GET", "HEAD"):
            return self._get(path, cursor)
        if self.command != "POST":
            raise HTTPError(405, "method_not_allowed", "Use GET or POST")
        self._origin(host)
        body = self._body()
        if path == "/api/login":
            if not self.app.rate.allow(self.client_address[0]):
                raise HTTPError(429, "rate_limited", "Too many login attempts; try again later")
            username, password = body.get("username"), body.pop("password", None)
            try:
                identity = self.app.auth.authenticate(username, password)
            except AuthError:
                self.app.audit.event("login_failed", address=self.client_address[0])
                raise
            finally:
                password = None
            old_token = self._cookie_token()
            self.app.consoles.close_owner(self.app.sessions.key(old_token))
            self.app.sessions.delete(old_token)
            token, session = self.app.sessions.create(identity["user"], identity["role"])
            self.app.audit.event("login", user=session.user, role=session.role, address=self.client_address[0])
            return self._json(200, self._session_json(session), self._cookie(token))
        console_route = re.fullmatch(r"/api/consoles/(" + CONSOLE_ID + r")/(input|resize|close)", path)
        if path not in ("/api/logout", "/api/plan", "/api/apply", "/api/consoles") and not console_route:
            raise HTTPError(404, "not_found", "Unknown API route")
        session = self._session(admin=path != "/api/logout")
        self._csrf(session)
        if path == "/api/logout":
            self.app.consoles.close_owner(session.key)
            self.app.sessions.delete(self._cookie_token())
            self.app.audit.event("logout", user=session.user)
            return self._json(200, self._session_json(None), self._cookie("", clear=True))
        if path == "/api/consoles":
            self._console_fields(body, {"target", "rows", "cols"}, {"target"})
            result = self.app.consoles.create(session.key, session.user, body["target"],
                                              rows=body.get("rows", 24), cols=body.get("cols", 80))
            self.app.audit.event("console_open", user=session.user, action=body["target"])
            return self._json(200, result)
        if console_route:
            terminal_id, action = console_route.groups()
            if action == "input":
                self._console_fields(body, {"data"}, {"data"})
                result = self.app.consoles.write(session.key, terminal_id, body["data"])
            elif action == "resize":
                self._console_fields(body, {"rows", "cols"}, {"rows", "cols"})
                result = self.app.consoles.resize(session.key, terminal_id, body["rows"], body["cols"])
            else:
                self._console_fields(body, set(), set())
                result = self.app.consoles.close(session.key, terminal_id)
                self.app.audit.event("console_close", user=session.user)
            return self._json(200, result or {})
        if path == "/api/plan":
            action, values, revision = body.get("action"), body.get("values"), body.get("revision")
            if not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", action) or not isinstance(values, dict):
                raise HTTPError(400, "invalid_plan", "An allowlisted action and values object are required")
            if revision is not None and (not isinstance(revision, str) or len(revision) > 128):
                raise HTTPError(400, "invalid_revision", "Invalid revision")
            plan = self.app.adapter.plan(action, values, revision)
            self.app.remember_plan(session, plan)
            self.app.audit.event("plan", user=session.user, action=action, plan_id=plan["id"])
            return self._json(200, plan)
        plan_id = body.get("plan_id")
        if body.get("acknowledged") is not True or not isinstance(plan_id, str) or len(plan_id) > 128:
            raise HTTPError(400, "confirmation_required", "Review and explicitly acknowledge the plan")
        return self._json(202, {"job_id": self.app.enqueue(session, plan_id)})

    @staticmethod
    def _console_fields(body, allowed, required):
        if set(body) - allowed or not required.issubset(body):
            raise HTTPError(400, "invalid_console_request", "Invalid terminal request fields")

    def _get(self, path, cursor=0):
        if path == "/api/health":
            return self._json(200, {"status": "ok", "mode": self.app.mode})
        if path == "/api/session":
            return self._json(200, self._session_json(self._session(required=False)))
        if path == "/api" or path.startswith("/api/"):
            session = self._session()
            if path == "/api/consoles/capabilities":
                self._session(admin=True)
                return self._json(200, self.app.consoles.capabilities(session.user))
            console_route = re.fullmatch(r"/api/consoles/(" + CONSOLE_ID + r")", path)
            if console_route:
                self._session(admin=True)
                return self._json(200, self.app.consoles.read(session.key, console_route[1], cursor))
            fixed = {"/api/catalog": self.app.adapter.catalog, "/api/overview": self.app.adapter.overview,
                     "/api/ports": self.app.adapter.ports, "/api/config/export": self.app.adapter.export_config}
            if path in fixed:
                return self._json(200, fixed[path]())
            if path.startswith("/api/ports/"):
                name = path[len("/api/ports/"):]
                if not re.fullmatch(r"[A-Za-z0-9_.:|/-]{1,128}", name):
                    raise HTTPError(400, "invalid_port", "Invalid interface identifier")
                return self._json(200, self.app.adapter.port_detail(name))
            if path.startswith("/api/pages/"):
                page = path[len("/api/pages/"):]
                if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", page):
                    raise HTTPError(400, "invalid_page", "Invalid page identifier")
                return self._json(200, self.app.adapter.page(page))
            if path.startswith("/api/jobs/"):
                self._session(admin=True)
                job_id = path[len("/api/jobs/"):]
                with self.app.lock:
                    job = self.app.jobs.get(job_id)
                    if not job or job["user"] != session.user:
                        raise HTTPError(404, "job_not_found", "Job not found")
                    result = {k: v for k, v in job.items() if k not in ("user", "plan_id", "action")}
                return self._json(200, result)
            raise HTTPError(404, "not_found", "Unknown API route")
        filename = self.app.static_dir / path.lstrip("/")
        try:
            resolved = filename.resolve()
            if not resolved.is_relative_to(self.app.static_dir):
                raise HTTPError(403, "invalid_path", "Path is outside the static directory")
            if not resolved.is_file():
                if Path(path).suffix:
                    raise HTTPError(404, "not_found", "Static file not found")
                resolved = (self.app.static_dir / "index.html").resolve()
            if not resolved.is_relative_to(self.app.static_dir) or not resolved.is_file():
                raise HTTPError(404, "not_found", "Frontend is not built")
            if resolved.stat().st_size > MAX_STATIC:
                raise HTTPError(413, "file_too_large", "Static file exceeds the size limit")
            payload = resolved.read_bytes()
        except OSError:
            raise HTTPError(404, "not_found", "Static file not found") from None
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self._headers(content_type, len(payload))
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _dispatch(self):
        try:
            self._route()
        except HTTPError as error:
            self.close_connection = True
            self._json(error.status, {"error": error.code, "message": str(error)})
        except AuthError as error:
            self._json(error.status, {"error": "authentication_failed", "message": str(error)})
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            try:
                self._json(408, {"error": "timeout", "message": "Request timed out"})
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            status = getattr(error, "status", 500)
            status = status if isinstance(status, int) and 400 <= status <= 599 else 500
            message = str(error)[:2000] if hasattr(error, "status") else "Internal service error"
            self.app.audit.event("request_error", status=status, error_type=type(error).__name__)
            self._json(status, {"error": "adapter_error" if hasattr(error, "status") else "internal_error", "message": message})

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch


def validate_bind(bind, demo):
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        raise ValueError("Bind must be an explicit IP address") from None
    if address.version != 4:
        raise ValueError("This deployment currently supports explicit IPv4 management binding")
    if demo:
        if not address.is_loopback:
            raise ValueError("Demo mode may bind only to loopback")
        return
    if address.is_unspecified or address.is_multicast or address.is_global:
        raise ValueError("Live mode must bind to a private management address or loopback")
    if address.is_loopback:
        return
    try:
        import fcntl
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
            value = fcntl.ioctl(handle.fileno(), 0x8915, struct.pack("256s", b"eth0"))
        management_ip = socket.inet_ntoa(value[20:24])
    except (ImportError, OSError):
        raise ValueError("Unable to verify the eth0 management address") from None
    if bind != management_ip:
        raise ValueError("Live bind must match eth0; do not bind a data-plane or wildcard address")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--static-dir", required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--pam-service", default="login")
    parser.add_argument("--admin-group", action="append")
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        validate_bind(args.bind, args.demo)
        if not 1 <= args.port <= 65535:
            raise ValueError("Invalid port")
        if bool(args.cert) != bool(args.key) or (not args.demo and not (args.cert and args.key)):
            raise ValueError("Live mode requires --cert and --key; certificate and key must be supplied together")
        for host in args.allowed_host:
            if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                raise ValueError("Invalid allowed host")
        context = None
        if args.cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.cert, args.key)
        from .sonic import SonicAdapter
        adapter = SonicAdapter(args.data_dir, demo=args.demo)
        authenticator = DemoAuthenticator() if args.demo else PamAuthenticator(
            service=args.pam_service, admin_groups=args.admin_group or ("sudo", "admin"))
        app = Application(adapter, args.data_dir, args.static_dir, args.demo, bool(context), authenticator)
        server = WebServer((args.bind, args.port), app, context, args.allowed_host)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("SONiC Web Manager: %s://%s:%s [%s]" % (
        "https" if context else "http", args.bind, args.port, "DEMO simulation only; demo/demo" if args.demo else "LIVE PAM"), flush=True)
    stopping = threading.Event()
    def stop_service(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            # BaseServer.shutdown must run outside the serve_forever thread.
            threading.Thread(target=server.shutdown, name="sonic-web-stop", daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
