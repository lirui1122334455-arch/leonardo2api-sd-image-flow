"""阿里云 OSS 上传（VEO 2K 等为字节流上传，非本地文件路径）。"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import oss2  # type: ignore
except Exception:  # pragma: no cover
    oss2 = None


@dataclass(frozen=True)
class OssConfig:
    enabled: bool
    endpoint: str
    region: str
    bucket: str
    public_base_url: str
    access_key_id: str = ""
    access_key_secret: str = ""


def _require_oss2() -> None:
    if oss2 is None:
        raise RuntimeError("缺少依赖：oss2 未安装，无法上传 OSS")


def _get_access_keys(*, cfg: OssConfig) -> tuple[str, str]:
    ak = (cfg.access_key_id or "").strip()
    sk = (cfg.access_key_secret or "").strip()
    if ak and sk:
        return ak, sk

    ak = (os.environ.get("OSS_ACCESS_KEY_ID") or "").strip()
    sk = (os.environ.get("OSS_ACCESS_KEY_SECRET") or "").strip()
    if not ak or not sk:
        raise RuntimeError(
            "缺少 OSS 密钥：请设置环境变量 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET，"
            "或在 config/setting.toml 的 [oss] 下配置 access_key_id/access_key_secret"
            "（也兼容 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET）"
        )
    return ak, sk


def oss_config_from_setting_section(section: Any) -> OssConfig:
    """从 `config.get_raw_config().get(\"oss\")` 构造配置。"""
    sec: Dict[str, Any] = section if isinstance(section, dict) else {}

    def pick_str(*keys: str, default: str = "") -> str:
        for k in keys:
            if k not in sec or sec[k] is None:
                continue
            s = str(sec[k]).strip()
            if s:
                return s
        return default

    return OssConfig(
        enabled=bool(sec.get("enabled", False)),
        endpoint=pick_str("endpoint"),
        region=pick_str("region"),
        bucket=pick_str("bucket"),
        public_base_url=pick_str("public_base_url"),
        access_key_id=pick_str("access_key_id", "OSS_ACCESS_KEY_ID"),
        access_key_secret=pick_str("access_key_secret", "OSS_ACCESS_KEY_SECRET"),
    )


def upload_bytes_to_oss(
    *,
    cfg: OssConfig,
    data: bytes,
    object_key: str,
    content_type: str = "image/jpeg",
) -> str:
    """同步上传字节（建议在 asyncio.to_thread 中调用）。返回 CDN/公开访问 URL。"""
    if not cfg.enabled:
        raise RuntimeError("OSS 未启用")
    _require_oss2()
    ak, sk = _get_access_keys(cfg=cfg)

    auth = oss2.AuthV4(ak, sk)
    bucket = oss2.Bucket(auth, cfg.endpoint, cfg.bucket, region=cfg.region, connect_timeout=30)
    headers = {"Content-Type": content_type} if content_type else None
    bucket.put_object(object_key, data, headers=headers)

    base = (cfg.public_base_url or "").rstrip("/") + "/"
    return f"{base}{object_key}"


def build_veo_upsample_object_key(*, project_id: str, media_name: Optional[str]) -> str:
    ts = time.strftime("%Y%m%d%H%M%S")
    rid = uuid.uuid4().hex[:8]
    safe = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in (project_id or "")) or "proj"
    mid = "".join(c if (c.isalnum() or c in ("-", "_", ".")) else "_" for c in (media_name or "")) or "media"
    return f"veo_workflow/image/upsample/{ts}_{rid}_{safe}_{mid}.jpg"
