"""FPBrowser2API - Main Entry Point

启动方式：
  cd fpbrowser2api
  python main.py
"""

import uvicorn


if __name__ == "__main__":
    from src.core.config import config

    uvicorn.run(
        "src.main:app",
        host=config.server_host,
        port=config.server_port,
        reload=False,
        ws_ping_interval=30,
        ws_ping_timeout=120,
    )

