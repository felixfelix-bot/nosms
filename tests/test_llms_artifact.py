"""The repo-root `llms.txt` must not drift from the document the service serves.

`GET /llms.txt` is rendered from the live Config, so it is always current. The
committed file is what a reviewer and a git diff see. Two sources of truth for
one contract is a bug waiting to happen, so this test makes drift impossible:
they must be byte-identical for the default config.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

from app.config import Config
from app.llms import llms_txt

ROOT = pathlib.Path(__file__).resolve().parent.parent
LLMS = ROOT / "llms.txt"

REQUIRED = [
    "POST /api/send",
    "X-Cashu:",
    "cashuB",
    "/api/message/<message_id>/status",
    "/api/refund/<message_id>",
    "insufficient_funds",
    "destination_cooldown",
    "daily_cap_reached",
    "escrowed",
    "input fee",
]


def test_committed_llms_txt_is_present_and_nonempty():
    assert LLMS.is_file()
    assert len(LLMS.read_text()) > 2000


def test_committed_llms_txt_matches_the_served_document():
    assert LLMS.read_text() == llms_txt(Config.from_env(env={})), (
        "llms.txt drifted from app/llms.py; run scripts/render_llms.py")


def test_send_contract_is_documented():
    body = LLMS.read_text()
    for needle in REQUIRED:
        assert needle in body, f"llms.txt is missing the send contract: {needle!r}"


def test_render_check_passes_on_the_committed_file():
    out = subprocess.run([sys.executable, "scripts/render_llms.py", "--check"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
