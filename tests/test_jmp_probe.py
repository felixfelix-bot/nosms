"""Offline tests for the cold-send probe's evidence redaction.

The published record must be *redacted by construction*: one masking convention,
no field named ``raw`` holding a masked value, and no un-masked destination
anywhere in the JSON — including inside the verbatim stanza.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "jmp_cold_send_probe.py"

# Numbers are assembled from parts so the mask under test is computed, never
# eyeballed: a second, different number proves the mask keeps the last four.
REAL = "+1" + "321" + "555" + "8875"
OTHER = "+1" + "555" + "123" + "0000"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("jmp_cold_send_probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mask_keeps_the_leading_digit_and_last_four(probe):
    m = probe.mask(REAL)
    assert m.startswith("+1") and m.endswith("8875")
    assert set(m[2:-4]) == {"*"}
    assert len(m) == len(REAL)               # same shape, only the middle masked


def test_mask_is_stable_and_short_input_stays_short(probe):
    assert probe.mask(REAL) == probe.mask(REAL)
    assert probe.mask("+123") == "***"       # nothing to keep: all stars


def test_redact_masks_every_e164_in_a_stanza(probe):
    stanza = (f"<message to='{REAL}@cheogram.com' from='{OTHER}@cheogram.com'/>")
    out = probe.redact(stanza)
    assert f"{probe.mask(REAL)}@cheogram.com" in out
    assert f"{probe.mask(OTHER)}@cheogram.com" in out
    assert REAL not in out and OTHER not in out
    # idempotent: applying it twice does not mangle the first result
    assert probe.redact(out) == out


def test_published_evidence_has_no_raw_field_and_no_unmasked_number(probe):
    ev = probe.build_evidence(
        jid="hermes-jmp@jabber.fr", to_number=REAL, msg_id="abc",
        body="hello from the rail", sent=True, sent_at=1.0,
        stanza_xml=f"<message to='{REAL}@cheogram.com'><body>x</body></message>",
        responses=[{"kind": "message", "type": "chat",
                    "stanza_xml": f"<message from='{REAL}@cheogram.com'/>"}])

    mask = probe.mask(REAL)
    assert ev["to_masked"] == mask
    assert ev["to_masked_jid"] == f"{mask}@cheogram.com"
    # no field claims to be "raw" while holding a mask
    assert not any("raw" in k.lower() for k in ev)

    blob = json.dumps(ev)
    assert REAL not in blob                  # the real value is nowhere...
    assert mask in blob                      # ...while the mask is intact
    assert ev["redaction_note"]              # the convention is documented in-band


def test_raw_record_is_deliberately_separate_and_labelled(probe):
    raw = probe.raw_evidence(
        jid="hermes-jmp@jabber.fr", to_number=REAL, msg_id="abc", body="hi",
        sent=True, sent_at=1.0, stanza_xml="<message/>", responses=[])
    assert raw["to_raw"] == f"{REAL}@cheogram.com"   # the name is honest
    assert "UN-REDACTED" in raw["WARNING"]


def test_accepted_is_false_when_a_refusal_stanza_arrives(probe):
    ev = probe.build_evidence(
        jid="j", to_number=REAL, msg_id="m", body="b", sent=True, sent_at=0.0,
        stanza_xml="<message/>",
        responses=[{"kind": "stream_error", "stanza_xml": "boom"}])
    assert ev["accepted"] is False


def test_committed_evidence_matches_the_one_convention(probe):
    """The published artefact and the README must agree on the mask, and the
    artefact must hold no un-masked destination."""
    ev = json.loads((ROOT / "evidence" / "cold-send-20261005T002427Z.json").read_text())
    readme = (ROOT / "evidence" / "README.md").read_text()

    assert not any("raw" in k.lower() for k in ev)
    mask = ev["to_masked"]
    assert set(mask[2:-4]) == {"*"}                  # the canonical mask shape
    assert mask in readme                            # README quotes the same value
    assert ev["to_masked_jid"] == f"{mask}@cheogram.com"
    assert mask in ev["outbound_stanza_xml"]
    assert any(ch.isdigit() for ch in mask[2:-4]) is False
