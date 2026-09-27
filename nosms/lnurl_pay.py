"""LNURL-pay (LUD-16) resolution + invoice requests.

Why this module exists
----------------------
Machankura (`8333.mobi`) is a Bitcoin/Lightning wallet reachable from any
phone over USSD/SMS, with no app and no internet. Every Machankura user
therefore has a *lightning address* of the form::

    <phone in international format, no plus>@8333.mobi
    <username>@8333.mobi

which resolves through the standard LNURL-pay endpoint::

    GET https://<host>/.well-known/lnurlp/<user>

That gives nosms a sats rail for people who have a handset but neither
internet nor email: they can pay us from their keypad, and we can push a
refund back to their phone number. Machankura advertises ``commentAllowed``
(the observed value is 60), so a payer can attach a short comment — for a
USSD/SMS user that comment is the only data channel available, since they
cannot open a link.

Everything in this module is protocol-level and pure: **no HTTP is performed
unless a transport is passed in explicitly** (``transport=...``), and no
sat is ever moved here. Wiring an actual wallet is
:mod:`nosms.payments`' job, behind ``LnBackend``.

Observed live responses (2026-09-27, read-only, two GETs) are captured as
fixtures under ``tests/fixtures/`` and cited in ``tests/test_lnurl_pay.py``.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Union, runtime_checkable

__all__ = [
    "MACHANKURA_HOST",
    "LNURLP_PATH",
    "DEFAULT_TIMEOUT",
    "HttpResult",
    "Transport",
    "UrllibTransport",
    "LightningAddress",
    "PayRequest",
    "Invoice",
    "resolve",
    "resolve_url",
    "request_invoice",
    "LnurlError",
    "MalformedAddress",
    "TransportFailure",
    "UnknownUser",
    "ServiceError",
    "NotAPayRequest",
    "InvalidPayRequest",
    "AmountOutOfRange",
    "CommentError",
    "CommentNotAllowed",
    "CommentTooLong",
    "InvoiceRequestFailed",
]

#: The host this integration was built for (Machankura).
MACHANKURA_HOST = "8333.mobi"

#: The LUD-16 well-known path.
LNURLP_PATH = "/.well-known/lnurlp/"

#: Default per-request timeout for :class:`UrllibTransport`.
DEFAULT_TIMEOUT = 10.0

# LUD-16 local part: lowercase alphanumerics plus `-_.+`. We normalise case
# instead of rejecting it (a user pasting `Sigidli@8333.mobi` means the same
# person), but a local part that is *structurally* wrong is a hard error.
_USER_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,127}$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_PHONE_RE = re.compile(r"^[0-9]{6,15}$")


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class LnurlError(Exception):
    """Base class for every failure raised by this module."""

    def __init__(self, message: str, *, address: Optional["LightningAddress"] = None) -> None:
        super().__init__(message)
        self.message = message
        self.address = address

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class MalformedAddress(LnurlError):
    """The ``user@host`` lightning address could not be parsed."""

    def __init__(self, raw: object, reason: str) -> None:
        super().__init__(f"malformed lightning address {raw!r}: {reason}")
        self.raw = raw
        self.reason = reason


class TransportFailure(LnurlError):
    """The injected transport could not complete the request at all."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"transport failed for {url}: {reason}")
        self.url = url
        self.reason = reason


class UnknownUser(LnurlError):
    """``404`` — the host answered, the identifier isn't a user of the service.

    Machankura's body is ``{"status":"error","reason":"User <x> doesn't use the
    Machankura service."}``; ``reason`` carries that string verbatim.
    """

    def __init__(self, address: "LightningAddress", reason: str, status_code: int = 404) -> None:
        super().__init__(f"{address.identifier} is unknown to {address.host}: {reason}", address=address)
        self.identifier = address.identifier
        self.reason = reason
        self.status_code = status_code


class ServiceError(LnurlError):
    """The endpoint answered with an unusable status code or an error document."""

    def __init__(self, address: "LightningAddress", reason: str, status_code: int) -> None:
        super().__init__(f"{address.identifier}: service error (HTTP {status_code}): {reason}", address=address)
        self.reason = reason
        self.status_code = status_code


class NotAPayRequest(LnurlError):
    """The document is not an LNURL-pay ``payRequest``."""

    def __init__(self, address: "LightningAddress", tag: object, reason: str = "") -> None:
        detail = f"tag={tag!r}" + (f" ({reason})" if reason else "")
        super().__init__(f"{address.identifier}: not a payRequest: {detail}", address=address)
        self.tag = tag
        self.reason = reason


class InvalidPayRequest(LnurlError):
    """The ``payRequest`` document is missing fields or carries invalid values."""

    def __init__(self, address: "LightningAddress", reason: str) -> None:
        super().__init__(f"{address.identifier}: invalid payRequest: {reason}", address=address)
        self.reason = reason


class AmountOutOfRange(LnurlError):
    """Requested amount (msat) is outside the advertised min/max."""

    def __init__(self, amount_msat: int, min_msat: int, max_msat: int, address: Optional["LightningAddress"] = None) -> None:
        super().__init__(
            f"amount {amount_msat} msat outside [{min_msat}, {max_msat}]",
            address=address,
        )
        self.amount_msat = amount_msat
        self.min_msat = min_msat
        self.max_msat = max_msat


class CommentError(LnurlError):
    """A comment was rejected locally, before any HTTP request was made."""

    def __init__(self, message: str, *, limit: int, attempted: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.attempted = attempted


class CommentNotAllowed(CommentError):
    """The receiver advertises ``commentAllowed: 0``."""

    def __init__(self, limit: int, attempted: int) -> None:
        super().__init__(
            f"receiver advertises commentAllowed={limit}; refusing to send a {attempted}-char comment",
            limit=limit,
            attempted=attempted,
        )


class CommentTooLong(CommentError):
    """The comment exceeds the receiver's advertised ``commentAllowed``."""

    def __init__(self, limit: int, attempted: int) -> None:
        super().__init__(
            f"comment is {attempted} chars but receiver allows {limit}",
            limit=limit,
            attempted=attempted,
        )


class InvoiceRequestFailed(LnurlError):
    """The callback did not return a bolt11 invoice."""

    def __init__(self, callback: str, reason: str, status_code: Optional[int] = None) -> None:
        status = "" if status_code is None else f" (HTTP {status_code})"
        super().__init__(f"invoice request to {callback} failed{status}: {reason}")
        self.callback = callback
        self.reason = reason
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HttpResult:
    """A raw HTTP response: status, bytes, and the URL that produced it."""

    url: str
    status_code: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@runtime_checkable
class Transport(Protocol):
    """The only thing this module needs from the network.

    Implementations MUST return an :class:`HttpResult` for every HTTP status
    (including 4xx/5xx) and raise :class:`TransportFailure` only when the
    request never completed (DNS, TLS, timeout).
    """

    def get(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> HttpResult:  # pragma: no cover - protocol
        ...


class UrllibTransport:
    """Stdlib-only :class:`Transport`. The only production implementation."""

    def __init__(self, *, user_agent: str = "nosms-lnurl/0.1 (+https://nosms.orangesync.tech)") -> None:
        self.user_agent = user_agent

    def get(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> HttpResult:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResult(url=url, status_code=int(response.status), body=response.read())
        except urllib.error.HTTPError as exc:  # a real answer we must read (404 carries `reason`)
            return HttpResult(url=url, status_code=int(exc.code), body=exc.read())
        except urllib.error.URLError as exc:
            raise TransportFailure(url, str(exc.reason)) from exc
        except OSError as exc:  # socket timeouts, reset connections
            raise TransportFailure(url, str(exc)) from exc


# --------------------------------------------------------------------------- #
# address
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LightningAddress:
    """A parsed ``user@host`` lightning address (LUD-16)."""

    user: str
    host: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.identifier

    @property
    def identifier(self) -> str:
        return f"{self.user}@{self.host}"

    @property
    def is_machankura(self) -> bool:
        """True when the host is Machankura — i.e. this is somebody's phone."""
        return self.host == MACHANKURA_HOST

    @classmethod
    def parse(cls, raw: object) -> "LightningAddress":
        """Parse ``user@host``.

        Raises :class:`MalformedAddress` for anything structurally wrong: a
        missing or doubled ``@``, whitespace, a non-domain host, or a local
        part outside ``[a-z0-9._+-]``. Case is folded, so
        ``Sigidli@8333.MOBI`` == ``sigidli@8333.mobi``.
        """
        if not isinstance(raw, str):
            raise MalformedAddress(raw, "expected a string")
        candidate = raw.strip()
        if not candidate:
            raise MalformedAddress(raw, "empty address")
        if any(ch.isspace() for ch in candidate):
            raise MalformedAddress(raw, "contains whitespace")
        if candidate.count("@") != 1:
            raise MalformedAddress(raw, "expected exactly one '@'")
        user, _, host = candidate.partition("@")
        if not user or not host:
            raise MalformedAddress(raw, "both the user and the host are required")
        user = user.lower()
        host = host.lower().rstrip(".")
        if not _USER_RE.match(user):
            raise MalformedAddress(raw, f"invalid local part {user!r}")
        labels = host.split(".")
        if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
            raise MalformedAddress(raw, f"invalid host {host!r}")
        return cls(user=user, host=host)

    @classmethod
    def from_phone(cls, number: object, host: str = MACHANKURA_HOST) -> "LightningAddress":
        """Build an address from a phone number in international format.

        Accepts ``+255 679 066 977``, ``+255-679-066-977``, ``255679066977`` —
        Machankura's identifier is the digits only, no plus and no separators.
        """
        if not isinstance(number, str):
            raise MalformedAddress(number, "expected a phone number string")
        digits = re.sub(r"[^0-9]", "", number)
        if not _PHONE_RE.match(digits):
            raise MalformedAddress(number, "not an international phone number (6-15 digits, no leading +)")
        return cls(user=digits, host=host.lower())


def resolve_url(address: LightningAddress) -> str:
    """The LNURL-pay discovery URL for ``address`` (always https)."""
    return f"https://{address.host}{LNURLP_PATH}{urllib.parse.quote(address.user, safe='')}"


# --------------------------------------------------------------------------- #
# payRequest
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PayRequest:
    """A validated LNURL-pay ``payRequest`` (LUD-06)."""

    address: LightningAddress
    callback: str
    min_sendable_msat: int
    max_sendable_msat: int
    metadata: str
    allows_nostr: bool = False
    nostr_pubkey: Optional[str] = None
    comment_allowed: int = 0
    tag: str = "payRequest"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def identifier(self) -> str:
        return self.address.identifier

    @property
    def is_machankura(self) -> bool:
        return self.address.is_machankura

    @property
    def min_sendable_sats(self) -> float:
        return self.min_sendable_msat / 1000

    @property
    def max_sendable_sats(self) -> float:
        return self.max_sendable_msat / 1000

    def metadata_entries(self) -> list[Any]:
        """``metadata`` is a JSON-encoded array; return it decoded."""
        try:
            decoded = json.loads(self.metadata)
        except (TypeError, ValueError):  # pragma: no cover - validated earlier
            return []
        return decoded if isinstance(decoded, list) else []

    def check_amount(self, amount_msat: object) -> int:
        """Return ``amount_msat`` if it is a valid in-range amount, else raise."""
        amount = _require_positive_int(amount_msat, "amount_msat")
        if amount < self.min_sendable_msat or amount > self.max_sendable_msat:
            raise AmountOutOfRange(amount, self.min_sendable_msat, self.max_sendable_msat, self.address)
        return amount

    def check_comment(self, comment: Optional[str]) -> Optional[str]:
        """Validate a comment against ``commentAllowed`` *before* any HTTP call."""
        if comment is None:
            return None
        if not isinstance(comment, str):
            raise CommentError("comment must be a string", limit=self.comment_allowed, attempted=-1)
        if self.comment_allowed <= 0:
            raise CommentNotAllowed(self.comment_allowed, len(comment))
        if len(comment) > self.comment_allowed:
            raise CommentTooLong(self.comment_allowed, len(comment))
        return comment


@dataclass(frozen=True)
class Invoice:
    """An invoice returned by a ``payRequest`` callback."""

    pr: str
    pay_request: PayRequest
    amount_msat: int
    comment: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def identifier(self) -> str:
        return self.pay_request.identifier


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _decode_json(result: HttpResult, address: LightningAddress) -> Any:
    try:
        return json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ServiceError(address, f"response is not JSON ({exc})", result.status_code) from exc


def _reason_of(document: Any, default: str) -> str:
    if isinstance(document, Mapping):
        reason = document.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    return default


def _parse_pay_request(address: LightningAddress, document: Any, status_code: int) -> PayRequest:
    if not isinstance(document, Mapping):
        raise InvalidPayRequest(address, f"expected a JSON object, got {type(document).__name__}")

    if document.get("status") == "ERROR" or document.get("status") == "error":
        raise ServiceError(address, _reason_of(document, "no reason given"), status_code)

    tag = document.get("tag")
    if tag != "payRequest":
        raise NotAPayRequest(address, tag)

    callback = document.get("callback")
    if not isinstance(callback, str) or not callback:
        raise InvalidPayRequest(address, "missing callback")
    parsed_callback = urllib.parse.urlsplit(callback)
    if parsed_callback.scheme not in ("http", "https") or not parsed_callback.netloc:
        raise InvalidPayRequest(address, f"callback is not an absolute URL: {callback!r}")
    if parsed_callback.scheme != "https" and parsed_callback.hostname not in ("localhost", "127.0.0.1"):
        raise InvalidPayRequest(address, f"refusing a non-https callback: {callback!r}")

    minimum = document.get("minSendable")
    maximum = document.get("maxSendable")
    for name, value in (("minSendable", minimum), ("maxSendable", maximum)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidPayRequest(address, f"{name} must be an integer number of msat, got {value!r}")
        if value < 0:
            raise InvalidPayRequest(address, f"{name} must not be negative, got {value}")
    if maximum <= 0:
        raise InvalidPayRequest(address, "maxSendable must be > 0")
    if minimum > maximum:
        raise InvalidPayRequest(address, f"minSendable {minimum} > maxSendable {maximum}")

    metadata = document.get("metadata")
    if not isinstance(metadata, str):
        raise InvalidPayRequest(address, "metadata must be a JSON-encoded string")
    try:
        metadata_document = json.loads(metadata)
    except ValueError as exc:
        raise InvalidPayRequest(address, f"metadata is not valid JSON ({exc})") from exc
    if not isinstance(metadata_document, list):
        raise InvalidPayRequest(address, "metadata must decode to a JSON array")

    allows_nostr = document.get("allowsNostr", False)
    if not isinstance(allows_nostr, bool):
        raise InvalidPayRequest(address, f"allowsNostr must be a boolean, got {allows_nostr!r}")
    nostr_pubkey = document.get("nostrPubkey")
    if nostr_pubkey is not None:
        if not isinstance(nostr_pubkey, str) or not _HEX64_RE.match(nostr_pubkey.lower()):
            raise InvalidPayRequest(address, f"nostrPubkey is not 64-char hex: {nostr_pubkey!r}")
        nostr_pubkey = nostr_pubkey.lower()
    if allows_nostr and nostr_pubkey is None:
        raise InvalidPayRequest(address, "allowsNostr is true but nostrPubkey is missing")

    comment_allowed = document.get("commentAllowed", 0)
    if isinstance(comment_allowed, bool) or not isinstance(comment_allowed, int) or comment_allowed < 0:
        raise InvalidPayRequest(address, f"commentAllowed must be a non-negative integer, got {comment_allowed!r}")

    return PayRequest(
        address=address,
        callback=callback,
        min_sendable_msat=minimum,
        max_sendable_msat=maximum,
        metadata=metadata,
        allows_nostr=allows_nostr,
        nostr_pubkey=nostr_pubkey,
        comment_allowed=comment_allowed,
        tag=tag,
        raw=document,
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
AddressLike = Union[str, LightningAddress]


def _as_address(address: AddressLike) -> LightningAddress:
    return address if isinstance(address, LightningAddress) else LightningAddress.parse(address)


def resolve(address: AddressLike, *, transport: Transport, timeout: float = DEFAULT_TIMEOUT) -> PayRequest:
    """Resolve ``<identifier>@<host>`` to a validated :class:`PayRequest`.

    Raises :class:`UnknownUser` on 404 (Machankura's "doesn't use the
    Machankura service" answer), :class:`ServiceError` on any other unusable
    status, and :class:`NotAPayRequest` / :class:`InvalidPayRequest` when the
    document does not survive validation.
    """
    addr = _as_address(address)
    url = resolve_url(addr)
    result = transport.get(url, timeout=timeout)
    document = _decode_json(result, addr)

    if result.status_code == 404:
        raise UnknownUser(addr, _reason_of(document, "unknown user"), status_code=404)
    if result.status_code != 200:
        raise ServiceError(addr, _reason_of(document, f"unexpected status {result.status_code}"), result.status_code)
    return _parse_pay_request(addr, document, result.status_code)


def invoice_callback_url(pay_request: PayRequest, amount_msat: int, comment: Optional[str] = None) -> str:
    """Build the invoice callback URL (amount in msat, comment when allowed)."""
    parts = urllib.parse.urlsplit(pay_request.callback)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("amount", str(amount_msat)))
    if comment is not None:
        query.append(("comment", comment))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def request_invoice(
    pay_request: PayRequest,
    amount_msat: int,
    comment: Optional[str] = None,
    *,
    transport: Transport,
    timeout: float = DEFAULT_TIMEOUT,
) -> Invoice:
    """Ask the callback for a bolt11 invoice for ``amount_msat`` + ``comment``.

    Both validations happen *locally and before* the callback is called: the
    amount must be inside the advertised ``[minSendable, maxSendable]`` range,
    and a comment must fit the advertised ``commentAllowed``. A comment the
    receiver never advertised support for is refused outright rather than
    silently dropped — the payer's only channel for a reference code would
    otherwise disappear without a trace.
    """
    amount = pay_request.check_amount(amount_msat)
    accepted_comment = pay_request.check_comment(comment)

    url = invoice_callback_url(pay_request, amount, accepted_comment)
    result = transport.get(url, timeout=timeout)
    document = _decode_json(result, pay_request.address)

    if result.status_code != 200:
        raise InvoiceRequestFailed(
            pay_request.callback,
            _reason_of(document, f"unexpected status {result.status_code}"),
            result.status_code,
        )
    if not isinstance(document, Mapping):
        raise InvoiceRequestFailed(pay_request.callback, f"expected a JSON object, got {type(document).__name__}")
    if document.get("status") in ("ERROR", "error"):
        raise InvoiceRequestFailed(pay_request.callback, _reason_of(document, "no reason given"))
    pr = document.get("pr")
    if not isinstance(pr, str) or not pr.startswith("ln"):
        raise InvoiceRequestFailed(pay_request.callback, f"no bolt11 invoice in response (pr={pr!r})")

    return Invoice(pr=pr, pay_request=pay_request, amount_msat=amount, comment=accepted_comment, raw=document)
