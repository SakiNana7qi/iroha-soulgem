import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from fan_auth import FanControlPassword, password_record
import main
from tools import set_fan_password


PASSWORD = "测试风扇密码-long-enough"


class PasswordFileTests(unittest.TestCase):
    def test_hash_is_salted_and_accepts_unicode_password(self):
        first = password_record(PASSWORD)
        second = password_record(PASSWORD)
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotIn(PASSWORD, json.dumps(first))
        verifier = FanControlPassword(first)
        self.assertTrue(verifier.verify(PASSWORD))
        self.assertFalse(verifier.verify("wrong password"))

    def test_missing_or_invalid_file_locks_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password.json"
            self.assertIsNone(FanControlPassword.load(path))
            for invalid in ("invalid JSON", "{}", "null", '{"algorithm":"plaintext"}'):
                with self.subTest(invalid=invalid), patch("fan_auth.logging.getLogger"):
                    path.write_text(invalid, encoding="utf-8")
                    self.assertIsNone(FanControlPassword.load(path))

    def test_setup_saves_only_hash_and_can_replace_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password.json"
            for password in (PASSWORD, "replacement-password"):
                with patch.object(set_fan_password, "PASSWORD_PATH", path), patch("sys.argv", ["set_fan_password.py"]), patch.object(set_fan_password.getpass, "getpass", side_effect=[password, password]), patch("builtins.print"):
                    self.assertEqual(set_fan_password.main(), 0)
                self.assertNotIn(password, path.read_text(encoding="utf-8"))
                self.assertTrue(FanControlPassword.load(path).verify(password))
                self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_setup_rejects_short_or_mismatched_password_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password.json"
            original = json.dumps(password_record(PASSWORD))
            path.write_text(original, encoding="utf-8")
            for prompts in (["short"], ["x" * 1025], ["new-long-password", "does-not-match"]):
                with self.subTest(prompts=prompts), patch.object(set_fan_password, "PASSWORD_PATH", path), patch("sys.argv", ["set_fan_password.py"]), patch.object(set_fan_password.getpass, "getpass", side_effect=prompts), patch("sys.stderr"):
                    with self.assertRaises(SystemExit):
                        set_fan_password.main()
                self.assertEqual(path.read_text(encoding="utf-8"), original)


class ProtectedApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = password_record(PASSWORD)

    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.verifier = FanControlPassword(self.record)
        self.stack.enter_context(patch.object(main, "fan_password", self.verifier))
        self.stack.enter_context(patch.object(main, "_fan_auth_lock", asyncio.Lock()))
        self.stack.enter_context(patch.object(main, "fanctl_enabled", True))
        self.stack.enter_context(patch.object(main.fanctl, "_get_ipmi", side_effect=AssertionError("Real hardware must not be contacted")))
        self.set_mode = self.stack.enter_context(patch.object(main.fanctl, "set_mode"))
        self.set_pwm = self.stack.enter_context(patch.object(main.fanctl, "set_manual_pwm"))
        # ASGITransport does not invoke lifespan, so no collectors start.
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)
        self.writes = (
            ("/api/fanctl/mode", {"mode": "manual"}),
            ("/api/fanctl/pwm", {"zones": {"0": 25}}),
        )

    async def test_missing_password_blocks_both_actual_http_routes(self):
        for path, body in self.writes:
            with self.subTest(path=path):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 401)
        self.set_mode.assert_not_called()
        self.set_pwm.assert_not_called()

    async def test_wrong_password_blocks_both_routes(self):
        for path, body in self.writes:
            response = await self.client.post(path, json={**body, "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        self.set_mode.assert_not_called()
        self.set_pwm.assert_not_called()

    async def test_correct_password_allows_both_routes(self):
        for path, body in self.writes:
            response = await self.client.post(path, json={**body, "password": PASSWORD})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            self.assertNotIn(PASSWORD, response.text)
        self.set_mode.assert_called_once_with("manual")
        self.set_pwm.assert_called_once_with({0: 25})

    async def test_unconfigured_password_fails_closed(self):
        with patch.object(main, "fan_password", None):
            for path, body in self.writes:
                response = await self.client.post(path, json={**body, "password": PASSWORD})
                self.assertEqual(response.status_code, 503)
        self.set_mode.assert_not_called()
        self.set_pwm.assert_not_called()

    async def test_empty_null_and_invalid_passwords_never_write(self):
        for password, status in (("", 401), (None, 401), ({}, 422), ("x" * 1025, 422)):
            response = await self.client.post("/api/fanctl/mode", json={"mode": "full", "password": password})
            self.assertEqual(response.status_code, status)
        self.set_mode.assert_not_called()

    async def test_monitor_reads_remain_public_and_do_not_expose_password_hash(self):
        snapshot = {"fanctl": main.fanctl.get_state()}
        with patch.object(main, "_latest_snapshot", snapshot):
            for path in ("/", "/api/status", "/api/fanctl"):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200)
                for secret in (PASSWORD, self.record["salt"], self.record["digest"]):
                    self.assertNotIn(secret, response.text)

    async def test_invalid_zone_still_returns_400_after_authentication(self):
        self.set_pwm.side_effect = ValueError("Unknown fan zones")
        response = await self.client.post("/api/fanctl/pwm", json={"zones": {99: 20}, "password": PASSWORD})
        self.assertEqual(response.status_code, 400)

    async def test_disabled_fan_control_stays_disabled(self):
        with patch.object(main, "fanctl_enabled", False):
            response = await self.client.post("/api/fanctl/mode", json={"mode": "full", "password": PASSWORD})
        self.assertEqual(response.status_code, 404)
        self.set_mode.assert_not_called()

    async def test_expensive_verification_does_not_block_api(self):
        started = threading.Event()
        release = threading.Event()

        def slow_verify(password):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Fake verification was not released")
            return True

        with patch.object(self.verifier, "verify", side_effect=slow_verify):
            task = asyncio.create_task(main.require_fan_password(SecretStr(PASSWORD)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                response = await asyncio.wait_for(self.client.get("/api/fanctl"), timeout=0.5)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task


if __name__ == "__main__":
    unittest.main()
