"""The live announcement carries exactly the port's contract surface (offline).

`tests/test_announce_parity.py` proves the port equals the kit. This file proves
the thing that is actually PUBLISHED is that same set: it reads a captured
`nak req` read-back of the live kind-11316 and diffs its contract surface against
`emit_announcement_tags`.

Offline on purpose: a test that needs a relay fails for reasons that are not the
code (relay down, this host IP-banned from some relays) and cannot run in CI. The
capture is a committed artifact with its event id recorded, so the comparison is
exact and reproducible; refresh it from the read-back in
`contextvm-services/evidence/S5a-live-announcement.md`.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.announce import emit_announcement_tags, load_vocab
from app.announce import AnnounceInput, ToolCap

import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

CAPTURE = REPO / "tests" / "fixtures" / "nosms-11316.live.json"
CONTRACT_URL = "https://nosms.orangesync.tech/llms.txt"

#: The letters the KIT owns. Payload (name/about/website) and pmi are appended by
#: the caller, so the comparison is scoped to what the emitter is responsible for.
KIT_LETTERS = {"d", "t", "g", "cap", "a", "r"}


def _live() -> dict:
    return json.loads(CAPTURE.read_text())


def _emitted_contract() -> list[list[str]]:
    tags, _, _ = emit_announcement_tags(
        AnnounceInput(
            service_class="sms",
            d="nosms",
            required=["payment.amount"],
            tools={"sms.send": ToolCap(amount=2900)},
            urls=[CONTRACT_URL],
            human_tags=["sms", "contextvm"],
        ),
        load_vocab(),
    )
    return tags


def test_the_capture_is_the_kind_we_think_it_is():
    live = _live()
    assert live["kind"] == 11316
    assert len(live["pubkey"]) == 64
    # the signing key is the nosms CVM key the card names
    # (npub1al953lfy7nv5qjwhcw8u2p0uuq0dwmjes3g4rgjxqlke75nxsd9q6lw3w9)
    assert live["pubkey"].startswith("efcb48fd"), "not signed by the nosms CVM key"


def test_live_contract_surface_equals_the_port_output():
    live = [t for t in _live()["tags"] if t[0] in KIT_LETTERS]
    emitted = _emitted_contract()
    assert live == emitted, (
        "the live event's contract surface differs from the port:\n"
        f"  live: {json.dumps(live)}\n"
        f"  port: {json.dumps(emitted)}"
    )


def test_live_event_has_no_stale_defects():
    tags = _live()["tags"]
    letters = [t[0] for t in tags]
    assert "contract" not in letters, "non-standard `contract` tag still published"
    assert sum(1 for t in tags if t[0] == "cap") == 1, "more than one cap on the wire"
    assert "g" not in letters, "a geohash is published although there is no location"
    t = [x[1] for x in tags if x[0] == "t"]
    assert "cvm:service:sms" in t
    assert "cvm:req:none" in t
    assert [v for v in t if v.startswith("cvm:tier:")] == ["cvm:tier:financial"]
    assert [x for x in tags if x[0] == "cap"] == [["cap", "tool:sms.send", "2900", "sats"]]


def test_live_pmi_is_present_exactly_once():
    pmi = [t for t in _live()["tags"] if t[0] == "pmi"]
    assert pmi == [["pmi", "bitcoin-cashu", "explicit_gating"]]
