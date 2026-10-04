"""The repo-root `llms.txt` must not drift from the document the service serves.

`GET /llms.txt` is rendered from the live Config, so it is always current. The
committed file is what a reviewer and a git diff see. Two sources of truth for
one contract is a bug waiting to happen, so this test makes drift impossible:
they must be byte-identical for the default config.
"""
from __future__ import annotations

import pathlib
import re
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


TOKEN_BLOCK = re.compile(r"Tokens you can rely on:(.*?)\n\n", re.S)
REASON_LITERALS = (
    re.compile(r'error_response\(\s*\d+\s*,\s*"([a-z_]+)"'),          # handlers
    re.compile(r'HTTP_HINTS[^\n]*?"([a-z_]+)"'),                       # status map
    re.compile(r'(?:Cashu|Quota|Nip98)Error\(\s*"([a-z_]+)"'),         # module errors
)


def test_every_emitted_x_reason_token_is_documented():
    """The documented token list must cover what the code can actually emit.

    `bad_request` and `llms_full_not_implemented` were both emitted while the
    list omitted them, and `send_not_implemented` stayed listed after M1b
    replaced the 501 stub — a caller branching on the documented set would have
    been wrong in both directions.
    """
    documented = set(re.findall(r"`([a-z_]+)`", TOKEN_BLOCK.search(LLMS.read_text()).group(1)))
    assert {"bad_request", "invalid_request", "internal_error"} <= documented
    emitted: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text()
        for pattern in REASON_LITERALS:
            emitted |= set(pattern.findall(source))
    assert emitted, "no reason literals found — the scanner has drifted from the code"
    assert emitted <= documented, f"undocumented X-Reason tokens: {sorted(emitted - documented)}"
