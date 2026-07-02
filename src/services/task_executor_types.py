"""任务执行器：通用类型定义。

说明：
- 该文件只放“跨执行器复用”的类型，避免 image/video/sora 等模块互相耦合。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

# progress_cb(progress_percent, payload)
ProgressCB = Callable[[int, Optional[Dict[str, Any]]], Awaitable[None]]


class NonPenalizedTaskError(RuntimeError):
    """失败但不计入窗口连续错误（consecutive_errors）的异常。

    用途：Sora 创建阶段常见的 400/invalid_request、排队超时等，
    这类错误不应导致窗口被连续错误熔断。
    """

    no_penalty: bool = True

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        content_violation: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.content_violation = bool(content_violation)

