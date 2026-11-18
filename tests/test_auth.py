import ctypes
import os
import types
import unittest
from unittest import mock

from server.auth import AuthError, DemoAuthenticator, RateLimiter, SessionStore, _pam_check, local_role, validate_credentials


class AuthTests(unittest.TestCase):
    def test_console_owner_check_does_not_renew_expiry(self):
        clock = [0.0]
        sessions = SessionStore(idle_seconds=10, clock=lambda: clock[0])
        token, session = sessions.create("admin", "admin")
        clock[0] = 9.0
        self.assertTrue(sessions.is_active_key(session.key))
        self.assertEqual(session.seen, 0.0)
        clock[0] = 10.0
        self.assertFalse(sessions.is_active_key(session.key))
        self.assertIsNone(sessions.get(token))

    def test_credentials_and_explicit_demo(self):
        self.assertEqual(DemoAuthenticator().authenticate("demo", "demo"), {"user": "demo", "role": "admin"})
        for user, password in (("demo", "wrong"), ("admin", "demo"), ("../root", "secret"), ("admin", "a\0b"), ("admin", "x"*1025)):
            with self.assertRaises(AuthError):
                DemoAuthenticator().authenticate(user, password)

    def test_sessions_rotate_expire_and_store_only_token_hash(self):
        clock = [100.0]
        sessions = SessionStore(idle_seconds=10, lifetime_seconds=30, limit=2, clock=lambda: clock[0])
        token, first = sessions.create("admin", "admin")
        self.assertNotIn(token, sessions._sessions)
        self.assertEqual(sessions.get(token).user, "admin")
        other_token, second = sessions.create("admin", "admin")
        self.assertNotEqual(token, other_token)
        self.assertNotEqual(first.csrf, second.csrf)
        clock[0] += 11
        self.assertIsNone(sessions.get(token))
        token, _ = sessions.create("viewer", "viewer")
        for _ in range(3):
            clock[0] += 9
            self.assertIsNotNone(sessions.get(token))
        clock[0] += 4
        self.assertIsNone(sessions.get(token))

    def test_session_limit_delete_and_rate_window(self):
        sessions = SessionStore(limit=1)
        first, _ = sessions.create("one", "viewer")
        second, _ = sessions.create("two", "admin")
        self.assertIsNone(sessions.get(first))
        sessions.delete(second)
        self.assertIsNone(sessions.get(second))
        clock = [0.0]
        rate = RateLimiter(attempts=2, window=5, clock=lambda: clock[0])
        self.assertTrue(rate.allow("ip"))
        self.assertTrue(rate.allow("ip"))
        self.assertFalse(rate.allow("ip"))
        clock[0] = 5
        self.assertTrue(rate.allow("ip"))

    def test_roles_are_derived_from_local_uid_and_groups(self):
        account = types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_shell="/bin/bash")
        fake_pwd = types.SimpleNamespace(getpwnam=lambda _: account)
        fake_grp = types.SimpleNamespace(getgrgid=lambda gid: types.SimpleNamespace(gr_name="sudo" if gid == 27 else "users"))
        with mock.patch.dict("sys.modules", {"pwd": fake_pwd, "grp": fake_grp}), mock.patch("os.getgrouplist", create=True, return_value=[1000]):
            self.assertEqual(local_role("someone"), "viewer")
            with mock.patch("os.getgrouplist", create=True, return_value=[1000, 27]):
                self.assertEqual(local_role("someone"), "admin")
            account.pw_uid = 0
            self.assertEqual(local_role("root"), "admin")
            account.pw_shell = "/usr/sbin/nologin"
            with self.assertRaises(AuthError):
                local_role("service")

    @unittest.skipUnless(os.name == "posix", "ctypes PAM ABI mock uses libc allocation")
    def test_pam_conversation_memory_and_account_expiry(self):
        memory = ctypes.CDLL(None)
        memory.free.argtypes = [ctypes.c_void_p]
        memory.free.restype = None

        class Function:
            def __init__(self, call): self.call = call
            def __call__(self, *args): return self.call(*args)

        class FakePam:
            def __init__(self, account_code):
                self.account_code, self.ended, self.prompts = account_code, [], []
                self.pam_start = Function(self.start)
                self.pam_authenticate = Function(self.authenticate)
                self.pam_acct_mgmt = Function(lambda *_: account_code)
                self.pam_end = Function(lambda handle, code: self.ended.append(code) or 0)

            def start(self, service, user, conversation, handle):
                self.callback = conversation._obj.conv
                handle._obj.value = 1
                return 0

            def authenticate(self, handle, flags):
                message_type = self.callback._argtypes_[1]._type_._type_
                response_type = self.callback._argtypes_[2]._type_._type_
                messages = [message_type(2, b"User:"), message_type(1, b"Password:")]
                pointers = (ctypes.POINTER(message_type)*2)(*[ctypes.pointer(m) for m in messages])
                response = ctypes.POINTER(response_type)()
                result = self.callback(2, pointers, ctypes.byref(response), None)
                if result == 0:
                    self.prompts = [ctypes.string_at(response[i].resp).decode() for i in range(2)]
                    for i in range(2): memory.free(response[i].resp)
                    memory.free(ctypes.cast(response, ctypes.c_void_p))
                return result

        pam = FakePam(0)
        self.assertTrue(_pam_check("admin", "synthetic-secret", "login", pam, memory))
        self.assertEqual(pam.prompts, ["admin", "synthetic-secret"])
        self.assertEqual(pam.ended, [0])
        expired = FakePam(12)
        self.assertFalse(_pam_check("admin", "synthetic-secret", "login", expired, memory))
        self.assertEqual(expired.ended, [12])


if __name__ == "__main__":
    unittest.main()
