"""Executable spec for the WhatsApp rail (ADR-0003) — offline, no emulator.

The rail's only live dependency is a device; the driver takes its ``runner``
(what actually executes ``adb``) as a parameter, so every test here drives the
real argument-construction, UI-classification and status-normalisation code
against a scripted double. No emulator, no adb binary, no network.

The honesty rules this file exists to protect (T2/T3 of ADR-0003):

- ``delivery_receipts`` is **False** and a scraped "delivered" is normalised to
  ``sent``: a UI render is not a delivery proof.
- ``best_effort`` is **True** and every status stays inside NORMALIZED_STATUSES.
- no ``receipt`` is ever returned, so the rail must not claim to be Pollable.
- a ban or an unregistered client **raises** (``RailUnavailable``) — it never
  returns a plain failed send, because a retry loop is what makes a ban permanent.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Config
from app.transports.base import NORMALIZED_STATUSES, Pollable, Transport
from app.transports.errors import RailPaced, RailUnavailable, UnsupportedDestination
from app.transports.whatsapp import (
    UI_STATUS_MAP,
    AdbWhatsAppDriver,
    HaltRecord,
    WhatsAppConfig,
    WhatsAppTransport,
    clear_halt,
    load_halt,
    normalize_ui_status,
    write_halt,
)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class ScriptedAdb:
    """Records every argv and answers like the emulator would. Replaces `adb`."""

    def __init__(self, ui_xml="", state="device", raises=None, raise_on="uiautomator"):
        self.calls: list[tuple[list[str], float]] = []
        self.ui_xml = ui_xml
        self.state = state
        self.raises = raises
        self.raise_on = raise_on

    def __call__(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        args = list(argv)
        if self.raises is not None and self.raise_on in args:
            raise self.raises
        if "uiautomator" in args:
            return _proc()
        if "cat" in args:
            return _proc(stdout=self.ui_xml)
        if "get-state" in args:
            if self.state is None:
                return _proc(stdout="device offline\n", returncode=1)
            return _proc(stdout=self.state + "\n")
        return _proc()

    @property
    def argv(self) -> list[list[str]]:
        return [c[0] for c in self.calls]


class FakeDriver:
    """The driver double: the transport never knows it is not talking to a phone."""

    def __init__(self, ui_state="sent", detail="wa.me/1555", raises=None,
                 online=True):
        self.ui_state = ui_state
        self.detail = detail
        self.raises = raises
        self.online = online
        self.sent: list[tuple[str, str]] = []

    def device_online(self):
        return self.online

    def send(self, dest, body):
        self.sent.append((dest, body))
        if self.raises is not None:
            raise self.raises
        from app.transports.whatsapp import DriverResult
        return DriverResult(ui_state=self.ui_state, detail=self.detail)


def _configured(driver=None, serial="emulator-5554", **kw):
    cfg = WhatsAppConfig(serial=serial, **kw)
    return WhatsAppTransport(driver if driver is not None else FakeDriver(),
                             config=cfg), cfg


# --- the contract ------------------------------------------------------------

def test_rail_satisfies_the_transport_protocol():
    t, _ = _configured()
    assert isinstance(t, Transport)
    assert t.name == "whatsapp"


def test_capabilities_are_honest_about_what_a_ui_scrape_can_prove():
    t, _ = _configured()
    caps = t.capabilities
    assert caps.available is True
    assert caps.best_effort is True              # conservative: pixels are the only feedback
    assert caps.delivery_receipts is False       # permanent; a scrape is not proof
    assert caps.countries == ["*"]               # WhatsApp is global


def test_rail_is_not_pollable_because_it_returns_no_receipt():
    """`delivery_receipts=False` and `receipt=None` mean there is nothing to poll.

    Implementing `status()` anyway would let the transport endpoint call a poll it
    can never get an answer from, so the rail must not look Pollable.
    """
    t, _ = _configured()
    assert not isinstance(t, Pollable)
    assert not hasattr(t, "status")


def test_an_unconfigured_rail_reports_itself_unavailable():
    t = WhatsAppTransport(driver=None, config=WhatsAppConfig(serial=""))
    assert t.capabilities.available is False


def test_a_named_device_without_a_driver_is_not_available():
    t = WhatsAppTransport(driver=None, config=WhatsAppConfig(serial="emulator-5554"))
    assert t.capabilities.available is False


# --- send: the happy path and the honest flags -------------------------------

def test_send_maps_a_sent_ui_state_onto_the_contract():
    driver = FakeDriver(ui_state="sent", detail="wa.me/15550004567")
    t, _ = _configured(driver)
    res = t.send("+1 (555) 000-4567", "hello")
    assert res.accepted is True
    assert res.rail == "whatsapp"
    assert res.best_effort is True
    assert res.receipt is None                   # never a receipt
    assert res.status == "sent"
    assert "sent" in res.detail and "wa.me/15550004567" in res.detail
    assert driver.sent == [("+1 (555) 000-4567", "hello")]


@pytest.mark.parametrize("ui", ["delivered", "read", "two_ticks"])
def test_a_scraped_delivery_is_downgraded_to_sent(ui):
    """THE headline honesty rule: two ticks in a screenshot is not a delivery proof."""
    t, _ = _configured(FakeDriver(ui_state=ui))
    res = t.send("+155****4567", "hello")
    assert res.accepted is True
    assert res.status == "sent"                  # NOT "delivered"
    assert res.receipt is None
    assert ui in res.detail                      # the raw scrape stays for humans


def test_an_unreadable_ui_is_reported_as_queued_not_delivered():
    """The send action went through; nothing readable came back. Silence is not delivery."""
    t, _ = _configured(FakeDriver(ui_state="unknown"))
    res = t.send("+155****4567", "hello")
    assert res.accepted is True
    assert res.status == "queued"


def test_ui_status_map_only_produces_normalized_statuses():
    assert set(UI_STATUS_MAP.values()) <= set(NORMALIZED_STATUSES)
    assert normalize_ui_status("delivered") == "sent"
    assert normalize_ui_status("  READ ") == "sent"
    assert normalize_ui_status("") == "unknown"
    assert normalize_ui_status(None) == "unknown"
    assert normalize_ui_status("something-new-whatsapp-invented") == "unknown"


# --- send: refusals are refundable, and loud where they must be --------------

def test_a_failed_ui_state_is_not_accepted_and_is_refundable():
    t, _ = _configured(FakeDriver(ui_state="failed"))
    res = t.send("+155****4567", "hello")
    assert res.accepted is False
    assert res.status == "failed"
    assert res.refundable is True                # escrow must give the sats back
    assert res.receipt is None


def test_empty_body_is_refused_without_touching_the_device():
    driver = FakeDriver()
    t, _ = _configured(driver)
    res = t.send("+155****4567", "")
    assert res.accepted is False and "empty body" in res.detail
    assert driver.sent == []


def test_a_non_ascii_body_is_refused_rather_than_mangled():
    """`adb shell input text` cannot type non-ASCII; a mangled send is worse than a refusal."""
    driver = FakeDriver()
    t, _ = _configured(driver)
    res = t.send("+155****4567", "grüße 👋")
    assert res.accepted is False and "non-ascii" in res.detail.lower()
    assert driver.sent == []


def test_send_refuses_when_the_device_is_not_configured():
    t = WhatsAppTransport(driver=FakeDriver(), config=WhatsAppConfig(serial=""))
    res = t.send("+155****4567", "hello")
    assert res.accepted is False
    assert "not configured" in res.detail.lower()
    assert "NOSMS_WHATSAPP_SERIAL" in res.detail


def test_a_driver_crash_becomes_a_refundable_refusal():
    t, _ = _configured(FakeDriver(raises=RuntimeError("device offline")))
    res = t.send("+155****4567", "hello")
    assert res.accepted is False and "RuntimeError" in res.detail
    assert res.refundable is True


def test_a_bad_destination_error_propagates_instead_of_being_flattened():
    t, _ = _configured(FakeDriver(raises=UnsupportedDestination("destination_unsupported",
                                                                "no digits")))
    with pytest.raises(UnsupportedDestination) as e:
        t.send("not-a-number", "hello")
    assert e.value.reason == "destination_unsupported"


def test_a_banned_account_raises_and_never_returns_a_send_result():
    """A ban must be branchable and fatal, not one more 'send failed' line."""
    t, _ = _configured(FakeDriver(ui_state="banned"))
    with pytest.raises(RailUnavailable) as e:
        t.send("+155****4567", "hello")
    assert e.value.reason == "terminated"
    assert "retry" in str(e.value).lower()


def test_an_unregistered_client_raises_rather_than_retrying():
    t, _ = _configured(FakeDriver(ui_state="unregistered"))
    with pytest.raises(RailUnavailable) as e:
        t.send("+155****4567", "hello")
    assert e.value.reason == "auth_failed"


def test_rail_unavailable_from_the_driver_also_propagates():
    t, _ = _configured(FakeDriver(raises=RailUnavailable("not_connected", "no device")))
    with pytest.raises(RailUnavailable) as e:
        t.send("+155****4567", "hello")
    assert e.value.reason == "not_connected"


# --- liveness is an explicit probe, never part of `capabilities` -------------

def test_device_online_is_false_without_a_driver():
    assert WhatsAppTransport(driver=None).device_online() is False


def test_device_online_delegates_to_the_driver():
    assert _configured(FakeDriver(online=True))[0].device_online() is True
    assert _configured(FakeDriver(online=False))[0].device_online() is False


def test_capabilities_never_shells_out(monkeypatch):
    """A capability read must stay cheap: `available` is config-derived, not probed."""
    def explode(*_a, **_k):
        raise AssertionError("capabilities must not probe the device")

    t, _ = _configured()
    monkeypatch.setattr(t, "device_online", explode)
    assert t.capabilities.available is True


# --- the driver: argv construction, parsing, and its failure modes -----------

def test_driver_builds_the_deep_link_input_and_enter_argv():
    runner = ScriptedAdb(ui_xml="<node text='Delivered'/>")
    cfg = WhatsAppConfig(serial="emulator-5554", send_timeout_seconds=12.0,
                         ui_timeout_seconds=3.0)
    result = AdbWhatsAppDriver(cfg, runner=runner).send("+1 (555) 000-4567", "hi there")
    assert "--" not in " ".join(runner.argv[0])          # sanity: real argv lists
    assert runner.argv[0][:3] == ["adb", "-s", "emulator-5554"]
    deep = runner.argv[0]
    assert "am" in deep and "android.intent.action.VIEW" in deep
    assert "https://wa.me/15550004567" in deep           # digits only, no '+'
    typed = next(a for a in runner.argv if "input" in a and "text" in a)
    assert "hi%sthere" in typed                          # a space is spelled %s
    assert any("keyevent" in a and "66" in a for a in runner.argv)   # ENTER
    assert runner.calls[0][1] == 12.0                    # the send timeout is used
    assert result.ui_state == "delivered"                # classification, not proof
    assert result.detail == "wa.me/15550004567"


def test_driver_escapes_shell_metacharacters_in_the_body():
    runner = ScriptedAdb(ui_xml="")
    AdbWhatsAppDriver(WhatsAppConfig(serial="s"), runner=runner).send(
        "+15550004567", "a $HOME `x` ; rm -rf &")
    typed = next(a for a in runner.argv if "input" in a and "text" in a)
    body = typed[-1]
    assert "$" not in body.replace("\\$", "")
    for ch in "$`;&":
        assert "\\" + ch in body


def test_driver_refuses_a_destination_with_no_digits():
    runner = ScriptedAdb()
    with pytest.raises(UnsupportedDestination) as e:
        AdbWhatsAppDriver(WhatsAppConfig(serial="s"), runner=runner).send("nope", "hi")
    assert e.value.reason == "destination_unsupported"
    assert runner.calls == []                            # nothing was sent


def test_driver_reports_an_unreadable_ui_as_unknown_not_as_a_lie():
    """An unreadable tree is `unknown`; `dump_ui` keeps the empty-string signal."""
    driver = AdbWhatsAppDriver(WhatsAppConfig(serial="s"),
                               runner=ScriptedAdb(raises=OSError("adb missing")))
    assert driver.dump_ui() == ""
    assert driver.read_state() == "unknown"


def test_driver_send_survives_a_ui_dump_failure_and_says_unknown():
    driver = AdbWhatsAppDriver(WhatsAppConfig(serial="s"),
                               runner=ScriptedAdb(raises=OSError("adb missing")))
    assert driver.send("+15550004567", "hi").ui_state == "unknown"


def test_device_online_requires_the_device_state():
    yes = AdbWhatsAppDriver(WhatsAppConfig(serial="s"), runner=ScriptedAdb(state="device"))
    no = AdbWhatsAppDriver(WhatsAppConfig(serial="s"), runner=ScriptedAdb(state=None))
    assert yes.device_online() is True
    assert no.device_online() is False


def test_device_online_is_false_when_adb_cannot_run():
    driver = AdbWhatsAppDriver(
        WhatsAppConfig(serial="s"),
        runner=ScriptedAdb(raises=OSError("no adb"), raise_on="get-state"))
    assert driver.device_online() is False


def test_device_online_is_false_when_adb_times_out():
    import subprocess

    driver = AdbWhatsAppDriver(
        WhatsAppConfig(serial="s"),
        runner=ScriptedAdb(raises=subprocess.TimeoutExpired("adb", 1),
                           raise_on="get-state"))
    assert driver.device_online() is False


def test_driver_without_a_serial_omits_the_s_flag():
    runner = ScriptedAdb()
    AdbWhatsAppDriver(WhatsAppConfig(serial=""), runner=runner).device_online()
    assert runner.argv[0] == ["adb", "get-state"]


@pytest.mark.parametrize("xml,expected", [
    # a FAILED message contains the word "sent" -- the ordering bug this guards
    ("<node text='Message not sent'/>", "failed"),
    ("<node content-desc='Couldn&apos;t send'/>", "failed"),
    ("<node text='Delivered'/>", "delivered"),
    ("<node text='Read'/>", "delivered"),
    ("<node text='Sent'/>", "sent"),
    ("<node text='Sending'/>", "sent"),
    ("<node text='Verify your phone number'/>", "unregistered"),
    ("<node text='You are banned from using WhatsApp'/>", "banned"),
    ("<node text='com.whatsapp id=action_bar'/>", "unknown"),
    ("", "unknown"),
])
def test_ui_classification_order(xml, expected):
    runner = ScriptedAdb(ui_xml=xml)
    state = AdbWhatsAppDriver(WhatsAppConfig(serial="s"), runner=runner).read_state()
    assert state == expected


def test_a_ban_screen_wins_over_everything_else_on_the_screen():
    xml = "<node text='Message not sent'/><node text='Temporarily banned'/>"
    state = AdbWhatsAppDriver(WhatsAppConfig(serial="s"),
                              runner=ScriptedAdb(ui_xml=xml)).read_state()
    assert state == "banned"


def test_default_runner_delegates_to_subprocess_run(monkeypatch):
    import app.transports.whatsapp as wa

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _proc(stdout="device\n")

    monkeypatch.setattr(wa.subprocess, "run", fake_run)
    res = wa._default_runner(["adb", "get-state"], 5)
    assert seen["argv"] == ["adb", "get-state"]
    assert seen["kw"]["check"] is False and seen["kw"]["timeout"] == 5
    assert res.stdout == "device\n"


# --- configuration: env, and the service's Config ----------------------------

def test_config_from_env_reads_every_knob():
    cfg = WhatsAppConfig.from_env({
        "NOSMS_WHATSAPP_ADB": "/opt/android/platform-tools/adb",
        "NOSMS_WHATSAPP_SERIAL": " emulator-5556 ",
        "NOSMS_WHATSAPP_AVD": "wa-2",
        "NOSMS_WHATSAPP_SEND_TIMEOUT_SECONDS": "45",
        "NOSMS_WHATSAPP_UI_TIMEOUT_SECONDS": "7.5",
    })
    assert cfg.adb == "/opt/android/platform-tools/adb"
    assert cfg.serial == "emulator-5556"          # trimmed
    assert cfg.avd_name == "wa-2"
    assert cfg.send_timeout_seconds == 45.0
    assert cfg.ui_timeout_seconds == 7.5


def test_config_from_env_defaults_and_tolerates_garbage():
    cfg = WhatsAppConfig.from_env({})
    assert (cfg.adb, cfg.serial, cfg.avd_name) == ("adb", "", "wa-dev")
    assert cfg.send_timeout_seconds == 90.0 and cfg.ui_timeout_seconds == 30.0
    # a malformed timeout must fall back to the default, never to a shorter one
    bad = WhatsAppConfig.from_env({"NOSMS_WHATSAPP_SEND_TIMEOUT_SECONDS": "soon",
                                   "NOSMS_WHATSAPP_UI_TIMEOUT_SECONDS": "  "})
    assert bad.send_timeout_seconds == 90.0
    assert bad.ui_timeout_seconds == 30.0


def test_from_env_builds_an_unconfigured_rail_without_touching_a_device():
    t = WhatsAppTransport.from_env({})
    assert isinstance(t, WhatsAppTransport)
    assert t.capabilities.available is False
    assert t.config.avd_name == "wa-dev"


def test_from_env_builds_a_configured_rail_from_the_serial():
    t = WhatsAppTransport.from_env({"NOSMS_WHATSAPP_SERIAL": "emulator-5554"})
    assert t.capabilities.available is True


def test_service_config_carries_the_whatsapp_env_vars():
    cfg = Config.from_env({"NOSMS_WHATSAPP_SERIAL": " emulator-5558 ",
                           "NOSMS_WHATSAPP_ADB": "/x/adb",
                           "NOSMS_WHATSAPP_AVD": "wa-x",
                           "NOSMS_WHATSAPP_SEND_TIMEOUT_SECONDS": "20",
                           "NOSMS_WHATSAPP_UI_TIMEOUT_SECONDS": "nope"})
    assert cfg.whatsapp_serial == "emulator-5558"
    assert cfg.whatsapp_adb == "/x/adb"
    assert cfg.whatsapp_avd == "wa-x"
    assert cfg.whatsapp_send_timeout_seconds == 20.0
    assert cfg.whatsapp_ui_timeout_seconds == 30.0     # garbage -> default
    assert Config.from_env({}).whatsapp_serial == ""   # no device by default


def test_rail_built_from_the_service_config_uses_those_values():
    cfg = Config.from_env({"NOSMS_WHATSAPP_SERIAL": "emulator-5554",
                           "NOSMS_WHATSAPP_ADB": "/x/adb",
                           "NOSMS_WHATSAPP_AVD": "wa-x"})
    t = WhatsAppTransport.from_service_config(cfg)
    assert t.config.adb == "/x/adb" and t.config.avd_name == "wa-x"
    assert t.capabilities.available is True

    bare = WhatsAppTransport.from_service_config(Config.from_env({}))
    assert bare.capabilities.available is False


# --- registry wiring ---------------------------------------------------------

def test_registry_builds_the_whatsapp_rail():
    from app.transports import build_transport
    for name in ("whatsapp", "wa"):
        t = build_transport(name)
        assert isinstance(t, WhatsAppTransport) and t.name == "whatsapp"


def test_registry_wiring_is_offline_and_reports_the_env_device():
    from app.transports import build_transport
    t = build_transport(env={"NOSMS_TRANSPORT": "whatsapp",
                             "NOSMS_WHATSAPP_SERIAL": "emulator-5554"})
    assert t.name == "whatsapp"
    assert t.capabilities.available is True
    assert t.capabilities.delivery_receipts is False


def test_registry_still_refuses_an_unknown_rail():
    from app.transports import build_transport
    with pytest.raises(ValueError):
        build_transport("carrier-pigeon")


def test_registry_offers_the_whatsapp_rail_with_the_email_degrade_path():
    """The same composition `jmp_cheogram` uses, for the ADR-0003 rail."""
    from app.transports import build_transport
    from app.transports.failover import FailoverTransport
    t = build_transport("whatsapp_email",
                        env={"NOSMS_WHATSAPP_SERIAL": "emulator-5554"})
    assert isinstance(t, FailoverTransport)
    assert isinstance(t.primary, WhatsAppTransport)
    assert t.primary.pacer is not None          # paced, like the plain rail
    assert t.fallback.name == "email_gateway"
    assert t.name == "failover:whatsapp->email_gateway"


def test_http_service_build_transport_selects_the_whatsapp_rail():
    from app.main import build_transport as http_build
    cfg = Config.from_env({"NOSMS_TRANSPORT": "whatsapp",
                           "NOSMS_WHATSAPP_SERIAL": "emulator-5554"})
    t = http_build(cfg)
    assert t.name == "whatsapp"
    assert t.capabilities.delivery_receipts is False


def test_the_http_services_rail_is_still_the_fake_by_default():
    from app.main import build_transport as http_build
    assert http_build(Config.from_env({})).name == "fake"


# --- T4: the pacing hook (ADR-0003 control #2) -------------------------------
#
# The rail rides the operator's PERSONAL line, so volume is the abuse surface. A
# paced call DEFERS — it is not a failure, and it must not consume a slot that a
# later caller could have used.

def _paced(driver=None, **policy_kw):
    from app.transports.pacing import Pacer, PacingPolicy
    policy = PacingPolicy(**policy_kw)
    pacer = Pacer(policy)
    rail = WhatsAppTransport(driver if driver is not None else FakeDriver(),
                             config=WhatsAppConfig(serial="emulator-5554"),
                             pacer=pacer)
    return rail, pacer


def test_a_paced_call_defers_and_never_touches_the_device():
    rail, pacer = _paced(daily_cap=5, min_gap_seconds=60, max_gap_seconds=60)
    pacer.record_send()                      # the gap is now armed
    driver = rail._driver
    with pytest.raises(RailPaced) as excinfo:
        rail.send("+155****4567", "hello")
    assert excinfo.value.reason == "min_gap"
    assert excinfo.value.retry_after > 0
    assert driver.sent == []                 # never attempted
    assert pacer.snapshot()["state"]["count"] == 1   # and no slot was eaten


def test_a_paced_call_at_the_daily_cap_defers_with_a_retry_after():
    rail, pacer = _paced(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0)
    pacer.record_send()
    with pytest.raises(RailPaced) as excinfo:
        rail.send("+155****4567", "hello")
    assert excinfo.value.reason == "daily_cap_reached"
    assert excinfo.value.retry_after > 0
    assert rail._driver.sent == []


def test_an_accepted_send_reserves_and_confirms_exactly_one_slot():
    rail, pacer = _paced(daily_cap=2, min_gap_seconds=0, max_gap_seconds=0)
    assert rail.send("+155****4567", "one").accepted is True
    state = pacer.snapshot()["state"]
    assert state["count"] == 1
    assert state["pending"] is False


def test_a_refused_send_gives_the_slot_back():
    """The message never left the client, so it must not consume the allowance."""
    rail, pacer = _paced(FakeDriver(ui_state="failed"),
                         daily_cap=1, min_gap_seconds=0, max_gap_seconds=0)
    assert rail.send("+155****4567", "one").accepted is False
    assert pacer.snapshot()["state"]["count"] == 0


def test_a_device_crash_gives_the_slot_back():
    rail, pacer = _paced(FakeDriver(raises=RuntimeError("device offline")),
                         daily_cap=1, min_gap_seconds=0, max_gap_seconds=0)
    assert rail.send("+155****4567", "one").accepted is False
    assert pacer.snapshot()["state"]["count"] == 0


def test_the_counted_send_is_the_one_that_was_confirmed():
    rail, pacer = _paced(daily_cap=1, min_gap_seconds=0, max_gap_seconds=0)
    assert rail.send("+155****4567", "one").accepted is True
    with pytest.raises(RailPaced):           # the cap is now real
        rail.send("+155****4567", "two")
    assert rail._driver.sent == [("+155****4567", "one")]


def test_from_env_builds_the_rail_with_its_own_pacing_knobs(tmp_path):
    t = WhatsAppTransport.from_env({
        "NOSMS_WHATSAPP_SERIAL": "emulator-5554",
        "NOSMS_WHATSAPP_DAILY_CAP": "3",
        "NOSMS_WHATSAPP_MIN_GAP_SECONDS": "5",
        "NOSMS_WHATSAPP_MAX_GAP_SECONDS": "7",
        "NOSMS_WHATSAPP_PACING_STATE": str(tmp_path / "wa_pacing.json"),
    }, halt_path=None)
    assert t.pacer is not None
    assert (t.pacer.policy.daily_cap, t.pacer.policy.min_gap_seconds,
            t.pacer.policy.max_gap_seconds) == (3, 5.0, 7.0)


def test_the_registry_injects_a_pacer_into_the_whatsapp_rail():
    from app.transports import build_transport
    t = build_transport("whatsapp")
    assert isinstance(t, WhatsAppTransport)
    assert t.pacer is not None               # the personal line is protected


# --- T4: ban handling — fail loudly, stop, alert, never retry ----------------

def test_a_ban_latches_the_rail_off_so_it_reports_itself_unavailable():
    t, _ = _configured(FakeDriver(ui_state="banned"))
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "hello")
    assert t.down_reason == "terminated"
    assert t.capabilities.available is False
    flags = t.capability_flags()
    assert flags["halted"] is True and flags["down_reason"] == "terminated"


def test_a_latched_rail_never_touches_the_device_again():
    """The whole point: no code path can turn a ban into a retry loop."""
    driver = FakeDriver(ui_state="banned")
    t, _ = _configured(driver)
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "one")
    assert len(driver.sent) == 1
    for _ in range(3):
        with pytest.raises(RailUnavailable) as excinfo:
            t.send("+155****4567", "again")
        assert excinfo.value.reason == "terminated"
    assert len(driver.sent) == 1             # exactly one attempt, ever


def test_a_transient_driver_failure_does_not_latch_the_rail():
    t, _ = _configured(FakeDriver(raises=RailUnavailable("not_connected", "no device")))
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "hello")
    assert t.down_reason is None             # a reconnect window is not a ban
    assert t.capabilities.available is True


def test_the_ban_alert_is_loud_and_calls_the_injected_notifier(caplog):
    seen = []
    t = WhatsAppTransport(FakeDriver(ui_state="banned"),
                          config=WhatsAppConfig(serial="emulator-5554"),
                          alert=lambda reason, detail: seen.append((reason, detail)))
    with caplog.at_level("CRITICAL"):
        with pytest.raises(RailUnavailable):
            t.send("+155****4567", "hello")
    assert seen and seen[0][0] == "terminated" and seen[0][1]
    assert any("STOPPED" in r.message for r in caplog.records)
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


def test_an_alert_hook_that_raises_does_not_hide_the_ban():
    def explode(reason, detail):
        raise RuntimeError("notifier down")

    t = WhatsAppTransport(FakeDriver(ui_state="banned"),
                          config=WhatsAppConfig(serial="emulator-5554"),
                          alert=explode)
    with pytest.raises(RailUnavailable) as excinfo:
        t.send("+155****4567", "hello")
    assert excinfo.value.reason == "terminated"


def test_a_detected_rate_limit_fails_loudly_and_is_not_retried():
    driver = FakeDriver(ui_state="rate_limited")
    t, _ = _configured(driver)
    with pytest.raises(RailUnavailable) as excinfo:
        t.send("+155****4567", "hello")
    assert excinfo.value.reason == "rate_limited"
    assert len(driver.sent) == 1             # the send is not re-attempted
    # Throttling is a warning, not a death: the rail stays usable for later.
    assert t.down_reason is None


def test_a_rate_limit_never_becomes_a_failed_send_result():
    """A throttled line must not be flattened into one more reversible failure."""
    t, _ = _configured(FakeDriver(ui_state="rate_limited"))
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "hello")
    assert t.down_reason is None


# --- T4: the halts that must survive a restart --------------------------------

def test_the_halt_is_persisted_so_a_restart_is_not_a_retry(tmp_path):
    halt = tmp_path / "whatsapp_halt.json"
    driver = FakeDriver(ui_state="banned")
    first = WhatsAppTransport(driver, config=WhatsAppConfig(serial="emulator-5554"),
                              halt_path=str(halt))
    with pytest.raises(RailUnavailable):
        first.send("+155****4567", "hello")
    assert halt.is_file()

    # a new process (a new rail) reading the same file stays stopped
    driver2 = FakeDriver()
    second = WhatsAppTransport(driver2, config=WhatsAppConfig(serial="emulator-5554"),
                               halt_path=str(halt))
    assert second.down_reason == "terminated"
    assert second.capabilities.available is False
    with pytest.raises(RailUnavailable) as excinfo:
        second.send("+155****4567", "hello")
    assert excinfo.value.reason == "terminated"
    assert driver2.sent == []                # the emulator is never touched


def test_an_unregistration_is_persisted_too(tmp_path):
    halt = tmp_path / "halt.json"
    t = WhatsAppTransport(FakeDriver(ui_state="unregistered"),
                          config=WhatsAppConfig(serial="emulator-5554"),
                          halt_path=str(halt))
    with pytest.raises(RailUnavailable) as excinfo:
        t.send("+155****4567", "hello")
    assert excinfo.value.reason == "auth_failed"
    assert load_halt(str(halt)) is not None
    assert load_halt(str(halt)).reason == "auth_failed"


def test_an_unreadable_halt_file_fails_closed(tmp_path):
    halt = tmp_path / "halt.json"
    halt.write_text("{ not json")
    t = WhatsAppTransport(FakeDriver(), config=WhatsAppConfig(serial="emulator-5554"),
                          halt_path=str(halt))
    assert t.down_reason == "halt_state_corrupt"
    assert t.capabilities.available is False
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "hello")
    assert t._driver.sent == []
    assert halt.read_text() == "{ not json"   # left in place for a human


def test_a_halt_file_with_no_reason_fails_closed(tmp_path):
    halt = tmp_path / "halt.json"
    halt.write_text("{}")
    t = WhatsAppTransport(FakeDriver(), config=WhatsAppConfig(serial="emulator-5554"),
                          halt_path=str(halt))
    assert t.down_reason == "halt_state_corrupt"


def test_a_halt_that_cannot_be_persisted_still_stops_the_rail(tmp_path, caplog):
    """Losing the file must not lose the stop: the latch and alert still happen."""
    ro = tmp_path / "read-only"
    ro.mkdir()
    ro.chmod(0o500)                          # can be listed, cannot be written
    driver = FakeDriver(ui_state="banned")
    t = WhatsAppTransport(driver, config=WhatsAppConfig(serial="emulator-5554"),
                          halt_path=str(ro / "halt.json"))
    assert t.down_reason is None             # nothing to read yet
    with caplog.at_level("CRITICAL"):
        with pytest.raises(RailUnavailable):
            t.send("+155****4567", "hello")
    assert t.down_reason == "terminated"
    assert t.capabilities.available is False
    assert any("could NOT persist" in r.message for r in caplog.records)


def test_clear_halt_is_the_operator_way_back(tmp_path):
    halt = tmp_path / "halt.json"
    t = WhatsAppTransport(FakeDriver(ui_state="banned"),
                          config=WhatsAppConfig(serial="emulator-5554"),
                          halt_path=str(halt))
    with pytest.raises(RailUnavailable):
        t.send("+155****4567", "hello")
    assert clear_halt(str(halt)) is True
    assert clear_halt(str(halt)) is False     # idempotent for an operator

    fresh = WhatsAppTransport(FakeDriver(), config=WhatsAppConfig(serial="emulator-5554"),
                              halt_path=str(halt))
    assert fresh.down_reason is None
    assert fresh.send("+155****4567", "hello").accepted is True


def test_load_halt_on_a_missing_or_disabled_path_is_none(tmp_path):
    assert load_halt(None) is None
    assert load_halt(str(tmp_path / "nope.json")) is None
    assert write_halt(None, HaltRecord(reason="terminated")) is False


def test_cvm_runner_builds_the_whatsapp_rail_and_never_falls_back_to_the_double():
    runner = pytest.importorskip("scripts.run_cvm_server")
    assert runner.build_transport("whatsapp").name == "whatsapp"
    assert runner.build_transport("wa").name == "whatsapp"
