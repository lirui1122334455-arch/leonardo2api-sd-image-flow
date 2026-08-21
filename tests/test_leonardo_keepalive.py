import time
import unittest

from src.services.leonardo_task_executor import (
    LEONARDO_BETTER_AUTH_DATA_PREFIX,
    LEONARDO_BETTER_AUTH_SESSION_COOKIE,
    _leonardo_auth_session_probe,
    _leonardo_cookie_session_state,
)


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, _url):
        return self._cookies


class _CookiePage:
    def __init__(self, cookies):
        self.context = _FakeContext(cookies)


class _EvaluatePage:
    def __init__(self):
        self.expression = ""
        self.argument = None
        self.now = int(time.time())

    async def evaluate(self, expression, argument):
        self.expression = expression
        self.argument = argument
        return {
            "status": 200,
            "hasSession": True,
            "hasUser": True,
            "empty": False,
            "sessionExpiresAt": self.now + 600,
            "sessionUpdatedAt": self.now - 30,
            "sessionCreatedAt": self.now - 120,
        }


class LeonardoKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_state_reports_session_ttl_without_values(self):
        expires = time.time() + 600
        page = _CookiePage(
            [
                {
                    "name": LEONARDO_BETTER_AUTH_SESSION_COOKIE,
                    "value": "secret-session-value",
                    "expires": expires,
                },
                {
                    "name": f"{LEONARDO_BETTER_AUTH_DATA_PREFIX}.0",
                    "value": "secret-data-value",
                    "expires": expires - 60,
                },
            ]
        )

        state = await _leonardo_cookie_session_state(page)

        self.assertTrue(state["better_auth_session_present"])
        self.assertGreaterEqual(state["better_auth_session_ttl_seconds"], 598)
        self.assertLessEqual(state["better_auth_session_ttl_seconds"], 600)
        self.assertNotIn("secret-session-value", str(state))
        self.assertNotIn("secret-data-value", str(state))

    async def test_auth_probe_bypasses_cookie_cache(self):
        page = _EvaluatePage()

        result = await _leonardo_auth_session_probe(
            page,
            timeout_seconds=3,
            disable_cookie_cache=True,
        )

        self.assertTrue(result["authenticated"])
        self.assertTrue(result["cookie_cache_bypassed"])
        self.assertEqual(page.argument, {"disableCookieCache": True})
        self.assertIn("disableCookieCache=true", page.expression)
        self.assertIn("cache: 'no-store'", page.expression)
        self.assertGreaterEqual(result["session_expires_in_seconds"], 599)
        self.assertLessEqual(result["session_expires_in_seconds"], 600)
        self.assertGreaterEqual(result["session_updated_age_seconds"], 30)
        self.assertGreaterEqual(result["session_age_seconds"], 120)


if __name__ == "__main__":
    unittest.main()
