import unittest
from io import BytesIO
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.services import reference_image_fetcher as fetcher


class ReferenceImageValidationTests(unittest.TestCase):
    def test_accepts_public_https_url(self):
        self.assertEqual(
            fetcher._validated_target("https://cdn.example.com/reference.png"),
            ("cdn.example.com", 443),
        )

    def test_rejects_non_https_and_nonstandard_port(self):
        with self.assertRaises(fetcher.ReferenceImageFetchError):
            fetcher._validated_target("http://cdn.example.com/reference.png")
        with self.assertRaises(fetcher.ReferenceImageFetchError):
            fetcher._validated_target("https://cdn.example.com:8443/reference.png")

    def test_rejects_private_and_fake_ips(self):
        for value in ("127.0.0.1", "10.0.0.2", "169.254.1.2", "198.18.0.129"):
            with self.subTest(value=value), self.assertRaises(fetcher.ReferenceImageFetchError):
                fetcher._public_ipv4([value])

    def test_prioritizes_physical_lan_addresses(self):
        rows = [
            (None, None, None, None, ("172.19.112.1", 0)),
            (None, None, None, None, ("198.18.0.1", 0)),
            (None, None, None, None, ("192.168.0.15", 0)),
        ]
        with patch.object(fetcher.socket, "getaddrinfo", return_value=rows):
            self.assertEqual(fetcher._local_ipv4_interfaces(), ["192.168.0.15", "172.19.112.1"])


class ReferenceImageDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_bound_interface(self):
        output = BytesIO()
        Image.new("RGB", (1, 1), "red").save(output, format="PNG")
        png = output.getvalue()
        success = fetcher._FetchResult(200, png, "image/png", "")

        def fake_fetch(_url, _hostname, _remote_ip, interface, _max_bytes):
            if interface is None:
                raise RuntimeError("proxy TLS failure")
            return success

        with (
            patch.object(fetcher, "_resolve_public_ipv4", new=AsyncMock(return_value=["203.0.113.10"])),
            patch.object(fetcher, "_local_ipv4_interfaces", return_value=["192.168.0.15"]),
            patch.object(fetcher, "_curl_fetch_once", side_effect=fake_fetch) as mocked_fetch,
        ):
            data, mime = await fetcher.download_reference_image("https://cdn.example.com/reference.png")

        self.assertEqual(data, png)
        self.assertEqual(mime, "image/png")
        self.assertEqual(mocked_fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
