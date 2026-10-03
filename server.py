"""Entrypoint.

Two supported ways to run:

    python -m server            # uses NOSMS_HOST / NOSMS_PORT (default 127.0.0.1:8088)
    uvicorn server:app          # for systemd / Caddy fronting

Configuration is environment-only; no secret is ever read from this file.
"""
from __future__ import annotations

import os

from app.config import Config
from app.main import create_app

app = create_app(Config.from_env())


def main() -> None:
    import uvicorn

    host = os.environ.get("NOSMS_HOST", "127.0.0.1")
    port = int(os.environ.get("NOSMS_PORT", "8088"))
    log_level = os.environ.get("NOSMS_LOG_LEVEL", "info")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
