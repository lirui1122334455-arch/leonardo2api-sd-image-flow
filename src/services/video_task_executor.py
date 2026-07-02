"""视频任务执行器（目前为模拟实现）。

注意：
- `gen_video` 的模拟仍保留在这里；
- Sora 生视频（真实自动化）在 `sora_task_executor.py`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .task_executor_types import ProgressCB


async def simulate_video_task(prompt: str, image_path: Optional[str], progress_cb: ProgressCB) -> Dict[str, Any]:
    """模拟视频生成：约 25 秒完成。"""

    for p in (3, 8, 15, 25, 35, 45, 55, 65, 75, 85, 92, 96):
        await asyncio.sleep(2.0)
        await progress_cb(p, None)
    await asyncio.sleep(1.5)
    await progress_cb(100, None)
    return {
        "type": "video",
        "message": "模拟执行完成（请在 video_task_executor.py 接入真实视频生成/自动化）",
        "prompt": prompt,
        "image_path": image_path,
        "outputs": [],
    }

