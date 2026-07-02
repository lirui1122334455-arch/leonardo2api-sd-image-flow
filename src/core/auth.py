"""Authentication helpers.

分两套鉴权（参考 flow2api）：
- 管理后台：账号密码登录，返回“管理会话 token”
- 对外使用接口：API Key（Authorization: Bearer <api_key>）
"""

from __future__ import annotations

import bcrypt
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import config


security = HTTPBearer()


class AuthManager:
    @staticmethod
    def verify_api_key(api_key: str) -> bool:
        return api_key == config.api_key

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False


async def verify_api_key_header(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    api_key = credentials.credentials
    if not AuthManager.verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

