"""Rail: WhatsApp — the official Android client driven over ``adb`` (ADR-0003).

Mechanism: the real WhatsApp Android app runs on an emulator host (dq05, see
``tools/emulator/``); this rail addresses it through ``adb``. Opening a chat uses
the ``https://wa.me/<number>`` deep link, the body is typed with
``adb shell input text``, and the message is submitted with KEYCODE_ENTER. The UI
tree is then scraped with ``uiautomator dump`` and classified into this service's
status vocabulary.

Honesty rules baked in (see ``docs/adr/0003-whatsapp-rail.md``)
---------------------------------------------------------------
* **``delivery_receipts = False``, permanently.** A scraped UI render is *not* a
  delivery proof — it is our own reading of pixels on a guest we control, it can
  be stale, and it cannot be attested to anyone else. The scraped string is kept
  in :attr:`SendResult.detail` for humans; it never becomes the service's
  ``delivered``.
* **A scraped "delivered" is normalised to ``sent``.** :data:`UI_STATUS_MAP`
  downgrades every delivered/read signal on purpose, and a test pins that
  downgrade so it cannot be "improved" away.
* **``best_effort = True``** (the plan's conservative default). Nothing in the
  emulator harness can demonstrate anything stronger, so this is not a
  placeholder — and it is the flag a caller reads, so it must not be aspirational.
* **No ``receipt`` is ever returned.** There is no provider-issued id here, and
  manufacturing a local one would let the refund sweep read our own token as a
  delivery handle (the ``email_gateway.py`` pattern). Because there is no
  receipt, this rail is deliberately **not** :class:`Pollable`: the transport
  endpoint can never poll it, so it must not pretend to.
* **A ban fails loudly.** A detected ban or an unregistered client raises
  :class:`RailUnavailable` with a machine token rather than returning a failed
  :class:`SendResult`, so the caller can stop and alert. Never retry a ban: the
  retry loop is what turns a warning into a permanent ban. (Alerting, the pacing
  hook and the degrade path land with T4.)

The device layer is injected
----------------------------
:class:`AdbWhatsAppDriver` is the only object that touches the emulator, and it
takes its ``runner`` (the thing that actually executes ``adb``) as a parameter.
That is what lets the whole rail — argument construction, UI classification,
status normalisation and every failure path — be exercised with **no emulator and
no network**. ``subprocess.run`` is only the default.
"""
from __future__ import annotations

import html
import os
import subprocess
from dataclasses import dataclass

from .base import (Capabilities, NORMALIZED_STATUSES, SendResult, normalize_status)
from .errors import RailUnavailable, UnsupportedDestination

__all__ = [
    "AdbWhatsAppDriver", "DriverResult", "UI_STATUS_MAP", "WhatsAppConfig",
    "WhatsAppTransport", "normalize_ui_status",
]

#: The emulator host facts live in ``tools/emulator/`` (AVD ``wa-dev`` on dq05).
DEFAULT_ADB = "adb"
DEFAULT_AVD_NAME = "wa-dev"
DEFAULT_SEND_TIMEOUT_S = 90.0
DEFAULT_UI_TIMEOUT_S = 30.0

#: Where the scraped UI tree is written on the *guest*.
UI_DUMP_PATH = "/sdcard/nosms-whatsapp-ui.xml"

#: UI states this rail can read out of the client, mapped onto the service's
#: five-value vocabulary. Every ``delivered``/read signal is downgraded to
#: ``sent``: a scrape is not a delivery proof. ``banned``/``unregistered`` are
#: ``failed`` here **and** handled explicitly by :meth:`WhatsAppTransport.send`,
#: which raises instead of returning.
UI_STATUS_MAP: dict[str, str] = {
    "composing": "queued",
    "not_sent": "queued",
    "queued": "queued",
    "sending": "queued",
    "unknown": "unknown",
    "sent": "sent",
    "single_tick": "sent",
    "delivered": "sent",       # downgraded on purpose (see module docstring)
    "read": "sent",            # downgraded on purpose
    "two_ticks": "sent",       # downgraded on purpose
    "banned": "failed",
    "unregistered": "failed",
    "error": "failed",
    "failed": "failed",
    "message_not_sent": "failed",
}

#: Substrings of a scraped UI tree, checked in this order. ``FAILED`` is checked
#: **before** ``SENT`` because "message not sent" contains "sent" — the classic
#: ordering bug this rail must not have.
BAN_MARKERS = (
    "banned from using whatsapp", "account is banned", "you are banned",
    "temporarily banned",
)
REGISTRATION_MARKERS = (
    "verify your phone number", "verify your number", "registration",
)
FAILED_MARKERS = (
    "message not sent", "couldn't send", "could not send", "try again",
)
DELIVERED_MARKERS = ("delivered", "read")
SENT_MARKERS = ("sent", "sending")

#: Characters ``adb shell input text`` would eat or misread on the guest shell.
_INPUT_SPECIAL = set("\\'\"`$&|;<>()*?[]{}~!#")


def _digits(dest: str) -> str:
    """E.164 destination -> the bare digits ``wa.me`` expects."""
    return "".join(ch for ch in (dest or "") if ch.isdigit())


def _escape_input(body: str) -> str:
    """Escape a body for ``adb shell input text``.

    ``input text`` runs the argument through the *guest's* shell: a literal space
    is a separator (the documented spelling is ``%s``), and shell metacharacters
    must be backslash-escaped or they are interpreted on the device.
    """
    out = []
    for ch in body:
        if ch == " ":
            out.append("%s")
        elif ch in _INPUT_SPECIAL:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _classify_ui(xml: str) -> str:
    """Classify a scraped ``uiautomator`` dump. Ban > registration > failed > rest.

    The dump is XML, so ``&apos;``/``&quot;`` must be unescaped first: the marker
    "couldn't send" never matches the raw attribute ``Couldn&apos;t send``.
    """
    text = html.unescape(xml or "").lower()
    for marker in BAN_MARKERS:
        if marker in text:
            return "banned"
    for marker in REGISTRATION_MARKERS:
        if marker in text:
            return "unregistered"
    for marker in FAILED_MARKERS:
        if marker in text:
            return "failed"
    for marker in DELIVERED_MARKERS:
        if marker in text:
            return "delivered"
    for marker in SENT_MARKERS:
        if marker in text:
            return "sent"
    return "unknown"


def normalize_ui_status(ui_state: str) -> str:
    """Map a scraped UI state onto :data:`NORMALIZED_STATUSES`.

    An unknown state normalises to ``unknown`` — never to something optimistic.
    """
    return UI_STATUS_MAP.get((ui_state or "").strip().lower(), "unknown")


def _float_env(raw: str | None, default: float) -> float:
    """Parse a timeout from the environment, tolerating garbage.

    A malformed timeout must not take the service down: falling back to the
    default is the safe direction (a *shorter* timeout would abort sends).
    """
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WhatsAppConfig:
    """Everything this rail needs to find one emulator. Env-driven, no secrets."""

    adb: str = DEFAULT_ADB
    #: the adb device serial, e.g. ``emulator-5554``. Empty means "no device":
    #: the rail is then unconfigured and refuses to send rather than failing late.
    serial: str = ""
    avd_name: str = DEFAULT_AVD_NAME
    send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_S
    ui_timeout_seconds: float = DEFAULT_UI_TIMEOUT_S

    @classmethod
    def from_env(cls, env: dict | None = None) -> "WhatsAppConfig":
        e = os.environ if env is None else env
        return cls(
            adb=e.get("NOSMS_WHATSAPP_ADB") or DEFAULT_ADB,
            serial=(e.get("NOSMS_WHATSAPP_SERIAL") or "").strip(),
            avd_name=e.get("NOSMS_WHATSAPP_AVD") or DEFAULT_AVD_NAME,
            send_timeout_seconds=_float_env(
                e.get("NOSMS_WHATSAPP_SEND_TIMEOUT_SECONDS"), DEFAULT_SEND_TIMEOUT_S),
            ui_timeout_seconds=_float_env(
                e.get("NOSMS_WHATSAPP_UI_TIMEOUT_SECONDS"), DEFAULT_UI_TIMEOUT_S),
        )


@dataclass(frozen=True)
class DriverResult:
    """What the driver could read back after a send attempt.

    ``ui_state`` is a *classification* (:func:`_classify_ui`), not proof of
    anything; ``detail`` is the human-readable trace.
    """

    ui_state: str
    detail: str = ""


class AdbWhatsAppDriver:
    """The only object that talks to the emulator. Everything else is contract.

    ``runner`` is ``(argv, timeout) -> CompletedProcess``; it is injected so the
    whole driver runs offline, and ``subprocess.run`` is merely the default.
    """

    def __init__(self, config: WhatsAppConfig | None = None, *, runner=None):
        self.config = config or WhatsAppConfig()
        self._runner = runner or _default_runner

    def _adb(self, *args: str, timeout: float | None = None):
        """Run one ``adb`` subcommand against the configured device."""
        argv = [self.config.adb]
        if self.config.serial:
            argv += ["-s", self.config.serial]
        argv += list(args)
        return self._runner(argv, timeout or self.config.ui_timeout_seconds)

    def device_online(self) -> bool:
        """True only when adb reports this device in the ``device`` state.

        This is the explicit liveness probe. It is deliberately **not** called by
        :meth:`WhatsAppTransport.capabilities`: a capability read must stay cheap
        and side-effect free (``app/config.py``), so ``available`` means
        "addressed by config", and this method means "reachable right now".
        """
        try:
            res = self._adb("get-state")
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0 and (res.stdout or "").strip() == "device"

    def dump_ui(self) -> str:
        """The guest's UI tree as XML; ``""`` when it cannot be read (never raises)."""
        try:
            self._adb("shell", "uiautomator", "dump", UI_DUMP_PATH)
            res = self._adb("shell", "cat", UI_DUMP_PATH)
        except (OSError, subprocess.SubprocessError):
            return ""
        return res.stdout or ""

    def read_state(self) -> str:
        """Classify the current UI. ``unknown`` when the tree is unreadable.

        A tree that could not be read and a tree that says nothing recognisable
        are the same answer here on purpose: both mean "we did not observe it",
        and this rail may never turn that into something optimistic.
        """
        return _classify_ui(self.dump_ui())

    def send(self, dest: str, body: str) -> DriverResult:
        """Drive one send on the device and read the resulting UI state."""
        number = _digits(dest)
        if not number:
            raise UnsupportedDestination("destination_unsupported",
                                         "no digits in destination")
        # 1. open the chat by deep link — no coordinates to guess, no navigation
        self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW",
                  "-d", f"https://wa.me/{number}",
                  timeout=self.config.send_timeout_seconds)
        # 2. type the body, then submit with KEYCODE_ENTER (66)
        self._adb("shell", "input", "text", _escape_input(body),
                  timeout=self.config.send_timeout_seconds)
        self._adb("shell", "input", "keyevent", "66",
                  timeout=self.config.send_timeout_seconds)
        # 3. read the UI back. This scrape is a hint for a human, never proof.
        state = self.read_state()
        return DriverResult(ui_state=state, detail=f"wa.me/{number}")


def _default_runner(argv, timeout):
    """The real ``adb`` invocation. Returns a CompletedProcess; never raises on
    a non-zero exit (the caller decides what an exit code means)."""
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, check=False)


class WhatsAppTransport:
    """Adapter: the official Android client behind the nosms ``Transport`` contract."""

    name = "whatsapp"

    def __init__(self, driver: AdbWhatsAppDriver | None = None, *,
                 config: WhatsAppConfig | None = None, configured: bool | None = None):
        self.config = config or getattr(driver, "config", None) or WhatsAppConfig()
        self._driver = driver
        if configured is not None:
            self._configured = bool(configured)
        else:
            self._configured = bool(self.config.serial)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "WhatsAppTransport":
        """Build the rail from the environment (no device is touched here)."""
        config = WhatsAppConfig.from_env(env)
        return cls(AdbWhatsAppDriver(config), config=config,
                   configured=bool(config.serial))

    @classmethod
    def from_service_config(cls, cfg) -> "WhatsAppTransport":
        """Build from the service ``Config`` (``app/config.py``).

        The ``NOSMS_WHATSAPP_*`` values live in ``Config`` so there is one source
        of truth for them; this translates that into the rail's own config rather
        than reading the environment a second time and letting the two disagree.
        """
        config = WhatsAppConfig(
            adb=cfg.whatsapp_adb, serial=(cfg.whatsapp_serial or "").strip(),
            avd_name=cfg.whatsapp_avd,
            send_timeout_seconds=cfg.whatsapp_send_timeout_seconds,
            ui_timeout_seconds=cfg.whatsapp_ui_timeout_seconds)
        return cls(AdbWhatsAppDriver(config), config=config,
                   configured=bool(config.serial))

    @property
    def capabilities(self) -> Capabilities:
        """What this rail can prove — never aspirational.

        ``available`` is *config-derived* (a device serial is named and a driver
        exists): a capability read must not shell out. ``device_online()`` is the
        explicit probe. ``delivery_receipts=False`` is permanent — a UI scrape is
        not a delivery proof — and ``best_effort=True`` is the conservative
        default for a rail whose only feedback is pixels.
        """
        usable = bool(self._configured and self._driver is not None)
        return Capabilities(available=usable, best_effort=True,
                            delivery_receipts=False, countries=["*"])

    def device_online(self) -> bool:
        """Explicit liveness probe of the emulator (never part of ``capabilities``)."""
        if self._driver is None:
            return False
        return bool(self._driver.device_online())

    def send(self, dest: str, body: str, **kwargs) -> SendResult:
        if not body:
            return self._refused("empty body")
        if not body.isascii():
            # `adb shell input text` cannot type non-ASCII, and a partial send is
            # worse than a refusal: the recipient would get mangled text.
            return self._refused("adb `input text` cannot type a non-ASCII body")
        if self._driver is None or not self._configured:
            return self._refused(
                "whatsapp transport is not configured (set NOSMS_WHATSAPP_SERIAL "
                "to the adb device, e.g. emulator-5554)")

        try:
            result = self._driver.send(dest, body)
        except (UnsupportedDestination, RailUnavailable):
            # The rail-level errors are the caller's to branch on: a wrong
            # destination is a request problem, and a ban/termination must be
            # able to stop the service rather than be flattened to one more
            # "send failed" line.
            raise
        except Exception as exc:                              # noqa: BLE001
            return self._refused(f"device driver failed: {type(exc).__name__}")

        ui = (result.ui_state or "unknown").strip().lower()
        if ui == "banned":
            raise RailUnavailable(
                "terminated",
                "WhatsApp reports this account is banned. The rail stops here: a "
                "retry loop is what turns a ban warning into a permanent ban.")
        if ui == "unregistered":
            raise RailUnavailable(
                "auth_failed",
                "the WhatsApp client is not registered on this emulator; "
                "registration is a human step (E2), not a retry.")

        status = normalize_ui_status(ui)
        detail = f"ui={ui}; {result.detail}".strip("; ")
        if status == "failed":
            return self._refused(detail)
        if status == "unknown":
            # The send action went through, but nothing readable came back. The
            # honest report is "queued" — submitted, not observed: silence is not
            # delivery, and this rail may never claim it.
            status = "queued"
        return SendResult(accepted=True, rail=self.name, best_effort=True,
                          receipt=None, detail=detail, status=status)

    def _refused(self, detail: str) -> SendResult:
        return SendResult(accepted=False, rail=self.name, best_effort=True,
                          receipt=None, detail=detail, status="failed")


#: The vocabulary this rail's statuses must stay inside (import-time guard, so a
#: future edit to UI_STATUS_MAP cannot invent a sixth status the service has no
#: meaning for). Kept as an assertion rather than a test only because a wrong map
#: is a contract bug that must not be able to ship.
assert set(UI_STATUS_MAP.values()) <= set(NORMALIZED_STATUSES), (
    "whatsapp UI_STATUS_MAP must only produce NORMALIZED_STATUSES")

# ``normalize_status`` is re-exported for callers that already import the shared
# normaliser from a rail module (the email rail does the same with its error type).
_ = normalize_status
