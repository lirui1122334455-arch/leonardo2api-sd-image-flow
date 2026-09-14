"""Fetch public reference images without inheriting a broken system proxy route."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from curl_cffi import Curl, CurlInfo, CurlOpt
from curl_cffi.curl import CurlError
from PIL import Image, UnidentifiedImageError


REFERENCE_IMAGE_MAX_BYTES = 25 * 1024 * 1024
_MAX_REDIRECTS = 3
_MAX_REMOTE_IPS = 4
_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_SUPPORTED_IMAGE_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_DOH_ENDPOINTS = (
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
)


class ReferenceImageFetchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class _FetchResult:
    status_code: int
    content: bytes
    content_type: str
    redirect_url: str


def _validated_target(raw_url: str) -> tuple[str, int]:
    value = str(raw_url or "").strip()
    if not value or len(value) > 8192:
        raise ReferenceImageFetchError("invalid reference image URL", status_code=400)
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError as exc:
        raise ReferenceImageFetchError("invalid reference image URL", status_code=400) from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ReferenceImageFetchError("reference image URL must use HTTPS", status_code=400)
    if parsed.username or parsed.password or port != 443:
        raise ReferenceImageFetchError("reference image URL contains a disallowed authority", status_code=400)
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ReferenceImageFetchError("invalid reference image hostname", status_code=400) from exc
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        raise ReferenceImageFetchError("reference image hostname is not public", status_code=400)
    return hostname, port


def _public_ipv4(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(str(value or "").strip())
        except ValueError:
            continue
        if address.version != 4:
            continue
        if not address.is_global or address in _BENCHMARK_NETWORK:
            raise ReferenceImageFetchError("reference image hostname resolved to a non-public address", status_code=400)
        normalized = str(address)
        if normalized not in output:
            output.append(normalized)
    return output


async def _resolve_public_ipv4(hostname: str) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return _public_ipv4([str(literal)])

    timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        for endpoint in _DOH_ENDPOINTS:
            try:
                response = await client.get(
                    endpoint,
                    params={"name": hostname, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                )
                response.raise_for_status()
                payload = response.json()
                answers = payload.get("Answer") if isinstance(payload, dict) else None
                ips = _public_ipv4(
                    [str(item.get("data") or "") for item in (answers or []) if int(item.get("type") or 0) == 1]
                )
                if ips:
                    return ips[:_MAX_REMOTE_IPS]
            except ReferenceImageFetchError:
                raise
            except Exception:
                continue

    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ReferenceImageFetchError("reference image hostname could not be resolved") from exc
    ips = _public_ipv4([str(item[4][0]) for item in resolved])
    if not ips:
        raise ReferenceImageFetchError("reference image hostname has no public IPv4 address")
    return ips[:_MAX_REMOTE_IPS]


def _local_ipv4_interfaces() -> list[str]:
    try:
        rows = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
    except OSError:
        return []
    candidates: list[ipaddress.IPv4Address] = []
    for row in rows:
        try:
            address = ipaddress.ip_address(str(row[4][0]))
        except ValueError:
            continue
        if address.version != 4 or address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        if address in _BENCHMARK_NETWORK or address in candidates:
            continue
        candidates.append(address)

    def rank(address: ipaddress.IPv4Address) -> tuple[int, str]:
        if address in ipaddress.ip_network("192.168.0.0/16"):
            return 0, str(address)
        if address in ipaddress.ip_network("10.0.0.0/8"):
            return 1, str(address)
        if address in ipaddress.ip_network("172.16.0.0/12"):
            return 2, str(address)
        return 3, str(address)

    return [str(address) for address in sorted(candidates, key=rank)]


def _curl_fetch_once(url: str, hostname: str, remote_ip: str, interface: str | None, max_bytes: int) -> _FetchResult:
    body = BytesIO()
    too_large = False

    def write_chunk(chunk: bytes) -> int:
        nonlocal too_large
        if body.tell() + len(chunk) > max_bytes:
            too_large = True
            return 0
        return body.write(chunk)

    curl = Curl()
    try:
        curl.setopt(CurlOpt.URL, url.encode("utf-8"))
        curl.setopt(CurlOpt.WRITEFUNCTION, write_chunk)
        curl.setopt(CurlOpt.RESOLVE, [f"{hostname}:443:{remote_ip}".encode("ascii")])
        curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, 8000)
        curl.setopt(CurlOpt.TIMEOUT_MS, 45000)
        curl.setopt(CurlOpt.MAXFILESIZE_LARGE, max_bytes)
        curl.setopt(CurlOpt.FOLLOWLOCATION, 0)
        curl.setopt(CurlOpt.USERAGENT, b"fpbrowser2api-reference-fetch/1.0")
        curl.setopt(CurlOpt.ACCEPT_ENCODING, b"")
        if interface:
            curl.setopt(CurlOpt.INTERFACE, interface.encode("ascii"))
        curl.perform()
        content_type = curl.getinfo(CurlInfo.CONTENT_TYPE) or b""
        redirect_url = curl.getinfo(CurlInfo.REDIRECT_URL) or b""
        return _FetchResult(
            status_code=int(curl.getinfo(CurlInfo.RESPONSE_CODE) or 0),
            content=body.getvalue(),
            content_type=content_type.decode("latin-1", errors="replace") if isinstance(content_type, bytes) else str(content_type),
            redirect_url=redirect_url.decode("utf-8", errors="replace") if isinstance(redirect_url, bytes) else str(redirect_url),
        )
    except CurlError as exc:
        if too_large or int(getattr(exc, "code", 0) or 0) == 63:
            raise ReferenceImageFetchError("reference image exceeds the size limit", status_code=413) from exc
        raise
    finally:
        curl.close()


def _verified_image_mime(data: bytes, content_type: str) -> str:
    if not data:
        raise ReferenceImageFetchError("reference image download returned an empty body")
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ReferenceImageFetchError("reference URL did not return a valid image", status_code=415) from exc
    mime = _SUPPORTED_IMAGE_MIME.get(image_format)
    if not mime:
        source_mime = str(content_type or "").split(";", 1)[0].strip().lower()
        raise ReferenceImageFetchError(
            f"unsupported reference image type: {source_mime or image_format.lower()}",
            status_code=415,
        )
    return mime


async def download_reference_image(
    url: str,
    *,
    max_bytes: int = REFERENCE_IMAGE_MAX_BYTES,
) -> tuple[bytes, str]:
    current_url = str(url or "").strip()
    limit = max(1, min(int(max_bytes), REFERENCE_IMAGE_MAX_BYTES))
    last_error: Exception | None = None

    for redirect_index in range(_MAX_REDIRECTS + 1):
        hostname, _port = _validated_target(current_url)
        remote_ips = await _resolve_public_ipv4(hostname)
        interfaces: list[str | None] = [None, *_local_ipv4_interfaces()]
        redirect_target = ""

        for remote_ip in remote_ips:
            for interface in interfaces:
                try:
                    result = await asyncio.to_thread(
                        _curl_fetch_once,
                        current_url,
                        hostname,
                        remote_ip,
                        interface,
                        limit,
                    )
                except ReferenceImageFetchError:
                    raise
                except Exception as exc:
                    last_error = exc
                    continue

                if 300 <= result.status_code < 400 and result.redirect_url:
                    redirect_target = urljoin(current_url, result.redirect_url)
                    break
                if result.status_code < 200 or result.status_code >= 300:
                    raise ReferenceImageFetchError(f"reference image source returned HTTP {result.status_code}")
                mime = _verified_image_mime(result.content, result.content_type)
                return result.content, mime
            if redirect_target:
                break

        if redirect_target and redirect_index < _MAX_REDIRECTS:
            current_url = redirect_target
            continue
        if redirect_target:
            raise ReferenceImageFetchError("reference image redirected too many times")
        break

    detail = str(last_error or "all download routes failed").strip()
    if len(detail) > 180:
        detail = detail[:180]
    raise ReferenceImageFetchError(f"reference image download failed: {detail}")
