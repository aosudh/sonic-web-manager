#!/usr/bin/env python3
"""Read-only HTTP acceptance. Never calls plan/apply or changes switch config."""
import argparse
import atexit
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--user", default="demo")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--cafile")
    parser.add_argument("--output")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SystemExit("Use an origin URL without credentials/path/query")
    if args.mode == "live" and (parsed.scheme != "https" or not args.password_stdin):
        raise SystemExit("Live test requires HTTPS and password over stdin")
    context = ssl.create_default_context(cafile=args.cafile)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            return None  # Never forward authentication across an unexpected redirect.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(path, payload=None, csrf=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Accept": "application/json", "Origin": base}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        started = time.monotonic()
        with opener.open(urllib.request.Request(base + path, data=data, headers=headers), timeout=40) as response:
            value = json.load(response)
        return value, round(time.monotonic() - started, 3)

    health, _ = request("/api/health")
    if health.get("mode") != args.mode:
        raise SystemExit("Server mode differs from requested acceptance mode")
    password = sys.stdin.readline(1026).rstrip("\r\n") if args.password_stdin else "demo"
    if len(password.encode("utf-8")) > 1024:
        raise SystemExit("Password exceeds authentication limit")
    session, _ = request("/api/login", {"username": args.user, "password": password})
    password = None
    if session.get("authenticated") is not True:
        raise SystemExit("Login did not create authenticated session")
    def logout():
        try:
            request("/api/logout", {}, csrf=session.get("csrf"))
        except Exception:
            pass
    atexit.register(logout)
    report = {"mode": args.mode, "url": base, "checks": [], "pages": [], "writes_executed": False}
    overview, seconds = request("/api/overview")
    report["checks"].append({"name": "overview", "seconds": seconds, "version": overview.get("device", {}).get("version")})
    ports, seconds = request("/api/ports")
    report["checks"].append({"name": "ports", "seconds": seconds, "count": len(ports.get("items", [])), "layout": ports.get("layout")})
    for name in ["Ethernet52", "Ethernet64"]:
        if not any(item.get("name") == name for item in ports.get("items", [])):
            continue
        detail, seconds = request("/api/ports/" + name)
        report["checks"].append({"name": name, "seconds": seconds, "cage": detail.get("port", {}).get("cage_id")})
    catalog, _ = request("/api/catalog")
    for group in catalog.get("groups", []):
        for page in group.get("pages", []):
            entry = {"id": page["id"]}
            try:
                data, seconds = request("/api/pages/" + page["id"])
                entry.update(seconds=seconds, rows=len(data.get("rows", [])),
                             actions=len(data.get("actions", [])), capability=data.get("capability"),
                             valid_shape=all(key in data for key in ("id", "title", "columns", "rows", "capability", "revision")))
            except urllib.error.HTTPError as error:
                entry.update(error="HTTP " + str(error.code))
            except Exception as error:
                entry.update(error=type(error).__name__)
            report["pages"].append(entry)
    after, _ = request("/api/overview")
    report["revision_unchanged"] = after.get("revision") == overview.get("revision")
    report["failures"] = [item for item in report["pages"] if item.get("error") or not item.get("valid_shape")]
    # Deliberately omit session cookie, CSRF, username/password, rows and config contents.
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(result + "\n")
    print(result)
    logout()
    atexit.unregister(logout)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
