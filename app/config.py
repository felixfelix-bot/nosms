"""Runtime configuration for the nosms service core.

Everything is env-driven: no secrets in code, no outbound probes. The
transport *name* reported by /api/health is read from config, never discovered
by calling the provider (a health check must stay cheap and side-effect free).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace


def _float_env(raw, default: float) -> float:
    """Parse a numeric env var, tolerating garbage.

    A malformed value must not take the service down, and must not silently
    shorten a timeout (the unsafe direction for a send). Garbage -> the default.
    """
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _git_sha() -> str | None:
    """Best-effort short commit sha of the deployed checkout. Never raises."""
    for var in ("NOSMS_COMMIT", "NOSMS_BUILD_SHA", "GIT_COMMIT"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:                                    # noqa: BLE001
        pass
    return None


@dataclass(frozen=True)
class Config:
    service: str = "nosms"
    version: str = "0.1.0"
    commit: str | None = None
    transport: str = "fake"
    freshness_seconds: int = 120          # NIP-98 +/- window (card: 120 s)
    replay_ttl_seconds: int = 300         # must be >= freshness window

    # --- M1b: escrow / postage -------------------------------------------
    #: the mint every postage token is escrowed against (testnut in dev).
    mint_url: str = "https://testnut.cashu.space"
    #: sqlite file for escrow + quota state. ':memory:' keeps tests clean.
    escrow_db: str = ":memory:"
    #: how long a message may stay undelivered before the sweep refunds it.
    refund_after_seconds: int = 900
    #: per (identity, destination) cooldown. 0 disables.
    destination_cooldown_seconds: int = 60
    #: per-identity cap over a rolling 24 h. 0 disables.
    daily_cap: int = 100
    #: sms-gateway checkout the Telnyx rail imports (never vendored).
    sms_gateway_path: str = "~/repos/sms-gateway"
    #: longest message body accepted by the HTTP surface.
    max_body_chars: int = 640
    #: flat postage for one message, per ADR-0002 (the rugpull-risk premium).
    #: The formula and the walk-down live in app/pricing.py; this is the value
    #: the HTTP contract advertises, kept in sync by tests/test_llms_artifact.py.
    price_sats: int = 2900
    #: where the CVM contract is served, advertised in the CEP-6 catalog.
    cvm_contract_url: str = "https://nosms.orangesync.tech/cvm/llms.txt"

    # --- WhatsApp rail (ADR-0003) ----------------------------------------
    #: adb device serial of the emulator running the official WhatsApp client,
    #: e.g. ``emulator-5554``. Empty means *no device*: the rail then reports
    #: itself unavailable and refuses to send rather than failing late.
    whatsapp_serial: str = ""
    #: the adb binary (name or path) used to address that device.
    whatsapp_adb: str = "adb"
    #: the AVD the lifecycle harness owns (``tools/emulator/emulator-harness.sh``).
    whatsapp_avd: str = "wa-dev"
    #: how long one send on the device may take, and how long one UI read may take.
    whatsapp_send_timeout_seconds: float = 90.0
    whatsapp_ui_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: dict | None = None, **overrides) -> "Config":
        e = os.environ if env is None else env
        cfg = cls(
            service=e.get("NOSMS_SERVICE", "nosms"),
            version=e.get("NOSMS_VERSION", "0.1.0"),
            commit=e.get("NOSMS_COMMIT") or e.get("NOSMS_BUILD_SHA") or _git_sha(),
            transport=e.get("NOSMS_TRANSPORT", "fake"),
            freshness_seconds=int(e.get("NOSMS_NIP98_FRESHNESS_SECONDS", "120")),
            replay_ttl_seconds=int(e.get("NOSMS_REPLAY_TTL_SECONDS", "300")),
            mint_url=e.get("NOSMS_MINT_URL", "https://testnut.cashu.space"),
            escrow_db=e.get("NOSMS_DB_PATH", ":memory:"),
            refund_after_seconds=int(e.get("NOSMS_REFUND_AFTER_SECONDS", "900")),
            destination_cooldown_seconds=int(
                e.get("NOSMS_DESTINATION_COOLDOWN_SECONDS", "60")),
            daily_cap=int(e.get("NOSMS_DAILY_CAP", "100")),
            sms_gateway_path=e.get("NOSMS_SMS_GATEWAY_PATH", "~/repos/sms-gateway"),
            max_body_chars=int(e.get("NOSMS_MAX_BODY_CHARS", "640")),
            price_sats=int(e.get("NOSMS_PRICE_SATS", "2900")),
            cvm_contract_url=e.get("NOSMS_CVM_CONTRACT_URL",
                                   "https://nosms.orangesync.tech/cvm/llms.txt"),
            whatsapp_serial=(e.get("NOSMS_WHATSAPP_SERIAL") or "").strip(),
            whatsapp_adb=e.get("NOSMS_WHATSAPP_ADB") or "adb",
            whatsapp_avd=e.get("NOSMS_WHATSAPP_AVD") or "wa-dev",
            whatsapp_send_timeout_seconds=_float_env(
                e.get("NOSMS_WHATSAPP_SEND_TIMEOUT_SECONDS"), 90.0),
            whatsapp_ui_timeout_seconds=_float_env(
                e.get("NOSMS_WHATSAPP_UI_TIMEOUT_SECONDS"), 30.0),
        )
        if overrides:
            cfg = replace(cfg, **{k: v for k, v in overrides.items()
                                  if v is not None or k in cls.__dataclass_fields__})
        # the replay cache must outlive the freshness window
        if cfg.replay_ttl_seconds < cfg.freshness_seconds:
            cfg = replace(cfg, replay_ttl_seconds=cfg.freshness_seconds)
        return cfg
