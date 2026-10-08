"""nosms ASGI entrypoint.

Local:   python server.py            (or: uvicorn server:app --reload)
Prod:    uvicorn server:app --host 0.0.0.0 --port 8000   (behind Caddy on vps2)

Configuration is env-only (see app/config.py); no secrets live in code.
"""
from __future__ import annotations

import os

from app.main import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.environ.get("NOSMS_HOST", "127.0.0.1"),
        port=int(os.environ.get("NOSMS_PORT", "8000")),
        log_level=os.environ.get("NOSMS_LOG_LEVEL", "info"),
    )
