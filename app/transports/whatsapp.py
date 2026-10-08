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
* **A ban fails loudly and stops the rail.** A detected ban or an unregistered
  client raises :class:`RailUnavailable` with a machine token rather than
  returning a failed :class:`SendResult`, so the caller can stop and alert. It
  also **latches** the rail off (``mark_down``) and **persists** the halt
  (:data:`DEFAULT_HALT_STATE`), because a restart that re-touches a banned device
  is exactly the retry loop that turns a warning into a permanent ban. Once
  latched, every send raises immediately without touching the emulator. Never
  retry a ban.
* **Volume is paced.** The rail takes a :class:`~app.transports.pacing.Pacer`
  (own knobs, own counter: ADR-0003 control #2) and *reserves* a slot before the
  blocking send. A paced call raises :class:`RailPaced` — a deferral the caller
  answers with ``429`` + ``Retry-After`` — never a failed send.

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
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass

from .base import (Capabilities, NORMALIZED_STATUSES, SendResult, normalize_status)
from .errors import RailPaced, RailUnavailable, UnsupportedDestination
from .pacing import Pacer, load_whatsapp_pacing_policy

__all__ = [
    "AdbWhatsAppDriver", "DriverResult", "HaltRecord", "UI_STATUS_MAP",
    "WhatsAppConfig", "WhatsAppTransport", "normalize_ui_status",
    "load_halt", "write_halt", "clear_halt",
]

logger = logging.getLogger(__name__)

#: The emulator host facts live in ``tools/emulator/`` (AVD ``wa-dev`` on dq05).
DEFAULT_ADB = "adb"
DEFAULT_AVD_NAME = "wa-dev"
DEFAULT_SEND_TIMEOUT_S = 90.0
DEFAULT_UI_TIMEOUT_S = 30.0

#: Persisted pacing counter for *this* rail. Never the JMP counter: two personal
#: lines, two abuse surfaces (see :mod:`app.transports.pacing`).
DEFAULT_PACING_STATE = "~/.hermes/profiles/manager/state/whatsapp_pacing.json"

#: The persisted kill-switch. Written when the rail is halted by a detected ban
#: or an unregistered client; read at construction, so a restart does not
#: re-touch a device that is already banned.
DEFAULT_HALT_STATE = "~/.hermes/profiles/manager/state/whatsapp_halt.json"

#: ``RailUnavailable`` reasons that mean "this rail is gone, do not try again".
#: Only these latch and persist. A ``not_connected``/``connection_lost`` window is
#: transient by contract (see ``failover.py``) and must not halt the rail.
TERMINAL_REASONS = ("terminated", "auth_failed")

#: Reason recorded when the halt file exists but cannot be read. Fail **closed**:
#: an unreadable kill-switch must not hand the device back to the service.
HALT_STATE_UNREADABLE = "halt_state_corrupt"

#: Where the scraped UI tree is written on the *guest*.
UI_DUMP_PATH = "/sdcard/nosms-whatsapp-ui.xml"

#: UI states this rail can read out of the client, mapped onto the service's
#: five-value vocabulary. Every ``delivered``/read signal is downgraded to
#: ``sent``: a scrape is not a delivery proof. ``banned``/``unregistered``/
#: ``rate_limited`` are ``failed`` here **and** handled explicitly by
#: :meth:`WhatsAppTransport.send`, which raises instead of returning.
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
    "rate_limited": "failed",
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
class HaltRecord:
    """The persisted kill-switch: why the rail stopped, and since when."""

    reason: str
    detail: str = ""
    at: float = 0.0


def load_halt(path: str | None) -> HaltRecord | None:
    """Read the kill-switch. Missing -> ``None``; unreadable -> fail **closed**.

    A missing file is the normal "not halted" state. A *present* file that cannot
    be parsed is not: the safe direction on a banned account is "stay stopped
    until a human looks", so it becomes a halt with
    :data:`HALT_STATE_UNREADABLE` rather than a silent return to normal service.
    The file is left untouched so the failure stays visible and auditable.
    """
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return None
    try:
        with open(expanded) as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("halt state is not a JSON object")
        reason = str(data.get("reason") or "").strip()
        if not reason:
            raise ValueError("halt state names no reason")
        return HaltRecord(reason=reason, detail=str(data.get("detail") or ""),
                          at=float(data.get("at") or 0.0))
    except (OSError, ValueError, TypeError):
        logger.critical(
            "nosms whatsapp: halt state at %s is unreadable; failing closed "
            "(the rail stays stopped until an operator clears it)", expanded)
        return HaltRecord(reason=HALT_STATE_UNREADABLE,
                          detail=f"unreadable halt state at {expanded}")


def write_halt(path: str | None, record: HaltRecord) -> bool:
    """Persist the halt atomically. Returns True when it landed on disk.

    Never raises: losing the file must not lose the halt. The rail latches in
    memory and alerts either way; a write failure is reported as CRITICAL so the
    operator knows the stop will not survive a restart.
    """
    if not path:
        return False
    expanded = os.path.expanduser(path)
    try:
        os.makedirs(os.path.dirname(expanded) or ".", exist_ok=True)
        tmp = f"{expanded}.tmp"
        with open(tmp, "w") as handle:
            json.dump({"reason": record.reason, "detail": record.detail,
                       "at": record.at or time.time()}, handle, indent=2)
        os.replace(tmp, expanded)
        return True
    except OSError as exc:                                   # noqa: BLE001
        logger.critical("nosms whatsapp: could NOT persist the halt to %s (%s); "
                        "the rail is stopped in memory only", expanded, exc)
        return False


def clear_halt(path: str | None) -> bool:
    """Operator action: clear the kill-switch. Returns True when removed."""
    if not path:
        return False
    expanded = os.path.expanduser(path)
    try:
        os.remove(expanded)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:                                   # noqa: BLE001
        logger.warning("nosms whatsapp: could not clear halt %s (%s)",
                       expanded, exc)
        return False


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
                 config: WhatsAppConfig | None = None, configured: bool | None = None,
                 pacer: Pacer | None = None, alert=None,
                 halt_path: str | None = None):
        self.config = config or getattr(driver, "config", None) or WhatsAppConfig()
        self._driver = driver
        #: ADR-0003 control #2. ``None`` means unpaced (a rail built directly is
        #: the caller's own business; the service factories always inject one).
        self.pacer = pacer
        #: Optional ``(reason, detail) -> None`` notifier. The CRITICAL log is
        #: always emitted; this is how a deployer wires a real alert without the
        #: rail knowing what one is.
        self._alert_hook = alert
        self._halt_path = halt_path
        if configured is not None:
            self._configured = bool(configured)
        else:
            self._configured = bool(self.config.serial)
        self._down_reason: str | None = None
        self._halt_detail = ""
        persisted = load_halt(self._halt_path)
        if persisted is not None:
            # A previous run halted this rail. Honour it at construction, so the
            # restart cannot become the retry the ADR forbids.
            self._down_reason = persisted.reason
            self._halt_detail = persisted.detail
            logger.warning("nosms whatsapp: rail is halted by persisted state "
                           "(%s); clear %s to resume", persisted.reason,
                           self._halt_path)

    @classmethod
    def from_env(cls, env: dict | None = None, *, pacer: Pacer | None = None,
                 halt_path: str | None = None) -> "WhatsAppTransport":
        """Build the rail from the environment (no device is touched here).

        Builds the pacing counter too, from ``NOSMS_WHATSAPP_*`` — the same shape
        ``JmpCheogramTransport.from_env`` uses, and its own counter (never the
        JMP one). ``halt_path`` is left to the caller: the service factories
        resolve it from ``NOSMS_WHATSAPP_HALT_STATE``.
        """
        e = os.environ if env is None else env
        config = WhatsAppConfig.from_env(e)
        if pacer is None:
            pacer = Pacer(load_whatsapp_pacing_policy(e),
                          state_path=e.get("NOSMS_WHATSAPP_PACING_STATE",
                                           DEFAULT_PACING_STATE))
        return cls(AdbWhatsAppDriver(config), config=config,
                   configured=bool(config.serial), pacer=pacer,
                   halt_path=halt_path)

    @classmethod
    def from_service_config(cls, cfg, *, pacer: Pacer | None = None,
                            halt_path: str | None = None) -> "WhatsAppTransport":
        """Build from the service ``Config`` (``app/config.py``).

        The ``NOSMS_WHATSAPP_*`` device values live in ``Config`` so there is one
        source of truth for them; this translates that into the rail's own config
        rather than reading the environment a second time and letting the two
        disagree. The *pacing* knobs are read by the rail factory, exactly as the
        JMP rail does it (``Config`` carries no JMP pacing knobs either).
        """
        e = os.environ
        config = WhatsAppConfig(
            adb=cfg.whatsapp_adb, serial=(cfg.whatsapp_serial or "").strip(),
            avd_name=cfg.whatsapp_avd,
            send_timeout_seconds=cfg.whatsapp_send_timeout_seconds,
            ui_timeout_seconds=cfg.whatsapp_ui_timeout_seconds)
        if pacer is None:
            pacer = Pacer(load_whatsapp_pacing_policy(e),
                          state_path=e.get("NOSMS_WHATSAPP_PACING_STATE",
                                           DEFAULT_PACING_STATE))
        return cls(AdbWhatsAppDriver(config), config=config,
                   configured=bool(config.serial), pacer=pacer,
                   halt_path=halt_path)

    @property
    def capabilities(self) -> Capabilities:
        """What this rail can prove — never aspirational.

        ``available`` is *config-derived* (a device serial is named and a driver
        exists), and it also follows :attr:`down_reason`: a rail halted by a
        detected ban reports itself unavailable, exactly as the JMP rail reports
        a dead line. A capability read must not shell out; ``device_online()`` is
        the explicit probe. ``delivery_receipts=False`` is permanent — a UI
        scrape is not a delivery proof — and ``best_effort=True`` is the
        conservative default for a rail whose only feedback is pixels.
        """
        usable = bool(self._configured and self._driver is not None
                      and self._down_reason is None)
        return Capabilities(available=usable, best_effort=True,
                            delivery_receipts=False, countries=["*"])

    @property
    def down_reason(self) -> str | None:
        """Terminal token when the rail has latched itself off, else ``None``.

        The same name the JMP rail uses, so the degrade path
        (:class:`~app.transports.failover.FailoverTransport`) can read both
        rails the same way.
        """
        return self._down_reason

    def capability_flags(self) -> dict:
        """Serialisable form for ``/api/health`` and ``sms.capabilities``."""
        caps = self.capabilities
        return {
            "rail": self.name,
            "available": caps.available,
            "best_effort": caps.best_effort,
            "delivery_receipts": caps.delivery_receipts,
            "countries": list(caps.countries),
            "down_reason": self._down_reason,
            "paced": self.pacer is not None,
            "halted": self._down_reason is not None,
        }

    def mark_down(self, reason: str, detail: str = "") -> None:
        """Latch the rail off (ban, unregistration, operator kill-switch).

        In-memory only: :meth:`halt` is the detecting path, which also persists
        and alerts. Exposed separately so an operator/probe can stop the rail
        without claiming a ban.
        """
        self._down_reason = reason
        self._halt_detail = detail

    def halt(self, reason: str, detail: str = "") -> RailUnavailable:
        """Stop the rail for good and hand back the error to raise.

        Three things happen, in this order, and all three matter: the rail
        latches off (so no further send touches the device), the halt is
        **persisted** (so a restart is not a retry), and the alert is emitted
        (CRITICAL log + any injected notifier). The caller raises the returned
        error — the rail never silently swallows a ban.
        """
        self.mark_down(reason, detail)
        write_halt(self._halt_path, HaltRecord(reason=reason, detail=detail))
        self._alert(reason, detail)
        return RailUnavailable(reason, detail)

    def _alert(self, reason: str, detail: str) -> None:
        """Fail loudly: a CRITICAL line, plus the hook if one was injected."""
        logger.critical(
            "nosms whatsapp rail STOPPED (%s): %s — no retry will be attempted; "
            "a human must clear %s to resume", reason, detail,
            self._halt_path or "(in-memory only; nothing persisted)")
        if self._alert_hook is not None:
            try:
                self._alert_hook(reason, detail)
            except Exception:                                # noqa: BLE001
                logger.exception("nosms whatsapp: alert hook failed")

    def device_online(self) -> bool:
        """Explicit liveness probe of the emulator (never part of ``capabilities``)."""
        if self._driver is None:
            return False
        return bool(self._driver.device_online())

    def _release(self, claim, *, accepted: bool) -> None:
        if self.pacer is not None and claim is not None:
            self.pacer.release(claim, accepted=accepted)

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
        if self._down_reason is not None:
            # Stopped on purpose. Raising — not one more "send failed" line — is
            # the point: the caller must be able to stop and alert. No device is
            # touched, so a stopped rail cannot become a retry loop.
            raise RailUnavailable(
                self._down_reason,
                self._halt_detail or
                "the rail is halted; a human must clear the halt state to resume.")

        claim = None
        if self.pacer is not None:
            # Reserve the slot BEFORE the blocking send: check-then-act would let
            # two concurrent callers both ride the last allowance. A refusal here
            # is a deferral, never a failed send (the message was not attempted).
            claim = self.pacer.claim()
            if not claim.allowed:
                raise RailPaced(claim.reason, claim.retry_after_seconds,
                                detail=f"personal-line pacing: {claim.reason}")

        try:
            result = self._driver.send(dest, body)
        except UnsupportedDestination:
            # A request problem, decided before the message left: never halting.
            self._release(claim, accepted=False)
            raise
        except RailUnavailable as exc:
            self._release(claim, accepted=False)
            if exc.reason in TERMINAL_REASONS:
                # The device is gone (banned / unregistered): stop, persist, alert.
                raise self.halt(exc.reason, exc.detail)
            raise
        except Exception as exc:                              # noqa: BLE001
            self._release(claim, accepted=False)
            return self._refused(f"device driver failed: {type(exc).__name__}")

        ui = (result.ui_state or "unknown").strip().lower()
        if ui == "banned":
            self._release(claim, accepted=False)
            raise self.halt(
                "terminated",
                "WhatsApp reports this account is banned. The rail stops here: a "
                "retry loop is what turns a ban warning into a permanent ban.")
        if ui == "unregistered":
            self._release(claim, accepted=False)
            raise self.halt(
                "auth_failed",
                "the WhatsApp client is not registered on this emulator; "
                "registration is a human step (E2), not a retry.")
        if ui == "rate_limited":
            # Detected throttling is a warning, not a ban: fail loudly and let the
            # caller defer, but do not latch — the pacing counter is what keeps
            # this from happening again, and a halt would be an over-reaction.
            self._release(claim, accepted=False)
            self._alert("rate_limited",
                        "WhatsApp is throttling this line; the send was NOT "
                        "retried. Reduce volume or wait.")
            raise RailUnavailable(
                "rate_limited",
                "WhatsApp is throttling this line. The rail does not retry a "
                "rate limit — that is what turns throttling into a ban.")

        status = normalize_ui_status(ui)
        detail = f"ui={ui}; {result.detail}".strip("; ")
        if status == "failed":
            self._release(claim, accepted=False)
            return self._refused(detail)
        if status == "unknown":
            # The send action went through, but nothing readable came back. The
            # honest report is "queued" — submitted, not observed: silence is not
            # delivery, and this rail may never claim it.
            status = "queued"
        self._release(claim, accepted=True)
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
