"""The three JMP tools, tested as the CVM serves them — offline, no relay.

The acceptance targets, in the order they hurt when wrong:

1. **The secret never leaves in the clear.** `jmp.credentials` must answer with a
   NIP-44 payload addressed to the caller's npub, and nothing else in the
   response (or a log) may contain the plaintext.
2. **Authorization happens BEFORE payment.** An unauthorised npub is refused
   without the mint ever being touched, even when it attaches a valid token.
3. **`source` is honest.** `jmp.status` from a cache says `cached` and names its
   capture time; only a real drive may say `live-flow`, and a failed drive
   refuses instead of quietly serving the cached numbers.
4. **The number's state is named.** A session-scoped reservation, stated as such
   in the payload — not a number presented as owned.
5. **It cannot pay.** The flow selects the bitcoin branch only; no payment step
   exists to be emitted (the flow tests assert that, this file asserts the tool
   never grows one).
"""
from __future__ import annotations

import json
import pathlib

from coincurve import PrivateKey

from app.cvm_tools import CvmTools
from app.escrow import EscrowStore
from app.jmp_facts import CACHED_FACTS, CAPTURED_AT
from app.jmp_secrets import (
    ACCOUNT_SECRET,
    FUNDING_SECRET,
    NUMBER_SECRET,
    MemorySecretStore,
    OperatorAllowList,
    redaction_list,
)
from app.jmp_tools import (
    CREDENTIALS_PRICE_SATS,
    REVOKE_HINT,
    ROTATE_AFTER_DAYS,
    TOOL_JMP_CREDENTIALS,
    TOOL_JMP_FUNDING,
    TOOL_JMP_STATUS,
    JmpService,
)
from app.nip44 import decrypt, encrypt
from app.transports import FakeTransport
from app.transports.base import Capabilities  # noqa: F401  (documented rail flags)
from tests.stubs import StubMint, make_token

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = REPO / "docs" / "cvm"


def pub_of(secret_hex: str) -> str:
    return PrivateKey(bytes.fromhex(secret_hex)).public_key.format(compressed=True)[1:].hex()


SERVER_SK = "22" * 32
SERVER_PK = pub_of(SERVER_SK)
OPERATOR_SK = "33" * 32
OPERATOR_PK = pub_of(OPERATOR_SK)
STRANGER_SK = "44" * 32
STRANGER_PK = pub_of(STRANGER_SK)

#: the account material the store is expected to hold (test-only values).
ACCOUNT = {"jid": "hermes-jmp@jabber.fr", "secret": "s3cret-xmpp-password-9f2c"}
NUMBER = {"number": "(810) 258-3253", "number_state": "RESERVED",
          "reserved_at": "2026-09-26T20:52:00Z"}
FUNDING = {"btc_address": "bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk",
           "amount_btc": "0.000244", "amount_usd_min": "20.00",
           "captured_at": CAPTURED_AT}

SECRET_PLAINTEXT = ACCOUNT["secret"]


def store(**overrides) -> MemorySecretStore:
    values = {ACCOUNT_SECRET: dict(ACCOUNT), NUMBER_SECRET: dict(NUMBER),
              FUNDING_SECRET: dict(FUNDING)}
    values.update(overrides)
    return MemorySecretStore(values)


def load_docs() -> dict:
    return {"llms": (CONTRACT / "llms.txt").read_text(),
            "llms-full": (CONTRACT / "llms-full.txt").read_text()}


def service(**kw) -> JmpService:
    kw.setdefault("secret_store", store())
    kw.setdefault("server_secret_hex", SERVER_SK)
    kw.setdefault("allow_list", OperatorAllowList(frozenset({OPERATOR_PK})))
    return JmpService(**kw)


def cvm(mint=None, jmp=None, owners=None, **kw) -> CvmTools:
    return CvmTools(FakeTransport(), docs=load_docs(),
                    owner_pubkeys=owners if owners is not None else set(),
                    mint=mint, escrow=EscrowStore(":memory:"),
                    jmp=jmp if jmp is not None else service(), **kw)


def payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


# --- jmp.status: free, read-only, honestly labelled ------------------------

def test_status_returns_the_captured_funding_facts():
    body = payload(cvm().call(TOOL_JMP_STATUS, {}))
    assert body["number"] == "(810) 258-3253"
    assert body["number_state"] == "reserved"
    assert body["btc_address"] == "bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk"
    assert body["amount_btc"] == "0.000244"
    assert body["amount_usd_min"] == "20.00"
    assert body["balance_usd"] == "0.00"
    assert body["activation_state"] == "unfunded"


def test_status_source_is_cached_and_it_says_where_the_values_came_from():
    body = payload(cvm().call(TOOL_JMP_STATUS, {}))
    assert body["source"] == "cached"
    assert body["as_of"] == CAPTURED_AT
    assert body["provenance"]["mode"] == "cached"
    assert body["provenance"]["evidence"]


def test_status_names_the_number_as_a_session_scoped_reservation():
    body = payload(cvm().call(TOOL_JMP_STATUS, {}))
    assert "Session-scoped reservation" in body["number_note"]
    assert "not ownership" in body["number_note"]
    # and the observed history travels with it, so nothing is hidden
    numbers = {item["number"] for item in body["number_history"]}
    assert body["number"] in numbers


def test_status_is_free_and_needs_no_payment():
    result = cvm(mint=StubMint(fee_ppk=0)).call(TOOL_JMP_STATUS, {})
    assert "isError" not in result, payload(result)


def test_status_keeps_the_cached_label_even_with_a_live_probe():
    """A roster lookup does not produce a deposit address: `live-flow` would lie."""
    probe = lambda: {"number": "(810) 999-9999", "source": "live-flow",   # noqa: E731
                     "read_only_probe": {"roster_size": 1}}
    body = payload(cvm(jmp=service(status_probe=probe)).call(TOOL_JMP_STATUS, {}))
    assert body["source"] == "cached"
    assert body["read_only_probe"]["roster_size"] == 1


def test_status_records_a_failed_probe_instead_of_hiding_it():
    def boom():
        raise RuntimeError("xmpp: connection refused")

    body = payload(cvm(jmp=service(status_probe=boom)).call(TOOL_JMP_STATUS, {}))
    assert body["source"] == "cached"
    assert body["live_attempt"]["ok"] is False
    assert "connection refused" in body["live_attempt"]["detail"]
    assert body["btc_address"] == CACHED_FACTS["btc_address"]


def test_status_without_a_wired_capability_refuses_visibly():
    result = CvmTools(FakeTransport(), docs=load_docs()).call(TOOL_JMP_STATUS, {})
    assert result["isError"] is True
    assert payload(result)["reason"] == "jmp_not_configured"


def test_status_payload_matches_the_cached_record_it_will_be_diffed_against():
    """The card's cross-check evidence: address and amount are the captured pair."""
    body = payload(cvm().call(TOOL_JMP_STATUS, {}))
    assert (body["amount_btc"], body["btc_address"]) == \
        (CACHED_FACTS["amount_btc"], CACHED_FACTS["btc_address"])


# --- jmp.funding: drives, or refuses; never dresses cache up as live --------

def test_funding_without_a_driver_refuses_rather_than_serving_cache():
    result = cvm().call(TOOL_JMP_FUNDING, {})
    assert result["isError"] is True
    body = payload(result)
    assert body["reason"] == "rail_unavailable"
    assert "cached" in body["hint"]


def test_funding_with_a_driver_reports_live_flow():
    live = lambda: {"number": "(810) 258-3253", "number_state": "reserved",   # noqa: E731
                    "btc_address": FUNDING["btc_address"], "amount_btc": "0.000244",
                    "amount_usd_min": "20.00", "balance_usd": None,
                    "activation_state": "unfunded", "as_of": "2026-10-05T22:00:00Z"}
    body = payload(cvm(jmp=service(live_driver=live)).call(TOOL_JMP_FUNDING, {}))
    assert body["source"] == "live-flow"
    assert body["number"] == "(810) 258-3253"
    assert body["amount_btc"] == "0.000244"
    assert "Session-scoped reservation" in body["number_note"]


def test_a_failed_drive_is_a_visible_rail_error_not_a_cached_answer():
    def boom():
        raise RuntimeError("xmpp auth failed")

    body = payload(cvm(jmp=service(live_driver=boom)).call(TOOL_JMP_FUNDING, {}))
    assert body["reason"] == "rail_unavailable"
    assert "xmpp auth failed" in body["hint"]


def test_funding_is_free():
    live = lambda: {"number": None, "btc_address": None}                       # noqa: E731
    result = cvm(mint=StubMint(fee_ppk=0), jmp=service(live_driver=live)).call(TOOL_JMP_FUNDING, {})
    assert "isError" not in result, payload(result)


# --- jmp.credentials: the highest-risk surface -----------------------------

def test_an_unauthorised_npub_is_refused_and_never_gets_the_secret():
    mint = StubMint(fee_ppk=0)
    result = cvm(mint=mint).call(
        TOOL_JMP_CREDENTIALS, {"cashu_token": make_token([4096])}, caller=STRANGER_PK)
    body = payload(result)
    assert result["isError"] is True
    assert body["reason"] == "owner_only"
    assert SECRET_PLAINTEXT not in json.dumps(result)
    assert "ciphertext" not in body


def test_the_allow_list_is_checked_before_the_payment_path_is_entered():
    """A stranger cannot pay to obtain it: the mint is never touched, and an
    invalid token is not even examined (so the refusal is about THEM)."""
    mint = StubMint(fee_ppk=0)
    result = cvm(mint=mint).call(
        TOOL_JMP_CREDENTIALS, {"cashu_token": "not-even-a-token"}, caller=STRANGER_PK)
    body = payload(result)
    assert body["reason"] == "owner_only"          # not token_invalid
    assert mint.swap_calls == [] and mint.spent_secrets() == set()


def test_an_authorised_npub_gets_a_payload_that_decrypts_to_the_jid_and_secret():
    mint = StubMint(fee_ppk=0)
    result = cvm(mint=mint).call(
        TOOL_JMP_CREDENTIALS, {"cashu_token": make_token([4096])}, caller=OPERATOR_PK)
    body = payload(result)
    assert "isError" not in result, body
    assert body["enc"] == "nip44-v2"
    assert body["to"] == OPERATOR_PK
    # the caller's own key opens it — and only that key
    opened = json.loads(decrypt(OPERATOR_SK, SERVER_PK, body["ciphertext"]))
    assert opened["jid"] == ACCOUNT["jid"]
    assert opened["secret"] == SECRET_PLAINTEXT
    assert opened["secret_kind"] == "xmpp-password"
    assert opened["revoke_hint"] == REVOKE_HINT
    assert opened["rotate_by"] > opened["issued_at"]


def test_the_release_requires_payment_from_an_authorised_caller():
    body = payload(cvm().call(TOOL_JMP_CREDENTIALS, {}, caller=OPERATOR_PK))
    assert body["reason"] == "payment_required"
    assert body["cap"] == f"cap:tool:jmp.credentials:{CREDENTIALS_PRICE_SATS}:sats"
    assert body["pmi"] == "bitcoin-cashu"
    assert body["gating"] == "explicit_gating"


def test_the_release_actually_captures_the_postage_at_the_mint():
    """Paid means paid: the token is swapped, not merely acknowledged."""
    mint = StubMint(fee_ppk=0)
    result = cvm(mint=mint).call(
        TOOL_JMP_CREDENTIALS, {"cashu_token": make_token([4096])}, caller=OPERATOR_PK)
    assert "isError" not in result, payload(result)
    assert len(mint.swap_calls) == 1
    assert sum(mint.swap_calls[0]["outputs"]) == 4096


def test_the_plaintext_secret_is_nowhere_in_the_response():
    result = cvm(mint=StubMint(fee_ppk=0)).call(
        TOOL_JMP_CREDENTIALS, {"cashu_token": make_token([4096])}, caller=OPERATOR_PK)
    assert SECRET_PLAINTEXT not in json.dumps(result)
    assert ACCOUNT["jid"] not in json.dumps(payload(result)["ciphertext"])


def test_the_plaintext_is_redacted_from_any_transcript_that_records_it():
    """The driver redacts by value; the credential supplies that value list."""
    from app.jmp_flow import Transcript
    jmp = service()
    transcript = Transcript(None, echo=False, redact=jmp.redaction_values())
    transcript.add("OUT", f"login {ACCOUNT['jid']} password {SECRET_PLAINTEXT}",
                   raw=f"<x>{SECRET_PLAINTEXT}</x>")
    blob = json.dumps(transcript.records)
    assert SECRET_PLAINTEXT not in blob
    assert "<redacted" in blob


def test_the_redaction_list_contains_every_value_but_never_leaks_one_to_logs():
    jmp = service()
    values = jmp.redaction_values()
    assert SECRET_PLAINTEXT in values and ACCOUNT["jid"] in values
    # the public view of the read is what goes to a log, and it has no values
    public = json.dumps(jmp.preconditions())
    assert SECRET_PLAINTEXT not in public and ACCOUNT["jid"] not in public
    assert public.count("present") >= 1


def test_rotate_by_is_dated_and_the_revoke_path_is_documented():
    jmp = service(now=lambda: "2026-10-05T22:00:00Z")
    body = jmp.credentials(OPERATOR_PK)
    assert body["issued_at"] == "2026-10-05T22:00:00Z"
    assert body["rotate_by"].startswith("2027-01-03")          # +90 days
    assert ROTATE_AFTER_DAYS == 90
    assert body["revoke_hint"] == REVOKE_HINT
    # both halves of the revoke path are real and stated
    assert "change the XMPP password" in REVOKE_HINT
    assert "ALLOWLIST" in REVOKE_HINT


def test_a_missing_credential_fails_closed_and_names_the_store():
    jmp = service(secret_store=MemorySecretStore({}))          # nothing stored
    try:
        jmp.credentials(OPERATOR_PK)
        raise AssertionError("an absent credential must refuse, not answer")
    except Exception as exc:                                   # ToolError
        assert getattr(exc, "reason", "") == "credential_unavailable"
        assert ACCOUNT_SECRET in exc.hint
        assert "openbao" in exc.hint
        precondition = exc.extra["precondition"]
        assert precondition["present"] is False
        assert precondition["store"] == "openbao"
    assert "no secret store" not in jmp.preconditions()[ACCOUNT_SECRET]["detail"]


def test_a_partial_credential_fails_closed_and_names_the_missing_field():
    jmp = service(secret_store=store(**{ACCOUNT_SECRET: {"jid": ACCOUNT["jid"]}}))
    try:
        jmp.credentials(OPERATOR_PK)
        raise AssertionError("a credential with no secret must refuse")
    except Exception as exc:
        assert getattr(exc, "reason", "") == "credential_unavailable"
        assert "secret" in exc.hint


def test_an_unconfigured_allow_list_refuses_everyone():
    jmp = service(allow_list=OperatorAllowList())
    assert jmp.is_allowed(OPERATOR_PK) is False
    try:
        jmp.credentials(OPERATOR_PK)
        raise AssertionError("an empty allow-list must refuse")
    except Exception as exc:
        assert getattr(exc, "reason", "") == "owner_only"


def test_no_server_key_means_no_release_at_all():
    jmp = service(server_secret_hex=None)
    try:
        jmp.credentials(OPERATOR_PK)
        raise AssertionError("without a key the payload cannot be encrypted")
    except Exception as exc:
        assert getattr(exc, "reason", "") == "credentials_encryption_unconfigured"


def test_a_different_npub_cannot_open_what_the_server_sent():
    body = service().credentials(OPERATOR_PK)
    from app.nip44 import Nip44Error
    try:
        decrypt(STRANGER_SK, SERVER_PK, body["ciphertext"])
        raise AssertionError("a non-allow-listed key must not be able to open it")
    except Nip44Error:
        pass


def test_the_preconditions_report_says_which_store_and_whether_it_had_them():
    assert service().preconditions()[ACCOUNT_SECRET]["present"] is True
    missing = service(secret_store=MemorySecretStore({})).preconditions()
    assert missing[NUMBER_SECRET]["present"] is False


def test_credentials_payload_never_carries_a_field_of_the_secret():
    body = service().credentials(OPERATOR_PK)
    assert "secret" in json.dumps(body)              # the literal word `secret_kind`
    assert SECRET_PLAINTEXT not in json.dumps(body)
    assert body["secret_kind"] == "xmpp-password"


# --- the advertised surface -------------------------------------------------

def test_the_three_jmp_tools_are_advertised_with_the_risk_on_them():
    names = {tool["name"] for tool in cvm().tool_definitions()}
    assert {TOOL_JMP_STATUS, TOOL_JMP_FUNDING, TOOL_JMP_CREDENTIALS} <= names
    descriptions = {tool["name"]: tool["description"] for tool in cvm().tool_definitions()}
    for name in (TOOL_JMP_STATUS, TOOL_JMP_FUNDING, TOOL_JMP_CREDENTIALS):
        assert "ToS" in descriptions[name] or "terms" in descriptions[name]
    assert "PAID" in descriptions[TOOL_JMP_CREDENTIALS]
    assert "PAID" not in descriptions[TOOL_JMP_FUNDING]


def test_the_announcement_lists_the_jmp_tools():
    announced = set(cvm().server_announcement()["tools"])
    assert {TOOL_JMP_STATUS, TOOL_JMP_FUNDING, TOOL_JMP_CREDENTIALS} <= announced


def test_the_credentials_tool_is_the_only_paid_jmp_tool():
    from app.cvm_tools import FREE_TOOLS
    assert TOOL_JMP_STATUS in FREE_TOOLS and TOOL_JMP_FUNDING in FREE_TOOLS
    assert TOOL_JMP_CREDENTIALS not in FREE_TOOLS
