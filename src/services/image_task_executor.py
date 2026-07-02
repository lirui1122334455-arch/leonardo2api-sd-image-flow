"""图片任务执行器（目前为模拟实现）。

后续你接入真实图片生成（如调用外部平台、浏览器自动化等）时，
可以直接替换本文件内逻辑，而不影响 Sora / 其它执行器。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .task_executor_types import ProgressCB


async def simulate_image_task(prompt: str, image_path: Optional[str], progress_cb: ProgressCB) -> Dict[str, Any]:
    """模拟图片生成：约 8 秒完成。"""

    for p in (5, 15, 30, 45, 60, 75, 90):
        await asyncio.sleep(0.8)
        await progress_cb(p, None)
    await asyncio.sleep(0.8)
    await progress_cb(100, None)
    return {
        "type": "image",
        "message": "模拟执行完成（请在 image_task_executor.py 接入真实图片生成/自动化）",
        "prompt": prompt,
        "image_path": image_path,
        "outputs": [],
    }

