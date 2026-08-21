import base64
import json
import time
import unittest

from src.services.leonardo_cookie_probe import (
    _build_proxy_url,
    _cookie_header,
    _find_best_jwt,
    _jwt_metadata,
    _safe_error,
)


def make_jwt(*, token_use: str, exp: int) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return ".".join(
        [
            encode({"alg": "none", "typ": "JWT"}),
            encode(
                {
                    "iss": "https://cognito-idp.us-east-1.amazonaws.com/example",
                    "token_use": token_use,
                    "exp": exp,
                }
            ),
            "signature",
        ]
    )


class LeonardoCookieProbeTests(unittest.TestCase):
    def test_prefers_access_token_with_later_expiry(self):
        now = int(time.time())
        id_token = make_jwt(token_use="id", exp=now + 7200)
        access_token = make_jwt(token_use="access", exp=now + 3600)
        found = _find_best_jwt({"session": {"idToken": id_token, "accessToken": access_token}})
        self.assertEqual(found, access_token)
        self.assertEqual(_jwt_metadata(found)["token_use"], "access")

    def test_cookie_header_excludes_expired_and_non_leonardo(self):
        now = time.time()
        header, names = _cookie_header(
            [
                {"name": "session", "value": "ok", "domain": ".leonardo.ai", "path": "/", "expires": now + 60},
                {"name": "expired", "value": "no", "domain": ".leonardo.ai", "path": "/", "expires": now - 60},
                {"name": "other", "value": "no", "domain": ".example.com", "path": "/", "expires": now + 60},
            ]
        )
        self.assertEqual(header, "session=ok")
        self.assertEqual(names, ("session",))

    def test_proxy_url_quotes_credentials(self):
        url, protocol, last_ip = _build_proxy_url(
            {
                "proxyInfo": {
                    "protocol": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "proxyUserName": "user@example.com",
                    "proxyPassword": "a:b",
                    "lastIp": "1.2.3.4",
                }
            }
        )
        self.assertEqual(protocol, "socks5")
        self.assertEqual(last_ip, "1.2.3.4")
        self.assertEqual(url, "socks5://user%40example.com:a%3Ab@127.0.0.1:1080")

    def test_error_redacts_proxy_and_bearer(self):
        message = _safe_error("failed https://user:pass@proxy.test Bearer abc.def.ghi cookie=secret")
        self.assertNotIn("user:pass", message)
        self.assertNotIn("abc.def.ghi", message)
        self.assertNotIn("secret", message)


if __name__ == "__main__":
    unittest.main()
