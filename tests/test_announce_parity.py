"""The Python emitter must equal the kit emitter — proven, not asserted.

THE ARGUMENT
============

The kit is the source of truth and it lives in another repo
(``cvm-services/cvm-service-kit``, branch ``pr/s2a-announce-emitter``, commit
``7bf4be6``). ``app/announce.py`` is a port of it. A port that is "close enough"
is exactly the drift the contract exists to prevent, so parity is proven by a
two-leg triangle:

    leg 1  (contextvm-services, deno)   kit emitter  ==  tests/fixtures/nosms-11316.tags.json
           tools/nosms_announcement_test.ts, test "golden fixture: the kit still
           reproduces fixtures/nosms-11316.tags.json" — 12 tests, run in CI/deno.

    leg 2  (this repo, pytest)          python port  ==  tests/fixtures/nosms-11316.tags.json

Therefore the python port == the kit emitter. Each leg fails independently if
either side drifts, and neither leg can pass by both sides moving together —
the fixture is a frozen, reviewed artifact in git with a recorded kit commit.

WHAT THIS TEST DOES NOT CLAIM
=============================

It proves TAG parity. It does NOT prove wire parity with a live relay (that is
the live ``nak req -k 11316`` read-back) and it does NOT prove that the vendored
kit is correct (that is the kit's own reviewed test suite).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.announce import (
    AnnounceError,
    AnnounceInput,
    ToolCap,
    assert_announcement_tags,
    assess_announcement_tags,
    emit_announcement_tags,
    load_vocab,
    recompute_tier_from_tags,
    vocab_errors,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO / "tests" / "fixtures" / "nosms-11316.tags.json"
VOCAB_PATH = REPO / "vocab" / "service-inputs.json"

#: The kit commit the fixture was generated from. Bumping the fixture without
#: bumping this (and re-reviewing the diff) is the drift this constant catches.
PINNED_KIT_COMMIT = "7bf4be68a45e0a88b21f221852f3ef504b6a5807"

FIXTURE = json.loads(FIXTURE_PATH.read_text())
VOCAB = load_vocab(VOCAB_PATH)


def input_from_fixture() -> AnnounceInput:
    """Rebuild the AnnounceInput the fixture declares, with no hand-tuning."""
    raw = FIXTURE["input"]
    assert "tier" not in raw, "the fixture's input must not carry a tier (the emitter computes it)"
    return AnnounceInput(
        service_class=raw["serviceClass"],
        d=raw["d"],
        geohashes=raw.get("geohashes", []),
        required=raw.get("required", []),
        optional=raw.get("optional", []),
        tools={name: ToolCap(amount=cap["amount"], unit=cap.get("unit", "sats"))
               for name, cap in (raw.get("tools") or {}).items()},
        registries=raw.get("registries", []),
        urls=raw.get("urls", []),
        human_tags=raw.get("humanTags", []),
    )


# --------------------------------------------------------------------------
# leg 2 — the parity claim
# --------------------------------------------------------------------------


def test_fixture_provenance_is_pinned():
    """A fixture from an unnamed kit commit proves nothing."""
    assert FIXTURE["_kit"]["commit"] == PINNED_KIT_COMMIT, (
        "fixture was generated from a different kit commit — regenerate it in "
        "contextvm-services, re-review, and bump PINNED_KIT_COMMIT"
    )
    assert FIXTURE["_kit"]["repo"] == "cvm-services/cvm-service-kit"
    assert FIXTURE["_kit"]["branch"] == "pr/s2a-announce-emitter"
    assert FIXTURE["_generated_by"] == "tools/nosms_announcement.ts", (
        "the fixture must be DENO-KIT output, not a python self-portrait"
    )


def test_vendored_register_matches_the_fixture_generator():
    """The port must be fed the SAME register the fixture was generated with."""
    # both copies come from contextvm-services; this repo vendors the kit's copy.
    errors = vocab_errors(VOCAB)
    assert errors == [], f"vendored register is not usable: {errors}"
    reg = json.loads(VOCAB_PATH.read_text())
    assert reg["version"] == 1
    # the register the kit vendors is byte-identical to contextvm-services' own
    # copy (sha256 f2119003…) — recorded here so an accidental edit is caught.
    import hashlib

    digest = hashlib.sha256(VOCAB_PATH.read_bytes()).hexdigest()
    assert digest == "f21190037d264d891cb4fad12dcfa77516da290d89aa1989d36f75f8919b508b", (
        f"vendored register drifted (sha256 {digest}); re-vendor from contextvm-services"
    )


def test_python_emitter_reproduces_the_kit_fixture_byte_for_byte():
    """THE parity test: python output == the kit's own output, exactly.

    The fixture's ``contract_tags`` (plus ``tier``/``warnings``) IS the kit
    emitter's output — ``tags`` additionally carries the pmi/payload tags the
    CALLER appends afterwards (see ``test_python_port_splits_the_same_way_the_kit_does``).
    The port owns the kit's half only, so that is the half compared here.
    """
    tags, warnings, tier = emit_announcement_tags(input_from_fixture(), VOCAB)
    assert json.loads(json.dumps(tags)) == FIXTURE["contract_tags"], (
        "python emitter diverged from the kit fixture:\n"
        f"  python: {json.dumps(tags)}\n"
        f"  kit   : {json.dumps(FIXTURE['contract_tags'])}"
    )
    assert tier == FIXTURE["tier"]
    assert warnings == FIXTURE["warnings"]


def test_python_port_splits_the_same_way_the_kit_does():
    """contract_tags is what the kit emits; pmi/payload are appended, not emitted."""
    tags, _, _ = emit_announcement_tags(input_from_fixture(), VOCAB)
    contract = [t for t in tags if t[0] in {"d", "t", "g", "cap", "a", "r"}]
    assert contract == FIXTURE["contract_tags"], "the kit-owned subset must match the fixture's"
    # and the kit-owned subset contains no pmi — the kit has no CEP-8 support yet
    assert not any(t[0] == "pmi" for t in contract)
    assert FIXTURE["payment_tags"] == [["pmi", "bitcoin-cashu", "explicit_gating"]]


# --------------------------------------------------------------------------
# the card's definition of done
# --------------------------------------------------------------------------


def test_assert_announcement_tags_returns_ok_for_the_emitted_set():
    tags, _, _ = emit_announcement_tags(input_from_fixture(), VOCAB)
    assessment = assert_announcement_tags(tags, VOCAB)  # raises if violated
    assert assessment.violations == []
    assert assessment.tier_mismatch is False
    assert assessment.effective == "financial"
    assert assessment.declared.required == ["payment.amount"]
    assert assessment.declared.none_sentinel is True


def test_every_gap_from_the_live_event_is_closed():
    """The six gaps named on the card, asserted against the emitted set."""
    tags, _, _ = emit_announcement_tags(input_from_fixture(), VOCAB)
    flat = [t for t in tags]

    # 1. namespaced class (P2 MUST)
    assert ["t", "cvm:service:sms"] in flat
    # 2. the sentinel (P15)
    assert ["t", "cvm:req:none"] in flat
    # 3. the tier tag (P15 / D14 amendment)
    assert ["t", "cvm:tier:financial"] in flat
    assert sum(1 for t in flat if t[0] == "t" and t[1].startswith("cvm:tier:")) == 1
    # 4. NO geohash (P2: MUST NOT publish a meaningless one)
    assert not any(t[0] == "g" for t in flat)
    # 5. the URL on `r`, and no non-standard `contract` tag
    assert ["r", "https://nosms.orangesync.tech/llms.txt"] in flat
    assert not any(t[0] == "contract" for t in flat)
    # 6. exactly one cap, at the advertised price
    caps = [t for t in flat if t[0] == "cap"]
    assert caps == [["cap", "tool:sms.send", "2900", "sats"]]


def test_caller_cannot_supply_the_tier():
    """The type has no tier field, and a tier-shaped input cannot smuggle one in."""
    assert not hasattr(input_from_fixture(), "tier")
    # even a hostile `required` cannot produce a tier tag that disagrees with the fields
    hostile = AnnounceInput(
        service_class="sms", d="nosms", required=["payment.amount"],
        human_tags=["cvm:tier:sensitive"],  # a namespaced human tag is refused
    )
    with pytest.raises(AnnounceError, match="namespaced"):
        emit_announcement_tags(hostile, VOCAB)


def test_absent_is_not_none():
    """An input with no req/opt tags is UNCLASSIFIED, and cannot emit a tier tag."""
    tags, _, _ = emit_announcement_tags(AnnounceInput(service_class="sms", d="nosms"), VOCAB)
    a = assess_announcement_tags(tags, VOCAB)
    # the emitter derives a sentinel for a fieldless flow, so this one is "none"
    assert a.effective == "none"
    assert ["t", "cvm:req:none"] in tags
    # but a HAND-BUILT tag list with no fields at all is unclassified, not "none"
    bare = [["d", "nosms"], ["t", "cvm:service:sms"]]
    b = assess_announcement_tags(bare, VOCAB)
    assert b.declared.unclassified is True
    assert b.recomputed is None


def test_unknown_field_fails_loud_never_as_none():
    with pytest.raises(AnnounceError, match="unknown requirement field"):
        emit_announcement_tags(
            AnnounceInput(service_class="sms", d="nosms", required=["contact.telepathy"]), VOCAB
        )


def test_tier_mismatch_is_reported_with_both_values():
    """A lying aggregate: the reader uses the recomputed value and names both."""
    lying = [
        ["d", "nosms"],
        ["t", "cvm:service:sms"],
        ["t", "cvm:req:contact.phone"],
        ["t", "cvm:tier:none"],
    ]
    a = assess_announcement_tags(lying, VOCAB)
    assert a.tier_mismatch is True
    assert a.recomputed == "contact"
    assert any("says 'none'" in v and "recompute to 'contact'" in v for v in a.violations)
    with pytest.raises(AnnounceError, match="does not recompute|violates"):
        assert_announcement_tags(lying, VOCAB)


def test_recompute_from_tags_matches_the_emitter():
    tags, _, tier = emit_announcement_tags(input_from_fixture(), VOCAB)
    assert recompute_tier_from_tags(tags, VOCAB).tier == tier
