"""Rail: Telnyx — a thin adapter over the existing ``sms-gateway`` package.

This does not re-implement a provider. It imports ``TelnyxProvider`` and
``TelnyxConfig`` from a checkout of ``felixfelix-bot/sms-gateway`` (path from
``NOSMS_SMS_GATEWAY_PATH``, default ``~/repos/sms-gateway``) and translates that
package's pydantic results into this service's small ``SendResult`` /
``TransportStatus`` contract. Provider request construction, status mapping and
rate limiting stay in sms-gateway, where they are already tested.

One deliberate exception: the provider's own model does not carry the *raw*
status string, and this service must never turn a status it cannot read into a
"delivered". So ``raw_status`` reads ``GET /v1/messages/{id}`` through the
provider's own configured client — one extra field read, no duplicated logic.

Nothing here talks to Telnyx at import time; the network is only touched when a
send or a status poll actually happens, and an unconfigured adapter refuses to
send rather than failing late.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace

DEFAULT_SMS_GATEWAY_PATH = "~/repos/sms-gateway"

from .base import Capabilities, SendResult, TransportStatus, normalize_status


class SmsGatewayNotFound(Exception):
    """The sms-gateway checkout could not be found on disk."""


@dataclass
class _Gateway:
    TelnyxProvider: object
    TelnyxConfig: object


def load_sms_gateway(path: str | None = None) -> _Gateway:
    """Import the provider classes from a local sms-gateway checkout (never vendored)."""
    root = os.path.expanduser(path or os.environ.get("NOSMS_SMS_GATEWAY_PATH")
                              or DEFAULT_SMS_GATEWAY_PATH)
    if not os.path.isdir(os.path.join(root, "sms_gateway")):
        raise SmsGatewayNotFound(
            f"sms-gateway package not found at {root!r}; clone "
            f"felixfelix-bot/sms-gateway and point NOSMS_SMS_GATEWAY_PATH at it.")
    if root not in sys.path:
        sys.path.insert(0, root)
    provider_mod = importlib.import_module("sms_gateway.providers.telnyx")
    config_mod = importlib.import_module("sms_gateway.config")
    return _Gateway(TelnyxProvider=provider_mod.TelnyxProvider,
                    TelnyxConfig=config_mod.TelnyxConfig)


class TelnyxTransport:
    """Adapter: sms-gateway's TelnyxProvider behind the nosms Transport contract."""

    name = "telnyx"

    def __init__(self, provider=None, *, api_key: str | None = None,
                 from_number: str | None = None, configured: bool | None = None):
        self._provider = provider
        if provider is not None:
            self._api_key = api_key if api_key is not None else "injected"
            self._from_number = from_number if from_number is not None else "injected"
        else:
            self._api_key = api_key if api_key is not None else os.environ.get("TELNYX_API_KEY", "")
            self._from_number = from_number if from_number is not None else os.environ.get("TELNYX_FROM_NUMBER", "")
        self._configured = bool(configured) if configured is not None else bool(
            self._api_key and self._from_number)

    @classmethod
    def from_config(cls, sms_gateway_path: str | None = None, **overrides) -> "TelnyxTransport":
        """Build the adapter from the environment, importing sms-gateway by path."""
        gateway = load_sms_gateway(sms_gateway_path)
        api_key = overrides.get("api_key") or os.environ.get("TELNYX_API_KEY", "")
        from_number = overrides.get("from_number") or os.environ.get("TELNYX_FROM_NUMBER", "")
        profile_id = overrides.get("messaging_profile_id") or os.environ.get(
            "TELNYX_MESSAGING_PROFILE_ID", "")
        config = gateway.TelnyxConfig(api_key=api_key, messaging_profile_id=profile_id,
                                      from_number=from_number)
        transport = cls(gateway.TelnyxProvider(config), api_key=api_key, from_number=from_number)
        return transport

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(available=bool(self._configured and self._provider is not None),
                            best_effort=False, delivery_receipts=True,
                            countries=["*"])

    async def send(self, dest: str, body: str, **kwargs) -> SendResult:
        if not self._configured or self._provider is None:
            return SendResult(accepted=False, rail=self.name, best_effort=False, receipt=None,
                              status="failed",
                              detail="telnyx transport is not configured "
                                     "(set TELNYX_API_KEY and TELNYX_FROM_NUMBER)")
        result = await self._provider.send_sms(dest, body)
        status = normalize_status(getattr(result, "status", None))
        accepted = status in ("sent", "queued", "delivered")
        message_id = getattr(result, "message_id", "") or None
        if not accepted or not message_id:
            return SendResult(accepted=False, rail=self.name, best_effort=False, receipt=None,
                              status="failed",
                              detail=str(getattr(result, "error", "") or "provider refused the send"))
        return SendResult(accepted=True, rail=self.name, best_effort=False, receipt=message_id,
                          detail=status, status=status)

    async def status(self, receipt: str) -> TransportStatus:
        if not receipt or self._provider is None:
            return TransportStatus(normalized="unknown", raw="unknown", detail="no provider id")
        delivery = await self._provider.check_delivery(receipt)
        normalized = normalize_status(getattr(delivery, "status", None))
        raw = await self.raw_status(receipt)
        return TransportStatus(normalized=normalized, raw=raw or normalized,
                              detail=str(getattr(delivery, "error", "") or ""))

    async def raw_status(self, receipt: str) -> str | None:
        """The provider's own status string, unmodified."""
        reader = getattr(self._provider, "raw_status", None)
        if callable(reader):
            try:
                value = reader(receipt)
                if hasattr(value, "__await__"):
                    value = await value
                if value:
                    return str(value)
            except Exception:                                    # noqa: BLE001
                pass
        client_factory = getattr(self._provider, "_get_client", None)
        if callable(client_factory):
            try:
                resp = await client_factory().get(f"/messages/{receipt}")
                resp.raise_for_status()
                return (resp.json().get("data") or {}).get("status")
            except Exception:                                    # noqa: BLE001
                return None
        return None
