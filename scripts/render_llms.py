#!/usr/bin/env python3
"""Render the agent contract at the repo root (`llms.txt`).

`GET /llms.txt` is served from `app/llms.py` and rendered from the live Config,
so the running service can never advertise a stale contract. This script writes
the same document to `llms.txt` at the repo root, rendered with the *default*
config, so the contract is also reviewable in a diff and readable without
running the service.

The two cannot drift: `tests/test_llms_artifact.py` asserts the committed file
equals the rendered document, so editing one without the other fails the suite.

    python scripts/render_llms.py            # write llms.txt
    python scripts/render_llms.py --check    # exit 1 if it would change
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.llms import llms_txt  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, "llms.txt")


def rendered() -> str:
    """The default-config document — the same bytes the service calls policy."""
    return llms_txt(Config.from_env(env={}))


def main(argv: list[str]) -> int:
    body = rendered()
    current = open(TARGET).read() if os.path.exists(TARGET) else None
    if "--check" in argv:
        if current == body:
            print("llms.txt is up to date")
            return 0
        print("llms.txt is STALE: run scripts/render_llms.py", file=sys.stderr)
        return 1
    with open(TARGET, "w") as handle:
        handle.write(body)
    print(f"wrote {TARGET} ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
