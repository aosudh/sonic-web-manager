import base64
import datetime
import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest

from server.app import Application, WebServer, validate_bind
from server.auth import AuthError, DemoAuthenticator


class FakeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class FakeAuth:
    def authenticate(self, user, password):
        if user not in ("admin", "other", "viewer") or password != "test-secret-password":
            raise AuthError()
        return {"user": user, "role": "viewer" if user == "viewer" else "admin"}


class FakeAdapter:
    def __init__(self):
        self.applied = []
        self.plans = {}

    def catalog(self):
        return {"groups": [], "mode": "demo"}

    def overview(self):
        return {"device": {"hostname": "test-switch"}, "mode": "demo"}

    def ports(self):
        return {"items": [{"name": "Ethernet52", "alias": "QSFP2"}]}

    def port_detail(self, name):
        if name != "Ethernet52":
            raise FakeError("Unknown interface", 404)
        return {"port": {"name": name}}

    def page(self, page):
        return {"id": page, "rows": []}

    def export_config(self):
        return {"SNMP": {"community": "***"}}

    def plan(self, action, values, revision):
        if revision == "stale":
            raise FakeError("Configuration changed", 409)
        if action not in ("port.configure", "config.save"):
            raise FakeError("Unsupported action", 400)
        key = secrets.token_urlsafe(24)
        plan = {"id": key, "action": action, "title": "Review change", "changes": [],
                "requires_confirmation": True, "revision": "v1",
                "expires_at": (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=5)).isoformat()}
        self.plans[key] = plan
        return plan

    def apply(self, plan_id):
        self.applied.append(plan_id)
        return {"success": True, "message": "Simulated", "changed": True, "pending_save": True}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.static = self.base / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>Demo</title>")
        (self.static / "app.js").write_text("console.log('local');")
        (self.base / "secret.txt").write_text("MUST-NOT-LEAK")
        self.adapter = FakeAdapter()
        self.app = Application(self.adapter, self.base / "data", self.static, demo=True, authenticator=FakeAuth())
        self.server = WebServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, cookie=None, csrf=None, headers=None, origin=True, raw=None):
        fields = {}
        payload = raw
        if method == "POST":
            fields["Content-Type"] = "application/json"
            if origin:
                fields["Origin"] = f"http://127.0.0.1:{self.port}"
            if raw is None:
                payload = json.dumps(body if body is not None else {})
        if cookie:
            fields["Cookie"] = cookie
        if csrf:
            fields["X-CSRF-Token"] = csrf
        fields.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=payload, headers=fields)
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        code = response.status
        connection.close()
        try:
            value = json.loads(data)
        except ValueError:
            value = data.decode()
        return code, value, response_headers

    def login(self, user="admin"):
        status, value, headers = self.request("POST", "/api/login", {"username": user, "password": "test-secret-password"})
        self.assertEqual(status, 200, value)
        return headers["Set-Cookie"].split(";", 1)[0], value["csrf"]

    def plan(self, cookie, csrf):
        status, value, _ = self.request("POST", "/api/plan", {"action": "port.configure", "values": {"mtu": 9100}, "revision": "v1"}, cookie, csrf)
        self.assertEqual(status, 200, value)
        return value["id"]

    def test_session_login_cookie_logout_and_no_secret_audit(self):
        code, value, _ = self.request("GET", "/api/session")
        self.assertEqual((code, value), (200, {"authenticated": False, "mode": "demo"}))
        code, value, headers = self.request("POST", "/api/login", {"username": "admin", "password": "test-secret-password"})
        self.assertEqual(code, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/overview", cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", "/api/logout", cookie=cookie, csrf=value["csrf"])[0], 200)
        self.assertEqual(self.request("GET", "/api/overview", cookie=cookie)[0], 401)
        audit = (self.base / "data/audit.jsonl").read_text()
        self.assertNotIn("test-secret-password", audit)
        self.assertNotIn(value["csrf"], audit)
        self.assertNotIn(cookie.split("=", 1)[1], audit)

    def test_shutdown_finishes_running_job_and_cancels_unstarted_queue(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        def blocking_apply(plan_id):
            started.set()
            release.wait(3)
            return {"success": True, "changed": True, "pending_save": True}
        self.adapter.apply = blocking_apply
        cookie, csrf = self.login()
        first = self.plan(cookie, csrf)
        code, first_job, _ = self.request("POST", "/api/apply", {"plan_id": first, "acknowledged": True}, cookie, csrf)
        self.assertEqual(code, 202)
        self.assertTrue(started.wait(2))
        second = self.plan(cookie, csrf)
        code, second_job, _ = self.request("POST", "/api/apply", {"plan_id": second, "acknowledged": True}, cookie, csrf)
        self.assertEqual(code, 202)
        closer = threading.Thread(target=lambda: (self.app.close(), closed.set()))
        closer.start()
        try:
            self.assertFalse(closed.wait(0.05))
            self.assertEqual(self.app.jobs[second_job["job_id"]]["status"], "failed")
        finally:
            release.set()
            closer.join(3)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.app.jobs[first_job["job_id"]]["status"], "succeeded")

    def test_csrf_origin_and_host_are_enforced(self):
        cookie, csrf = self.login()
        body = {"action": "config.save", "values": {}}
        for bad_csrf in (None, "invalid"):
            self.assertEqual(self.request("POST", "/api/plan", body, cookie, bad_csrf)[0], 403)
        self.assertEqual(self.request("POST", "/api/plan", body, cookie, csrf, origin=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/plan", body, cookie, csrf, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/health", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.adapter.applied, [])

    def test_viewer_reads_but_cannot_plan_or_apply(self):
        cookie, csrf = self.login("viewer")
        self.assertEqual(self.request("GET", "/api/ports", cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", "/api/plan", {"action": "config.save", "values": {}}, cookie, csrf)[0], 403)
        self.assertEqual(self.request("POST", "/api/apply", {"plan_id": "whatever", "acknowledged": True}, cookie, csrf)[0], 403)

    def test_plans_are_session_bound_confirmed_once_and_jobs_private(self):
        cookie, csrf = self.login()
        plan_id = self.plan(cookie, csrf)
        second, second_csrf = self.login("other")
        apply = {"plan_id": plan_id, "acknowledged": True}
        self.assertEqual(self.request("POST", "/api/apply", apply, second, second_csrf)[0], 404)
        self.assertEqual(self.request("POST", "/api/apply", {"plan_id": plan_id}, cookie, csrf)[0], 400)
        code, value, _ = self.request("POST", "/api/apply", apply, cookie, csrf)
        self.assertEqual(code, 202)
        self.assertEqual(self.request("POST", "/api/apply", apply, cookie, csrf)[0], 404)
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            status, job, _ = self.request("GET", "/api/jobs/"+value["job_id"], cookie=cookie)
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.01)
        self.assertEqual((status, job["status"]), (200, "succeeded"))
        self.assertEqual(self.adapter.applied, [plan_id])
        self.assertEqual(self.request("GET", "/api/jobs/"+value["job_id"], cookie=second)[0], 404)

    def test_stale_expired_and_unsupported_plans_rejected(self):
        cookie, csrf = self.login()
        self.assertEqual(self.request("POST", "/api/plan", {"action": "config.save", "values": {}, "revision": "stale"}, cookie, csrf)[0], 409)
        self.assertEqual(self.request("POST", "/api/plan", {"action": "shell.exec", "values": {}}, cookie, csrf)[0], 400)
        key = self.plan(cookie, csrf)
        self.app.plans[key]["deadline"] = time.monotonic()-1
        self.assertEqual(self.request("POST", "/api/apply", {"plan_id": key, "acknowledged": True}, cookie, csrf)[0], 409)

    def test_partial_apply_failure_is_not_reported_as_success(self):
        cookie, csrf = self.login()
        key = self.plan(cookie, csrf)
        self.adapter.apply = lambda _: {"success": False, "message": "Partial change; inspect device", "changed": True, "pending_save": True}
        code, value, _ = self.request("POST", "/api/apply", {"plan_id": key, "acknowledged": True}, cookie, csrf)
        self.assertEqual(code, 202)
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            _, job, _ = self.request("GET", "/api/jobs/"+value["job_id"], cookie=cookie)
            if job["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["result"]["changed"])
        self.assertTrue(job["result"]["pending_save"])
        self.assertIn("Partial change", job["error"])

    def test_unknown_api_and_invalid_json_never_return_spa(self):
        cookie, csrf = self.login()
        code, value, headers = self.request("GET", "/api/not-real", cookie=cookie)
        self.assertEqual(code, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("error", value)
        self.assertEqual(self.request("POST", "/api/plan", cookie=cookie, csrf=csrf, raw="[]")[0], 400)
        self.assertEqual(self.request("POST", "/api/plan", cookie=cookie, csrf=csrf, raw='{"values":NaN}')[0], 400)
        self.assertEqual(self.request("POST", "/api/plan", cookie=cookie, csrf=csrf, raw="x"*65537)[0], 413)
        self.assertEqual(self.request("DELETE", "/api/ports", cookie=cookie)[0], 405)

    def test_static_fallback_traversal_and_mime(self):
        self.assertEqual(self.request("GET", "/ports/Ethernet52")[0], 200)
        code, _, headers = self.request("GET", "/app.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%5csecret.txt"):
            code, value, _ = self.request("GET", path)
            self.assertEqual(code, 403)
            self.assertNotIn("MUST-NOT-LEAK", str(value))
        try:
            (self.static / "escape.txt").symlink_to(self.base / "secret.txt")
        except OSError:
            pass
        else:
            self.assertEqual(self.request("GET", "/escape.txt")[0], 403)
        self.assertEqual(self.request("GET", "/missing.js")[0], 404)

    def test_demo_is_explicit_and_live_cannot_use_demo_auth(self):
        with self.assertRaises(ValueError):
            Application(self.adapter, self.base / "x", self.static, demo=False, tls=False)
        with self.assertRaises(ValueError):
            Application(self.adapter, self.base / "x", self.static, demo=False, tls=True, authenticator=DemoAuthenticator())
        for address in ("0.0.0.0", "192.0.2.18", "8.8.8.8"):
            with self.assertRaises(ValueError):
                validate_bind(address, demo=True)
        validate_bind("127.0.0.1", demo=True)
        with self.assertRaises(ValueError):
            validate_bind("0.0.0.0", demo=False)

    def test_login_requires_origin_and_rate_is_bounded(self):
        self.assertEqual(self.request("POST", "/api/login?password=never-in-url", {"username": "admin", "password": "test-secret-password"})[0], 400)
        self.assertEqual(self.request("POST", "/api/login", {"username": "admin", "password": "test-secret-password"}, origin=False)[0], 403)
        self.app.rate.attempts = 1
        self.assertEqual(self.request("POST", "/api/login", {"username": "admin", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"username": "admin", "password": "wrong"})[0], 429)

    def test_console_auth_csrf_query_and_fixed_targets(self):
        self.assertEqual(self.request("GET", "/api/consoles/capabilities")[0], 401)
        viewer, viewer_csrf = self.login("viewer")
        self.assertEqual(self.request("GET", "/api/consoles/capabilities", cookie=viewer)[0], 403)
        self.assertEqual(self.request("POST", "/api/consoles", {"target": "sonic"}, viewer, viewer_csrf)[0], 403)
        cookie, csrf = self.login()
        self.assertEqual(self.request("GET", "/api/consoles/capabilities", cookie=cookie)[0], 200)
        for body in ({"target": "evil"}, {"target": "sonic", "user": "root"},
                     {"target": "bmc", "host": "1.2.3.4"}, {"target": "sonic", "rows": True}):
            self.assertEqual(self.request("POST", "/api/consoles", body, cookie, csrf)[0], 400)
        self.assertEqual(self.request("POST", "/api/consoles", {"target": "sonic"}, cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/consoles", {"target": "sonic"}, cookie, csrf, origin=False)[0], 403)
        code, session, _ = self.request("POST", "/api/consoles", {"target": "sonic"}, cookie, csrf)
        self.assertEqual(code, 200, session)
        path = "/api/consoles/" + session["id"]
        self.assertEqual(self.request("GET", path + "?cursor=0", cookie=cookie)[0], 200)
        for query in ("cursor=-1", "cursor=0&cursor=1", "cursor=0&data=secret", "cursor=x", "cursor=00"):
            self.assertEqual(self.request("GET", path + "?" + query, cookie=cookie)[0], 400)
        self.assertEqual(self.request("POST", path + "/input?cursor=0", {}, cookie, csrf)[0], 400)
        self.assertEqual(self.request("POST", path + "/input", {"data": "%%%"}, cookie, csrf)[0], 400)
        self.assertEqual(self.request("POST", path + "/resize", {"rows": 35, "cols": 100}, cookie, csrf)[0], 200)
        secret = "terminal-private-value"
        self.assertEqual(self.request("POST", path + "/input", {"data": base64.b64encode((secret + "\n").encode()).decode()}, cookie, csrf)[0], 200)
        audit = (self.base / "data/audit.jsonl").read_text()
        self.assertNotIn(secret, audit)
        self.assertNotIn(base64.b64encode((secret + "\n").encode()).decode(), audit)

    def test_console_sessions_are_private_and_logout_closes_them(self):
        cookie, csrf = self.login()
        code, terminal, _ = self.request("POST", "/api/consoles", {"target": "sonic"}, cookie, csrf)
        self.assertEqual(code, 200)
        identifier = terminal["id"]
        terminal_state = self.app.consoles._sessions[identifier]
        path = "/api/consoles/" + identifier
        other, other_csrf = self.login()  # Same username, distinct browser session.
        self.assertEqual(self.request("GET", path, cookie=other)[0], 404)
        for action, body in (("input", {"data": "Cg=="}), ("resize", {"rows": 25, "cols": 80}), ("close", {})):
            self.assertEqual(self.request("POST", path + "/" + action, body, other, other_csrf)[0], 404)
        self.assertEqual(self.request("POST", "/api/logout", {}, cookie, csrf)[0], 200)
        self.assertTrue(terminal_state.closed)
        self.assertEqual(self.request("GET", path, cookie=cookie)[0], 401)

    def test_console_relogin_and_service_shutdown_cleanup(self):
        cookie, csrf = self.login()
        _, first, _ = self.request("POST", "/api/consoles", {"target": "sonic"}, cookie, csrf)
        first_state = self.app.consoles._sessions[first["id"]]
        code, replacement, headers = self.request("POST", "/api/login", {"username": "admin", "password": "test-secret-password"}, cookie)
        self.assertEqual(code, 200)
        self.assertTrue(first_state.closed)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        _, second, _ = self.request("POST", "/api/consoles", {"target": "bmc"}, cookie, replacement["csrf"])
        second_state = self.app.consoles._sessions[second["id"]]
        self.app.close()
        self.assertTrue(second_state.closed)


if __name__ == "__main__":
    unittest.main()
