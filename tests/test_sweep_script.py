"""The sweep script is the thing the timer actually runs — so test it.

These tests drive `scripts/refund_sweep.run` against a real sqlite file and a
real (fake-rail) app, not against the internals: if the script cannot refund a
message, the T+15 min promise in llms.txt is a lie regardless of how green the
library tests are.
"""
from __future__ import annotations

import json
import os

import pytest

import scripts.refund_sweep as sweep_script
from app.config import Config
from app.escrow import EscrowStore
from app.refunds import refund_all
from app.transports import FakeTransport, TelnyxTransport
from tests.stubs import StubMint, make_app, make_token


def _cfg(tmp_path, **overrides) -> Config:
    base = {"escrow_db": str(tmp_path / "nosms.db"), "transport": "fake",
            "refund_after_seconds": 0}
    base.update(overrides)
    return Config.from_env(env={}, **base)


def _seed(cfg: Config, tmp_path, *, status="sent") -> str:
    """One accepted send, left undelivered, written straight into the store.

    ``status="sent"`` is the honest shape of "the rail took it, delivery never
    confirmed". The rail's answers come from ``_patch_rail`` below: the script
    builds its own transport from config (it is a separate process from the
    server), so a plain fresh FakeTransport — which honestly answers
    ``unknown`` for a receipt it never issued — would exercise the F2 bucket,
    not the window-expiry refund these tests exist for. (Before the F2 fix
    they passed via the opposite bug: the old sweep refunded on ``unknown``.)
    """
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0),
                   escrow_db=cfg.escrow_db)
    store = app.state.escrow
    message_id = "m_sweep01"
    store.create(message_id=message_id, pubkey="aa" * 32, dest="+155****0100",
                 rail="fake", price=100, change=28,
                 escrow_token=make_token([64, 32, 4]), change_token=make_token([16, 8, 4]),
                 status=status, provider_message_id="fake-1")
    return message_id


def _patch_rail(monkeypatch, transport):
    """Give the script a stateless rail, the way a real provider API answers.

    Telnyx/JMP are polled over HTTP/XMPP and answer for receipts this process
    never issued; the in-memory FakeTransport is not. Patching the script's
    transport seam models the real deployment without touching its logic.
    """
    monkeypatch.setattr(sweep_script, "build_transport", lambda _cfg: transport)


def _sent_rail() -> FakeTransport:
    """A rail that took the message and never confirmed delivery."""
    rail = FakeTransport()
    rail.set_status("fake-1", "sent", raw="accepted")
    return rail


def test_run_refunds_an_undelivered_message(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_rail(monkeypatch, _sent_rail())
    message_id = _seed(cfg, tmp_path)
    report = sweep_script.run(cfg)
    assert report["ok"] if "ok" in report else True
    assert report["refunded"] == 1
    record = EscrowStore(cfg.escrow_db).get(message_id)
    assert record.refunded and record.refund_amount == 128


def test_running_the_script_twice_does_not_double_refund(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_rail(monkeypatch, _sent_rail())
    message_id = _seed(cfg, tmp_path)
    first = sweep_script.run(cfg)
    second = sweep_script.run(cfg)
    assert first["refunded"] == 1
    assert second["refunded"] == 0
    assert second["checked"] == 0
    store = EscrowStore(cfg.escrow_db)
    assert store.get(message_id).refund_amount == 128
    # exactly one refund token exists, and it covers postage + change
    assert store.get(message_id).refund_token
    assert store.list_unrefunded() == []


def test_main_prints_json_and_exits_zero(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch_rail(monkeypatch, _sent_rail())
    _seed(cfg, tmp_path)
    monkeypatch.setenv("NOSMS_DB_PATH", cfg.escrow_db)
    monkeypatch.setenv("NOSMS_REFUND_AFTER_SECONDS", "0")
    monkeypatch.setenv("NOSMS_TRANSPORT", "fake")
    assert sweep_script.main([]) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["ok"] is True and report["refunded"] == 1


def test_dry_run_claims_nothing(tmp_path, capsys, monkeypatch):
    cfg = _cfg(tmp_path)
    message_id = _seed(cfg, tmp_path)
    monkeypatch.setenv("NOSMS_DB_PATH", cfg.escrow_db)
    monkeypatch.setenv("NOSMS_REFUND_AFTER_SECONDS", "0")
    assert sweep_script.main(["--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report["dry_run"] is True and report["candidates"] == 1
    assert EscrowStore(cfg.escrow_db).get(message_id).refunded_at is None


def test_a_broken_config_exits_nonzero_instead_of_looking_healthy(monkeypatch, capsys):
    monkeypatch.setenv("NOSMS_DB_PATH", "/proc/definitely/not/writable/nosms.db")
    assert sweep_script.main([]) == 1
    assert json.loads(capsys.readouterr().out.strip())["ok"] is False


def test_the_configured_transport_is_what_the_sweep_polls(tmp_path, monkeypatch):
    """An unavailable Telnyx rail must not be silently swapped for the fake."""
    cfg = _cfg(tmp_path, transport="telnyx")
    _seed(cfg, tmp_path)
    monkeypatch.setenv("NOSMS_DB_PATH", cfg.escrow_db)
    monkeypatch.setenv("NOSMS_TRANSPORT", "telnyx")
    monkeypatch.setenv("NOSMS_REFUND_AFTER_SECONDS", "0")
    monkeypatch.setenv("NOSMS_SMS_GATEWAY_PATH", str(tmp_path / "missing-checkout"))
    message_id = "m_sweep01"
    assert sweep_script.main([]) == 0
    # the telnyx adapter cannot observe delivery without credentials, so the
    # message is counted as unobservable rather than refunded on silence
    store = EscrowStore(cfg.escrow_db)
    assert store.get(message_id).refunded_at is None


# --- F1/F2 at the layer the timer actually runs --------------------------------

def test_the_script_refunds_a_failed_send_that_never_got_a_receipt(tmp_path):
    """F1, end to end through the script: a send that failed before a receipt
    existed is refunded by the timer — never stranded forever."""
    cfg = _cfg(tmp_path)
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0),
                   escrow_db=cfg.escrow_db)
    store = app.state.escrow
    store.create(message_id="m_dead", pubkey="aa" * 32, dest="+155****0100",
                 rail="fake", price=100, change=28,
                 escrow_token=make_token([64, 32, 4]),
                 change_token=make_token([16, 8, 4]),
                 status="failed")                    # no provider_message_id
    report = sweep_script.run(cfg)
    assert report["refunded"] == 1, report
    assert report["unobservable"] == 0
    record = EscrowStore(cfg.escrow_db).get("m_dead")
    assert record.refunded_at is not None
    assert record.refund_amount == 64 + 32 + 4 + 16 + 8 + 4   # postage + change


def test_the_script_does_not_refund_an_unclassifiable_status(tmp_path, monkeypatch):
    """F2, end to end through the script: an unknown poll is reported in its
    own bucket and pays out nothing."""
    cfg = _cfg(tmp_path)
    app = make_app(tmp_path, transport=FakeTransport(), mint=StubMint(fee_ppk=0),
                   escrow_db=cfg.escrow_db)
    store = app.state.escrow
    store.create(message_id="m_weird", pubkey="aa" * 32, dest="+155****0100",
                 rail="fake", price=100, change=28,
                 escrow_token=make_token([64, 32, 4]),
                 change_token=make_token([16, 8, 4]),
                 status="sent", provider_message_id="fake-77")
    rail = FakeTransport()
    rail.set_status("fake-77", "a-status-word-nobody-maps")
    _patch_rail(monkeypatch, rail)

    report = sweep_script.run(cfg)
    assert report["refunded"] == 0, report
    assert report["unclassifiable"] == 1, report
    record = EscrowStore(cfg.escrow_db).get("m_weird")
    assert record.refunded_at is None
    assert record.status == "unknown"              # the unclassifiable poll stuck
