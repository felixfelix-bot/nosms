"""The server's OWN announcement path is conformant (not just the port).

`tests/test_announce_parity.py` proves the port equals the kit. This file proves
the thing that actually publishes — `scripts/run_cvm_server.py::announcement_tags`
— uses that port and emits nothing extra: no hand-rolled `contract` tag, no
duplicate `cap`, no geohash, and no `cap` on a tool that is free.

It is deliberately separate from the parity test: parity is about the emitter,
this is about the wiring, and a regression in either should fail on its own.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

from app.announce import (
    ANNOUNCEMENT_KIND,
    assert_announcement_tags,
    load_vocab,
    tag_values,
)
from app.transports.fake import FakeTransport

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTRACT_URL = "https://nosms.orangesync.tech/llms.txt"


def _load_server_module():
    """Import scripts/run_cvm_server.py without running it (it needs nostr_sdk)."""
    path = REPO / "scripts" / "run_cvm_server.py"
    spec = importlib.util.spec_from_file_location("_nosms_run_cvm_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def server():
    return _load_server_module()


@pytest.fixture(scope="module")
def emitted(server):
    tool_names = [t["name"] for t in server.CvmTools(FakeTransport(), docs={}).tool_definitions()]
    tags, tier = server.announcement_tags(CONTRACT_URL, tool_names, load_vocab())
    return tags, tier, tool_names


def test_kind_is_the_cep6_server_announcement(server):
    assert server.ANNOUNCE_SERVER == ANNOUNCEMENT_KIND == 11316


def test_the_published_set_is_conformant(emitted):
    tags, tier, _ = emitted
    a = assert_announcement_tags(tags, load_vocab())  # raises if violated
    assert a.violations == []
    assert a.tier_mismatch is False
    assert tier == "financial"


def test_all_six_live_event_gaps_are_closed_on_the_wire_path(emitted):
    tags, _, _ = emitted
    t = tag_values(tags, "t")
    assert "cvm:service:sms" in t                    # 1
    assert "cvm:req:none" in t                       # 2
    assert "cvm:tier:financial" in t                 # 3
    assert not any(x[0] == "g" for x in tags)        # 4 (correctly absent, asserted)
    assert tag_values(tags, "r") == [CONTRACT_URL]   # 5
    assert not any(x[0] == "contract" for x in tags)  # 5 (non-standard tag gone)
    # 6: one cap, for the paid tool only, at the advertised price
    assert [x for x in tags if x[0] == "cap"] == [["cap", "tool:sms.send", "2900", "sats"]]


def test_no_cap_on_a_free_tool(emitted):
    """A `cap` on sms.status/docs/pricing/capabilities would claim they cost money."""
    tags, _, tool_names = emitted
    paid = {x[1].removeprefix("tool:") for x in tags if x[0] == "cap"}
    free = set(tool_names) - paid
    assert paid == {"sms.send"}, f"only sms.send is paid, got {paid}"
    assert free == {"sms.status", "sms.pricing", "sms.capabilities", "docs"}
    assert len(paid) == 1, "exactly one paid tool"


def test_declared_flow_inputs_are_money_only(emitted):
    """to/body are tool arguments, not flow fields; declaring them would lie."""
    tags, _, _ = emitted
    declared = [x[1].removeprefix("cvm:req:") for x in tags
                if x[0] == "t" and x[1].startswith("cvm:req:")]
    assert sorted(declared) == ["none", "payment.amount"]
    assert not any("to" == d or "body" == d for d in declared)


def test_the_announcement_is_deterministic(server, emitted):
    """A re-announce must be idempotent: the relay gets a byte-identical surface."""
    tags, _, tool_names = emitted
    again, tier2 = server.announcement_tags(CONTRACT_URL, tool_names, load_vocab())
    assert json.loads(json.dumps(again)) == json.loads(json.dumps(tags))
    assert tier2 == "financial"


def test_payload_tags_are_present_but_never_namespaced(emitted):
    """name/about/website are payload (D2); the filterable surface stays d/r/t."""
    tags, _, _ = emitted
    assert [x[1] for x in tags if x[0] == "name"] == ["nosms"]
    letters = {x[0] for x in tags if len(x[0]) == 1}
    assert letters == {"d", "r", "t"}, f"unexpected filterable tag letters: {letters}"
