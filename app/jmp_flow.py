"""XEP-0050 ad-hoc-command driver — the JMP/Cheogram funding flow, headlessly.

Port of the proven tool from the sibling archive
------------------------------------------------
Ported, not rewritten, from `cashu-sms/tools/xmpp_provision.py` (branch
`worker-base/t_3910f6fc`, commit `3a179dc` of felixfelix-bot/soveng-archive),
where a live run drove the JMP registration to the funding step and captured the
bitcoin deposit facts. That file carries 37 offline tests; the same 37 (plus
JMP-specific ones) run against this port in `tests/test_jmp_flow.py`, so the
"port" is provable rather than asserted.

What was changed in the port: the CLI is gone (this is a library used by the CVM
tool surface), the JMP flow is a module constant instead of a JSON file, and the
no-payment guarantee became an executable check (`assert_no_payment_emitted`)
instead of a note in a script.

Why a bot driver at all
-----------------------
JMP/Cheogram has **no web signup**: `jmp.chat/signup|/plans|/billing` are 404.
Registration is an XEP-0050 ad-hoc command (node ``jabber:iq:register``) executed
against the ``cheogram.com`` bot, after which the conversation continues as chat
messages. Playwright is the wrong tool; this is the right one.

Safety (the part that matters most)
----------------------------------
* **Nothing here can pay.** The flow selects the *bitcoin* activation method —
  which is what makes the bot print the deposit address — and stops. No card
  method is ever selected, no card form is ever submitted, and no out-of-band
  payment URL is ever opened (the bot prints such a URL; this driver records it
  as text and never fetches it).
* `assert_no_payment_emitted()` is called by every funding drive and raises if a
  payment-shaped action appears, so the guarantee is checked, not promised.
* Credentials are never printed: `Transcript` redacts any registered secret
  value from both the summary and the raw XML, and the driver only ever receives
  a password through an injected callable.

Requires `slixmpp` for the live path only; it is imported lazily so the offline
tests (and importing this module) work without it installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NS_COMMANDS = "http://jabber.org/protocol/commands"
NS_DISCO_ITEMS = "http://jabber.org/protocol/disco#items"
NS_DISCO_INFO = "http://jabber.org/protocol/disco#info"
NS_XDATA = "jabber:x:data"
NS_CLIENT = "jabber:client"
NS_STREAMS = "http://etherx.jabber.org/streams"
NS_OOB = "jabber:x:oob"
NS_REGISTER = "jabber:iq:register"
ACTIONS_FIELD = "http://jabber.org/protocol/commands#actions"

DEFAULT_STEP_TIMEOUT = 60.0

#: the JMP front door: registration is an ad-hoc command on this bot.
JMP_BOT = "cheogram.com"
JMP_REGISTER_NODE = "jabber:iq:register"


def now_iso() -> str:
    """UTC timestamp, second precision, Z-suffixed (the transcript's clock)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------
class Transcript:
    """Append-only verbatim log of everything sent and received.

    Every record is a summary line plus the raw stanza XML, so the bot's exact
    words are preserved. ``redact`` holds *values* that must never be written
    (passwords): each is replaced everywhere, in the summary and in the raw XML.
    """

    def __init__(self, path=None, echo=True, redact=()):
        self.path = path
        self.echo = echo
        self.redact = [r for r in (redact or ()) if r]
        self.records = []
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("\n##### transcript opened %s\n" % now_iso())

    def add(self, direction, summary, raw=None):
        """direction: OUT | IN | NOTE."""
        record = {
            "ts": now_iso(),
            "dir": direction,
            "summary": self._redact(summary),
            "raw": self._redact(raw) if raw else None,
        }
        self.records.append(record)
        self._write("===== %s  %-4s %s" % (record["ts"], direction, record["summary"]))
        if record["raw"]:
            for line in str(record["raw"]).splitlines():
                self._write("      | " + line)
        return record

    def note(self, text):
        return self.add("NOTE", text)

    def texts(self):
        """Just the IN summaries — what the bot actually said."""
        return [r["summary"] for r in self.records if r["dir"] == "IN"]

    def outbound(self):
        """Just the OUT summaries — what we actually sent."""
        return [r["summary"] for r in self.records if r["dir"] == "OUT"]

    def _redact(self, raw):
        out = str(raw)
        for secret in self.redact:
            out = out.replace(secret, "<redacted:%d chars>" % len(secret))
        return out

    def _write(self, line):
        if self.echo:
            print(line, flush=True)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


# --------------------------------------------------------------------------
# expectation engine (pure — offline-testable)
# --------------------------------------------------------------------------
class ExpectTimeout(RuntimeError):
    """Raised when an expected message never arrived in time."""


class Cursor:
    """Ordering-enforcing matcher over a stream of received messages.

    Messages are appended with :meth:`feed`; matching walks forward from the
    current position, so an expectation cannot be satisfied by a message that
    arrived *before* the previous expectation was met. A timeout raises
    :class:`ExpectTimeout` carrying what was pending, which is what makes a
    stalled bot flow debuggable.
    """

    def __init__(self, system=None):
        self.messages = []
        self.pos = 0
        self.system = system
        self._waiter = None

    def feed(self, text):
        self.messages.append(text or "")
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def pending(self):
        return self.messages[self.pos:]

    def find(self, pattern, start=None):
        """Index of the first message (>= start) matching ``pattern``, or None."""
        rx = re.compile(pattern, re.I | re.S)
        start = self.pos if start is None else start
        for i in range(start, len(self.messages)):
            if rx.search(self.messages[i]):
                return i
        return None

    def consume(self, pattern, start=None):
        """Match forward from ``self.pos``; advance past the match; return it."""
        i = self.find(pattern, start=start)
        if i is None:
            return None
        self.pos = i + 1
        return self.messages[i]

    def skipped(self, i):
        """Messages passed over before the match at ``i`` (context for logs)."""
        return self.messages[self.pos:i]

    async def wait_for(self, pattern, timeout=DEFAULT_STEP_TIMEOUT, label=None):
        """Wait until a message matching ``pattern`` arrives; return it."""
        deadline = time.monotonic() + timeout
        while True:
            i = self.find(pattern)
            if i is not None:
                text = self.messages[i]
                self.pos = i + 1
                return text
            if self.system is None:
                raise ExpectTimeout(
                    "no message matching %r (offline cursor, %d unread)"
                    % (pattern, len(self.pending())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ExpectTimeout(
                    "timed out waiting for %r (%s); unread=%s"
                    % (pattern, label or "", json.dumps(self.pending()[-3:])))
            self._waiter = asyncio.get_event_loop().create_future()
            try:
                await asyncio.wait_for(self._waiter, min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiter = None


# --------------------------------------------------------------------------
# XEP-0004 data-form helpers (pure — offline-testable)
# --------------------------------------------------------------------------
def _tag(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def parse_form(elem):
    """Parse an <x xmlns='jabber:x:data'> element into a plain dict.

    Returns None if ``elem`` is not a data form. Unknown/duplicate fields are
    preserved: the caller decides which ones to submit.
    """
    if elem is None or _tag(elem) != "x":
        return None
    if not (elem.tag.startswith("{%s}" % NS_XDATA) or elem.get("xmlns") == NS_XDATA):
        return None

    form = {"type": elem.get("type"), "title": None, "instructions": None,
            "fields": [], "reported": [], "items": []}
    for child in elem:
        name = _tag(child)
        if name == "title":
            form["title"] = child.text
        elif name == "instructions":
            form["instructions"] = (form["instructions"] or "") + (child.text or "")
        elif name == "field":
            field = {"var": child.get("var"), "type": child.get("type") or "text-single",
                     "label": child.get("label"), "desc": None, "values": [], "options": []}
            for sub in child:
                subname = _tag(sub)
                if subname == "value":
                    field["values"].append(sub.text)
                elif subname == "desc":
                    field["desc"] = sub.text
                elif subname == "option":
                    value = None
                    label = None
                    for option in sub:
                        if _tag(option) == "value":
                            value = option.text
                        elif _tag(option) == "label" and option.text:
                            label = option.text
                    field["options"].append({"value": value,
                                             "label": label or sub.get("label") or value})
            form["fields"].append(field)
    return form


def find_field(form, var):
    for field in form.get("fields", []):
        if field["var"] == var:
            return field
    return None


def form_text(form):
    """Human-readable rendering of a form — this is the bot's prompt text."""
    if not form:
        return "(no form)"
    out = []
    if form.get("title"):
        out.append(form["title"])
    if form.get("instructions"):
        out.append(form["instructions"].strip())
    for field in form.get("fields", []):
        if (field.get("type") or "") == "hidden":
            continue
        line = "  [%s] %s" % (field["var"], field.get("label") or "")
        if field.get("options"):
            line += "  options: " + ", ".join(
                "%s=%s" % (option["value"], option["label"]) for option in field["options"])
        if field.get("values"):
            line += "  value: " + ", ".join(str(v) for v in field["values"])
        if field.get("desc"):
            line += "  (%s)" % field["desc"]
        out.append(line.rstrip())
    return "\n".join(out)


def build_submit(fields, form_type="submit", base=None):
    """Build an <x xmlns='jabber:x:data'> submit element for ``fields``.

    ``base`` (a parsed form) is consulted for the FORM_TYPE hidden field, so a
    submitted form is well-formed for strict bots.
    """
    x = ET.Element("{%s}x" % NS_XDATA)
    x.set("type", form_type)
    submitted = dict(fields or {})
    if base:
        for field in base.get("fields", []):
            if field["var"] and field["type"] == "hidden" and field["var"] not in submitted:
                if field["var"] == "FORM_TYPE" and field.get("values"):
                    submitted[field["var"]] = field["values"][0]
    for var, value in submitted.items():
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        element = ET.SubElement(x, "field")
        element.set("var", var)
        for item in values:
            ET.SubElement(element, "value").text = "" if item is None else str(item)
    return x


def tostring(elem):
    """Verbatim XML for either an ElementTree element or a slixmpp stanza."""
    elem = getattr(elem, "xml", elem)
    try:
        from slixmpp.xmlstream.tostring import tostring as slix_tostring
        return slix_tostring(elem)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        return ET.tostring(elem, encoding="unicode")
    except Exception as exc:                                     # noqa: BLE001
        return "<unprintable %s: %s>" % (type(elem).__name__, exc)


def _elem(stanza):
    """Underlying XML element of a slixmpp stanza (or the element itself)."""
    return getattr(stanza, "xml", stanza)


# --------------------------------------------------------------------------
# XMPP client
# --------------------------------------------------------------------------
class XMPPProvisioner:
    """Thin slixmpp wrapper: connect, watch messages, send commands, log all."""

    def __init__(self, jid, password, transcript, bot=None, log=print):
        self.jid = jid
        self.password = password
        self.transcript = transcript
        self.bot = bot or jid.split("/")[0]
        self.log = log
        self.cursor = Cursor(system=lambda: True)
        self.forms = {}
        self.commands = []
        self._pending = {}
        self.xmpp = None

    @property
    def bare(self):
        return self.jid.split("/")[0]

    # -- lifecycle ---------------------------------------------------------
    def build(self):
        import slixmpp  # lazy: keeps offline tests importable

        self.slixmpp = slixmpp
        xmpp = slixmpp.ClientXMPP(self.jid, self.password)
        xmpp.register_plugin("xep_0030")   # service discovery
        xmpp.register_plugin("xep_0050")   # ad-hoc commands
        xmpp.register_plugin("xep_0004")   # data forms
        xmpp.register_plugin("xep_0077")   # in-band registration
        xmpp.register_plugin("xep_0199")   # ping (liveness)
        xmpp.add_event_handler("message", self._on_message)
        xmpp.add_event_handler("iq", self._on_iq_result)
        xmpp.add_event_handler("session_start", self._on_session_start)
        xmpp.add_event_handler("disconnected", self._on_disconnect)
        self.xmpp = xmpp
        return xmpp

    def _on_disconnect(self, event):
        self.transcript.note("DISCONNECTED: %r" % (event,))

    async def _on_session_start(self, event):
        self.xmpp.send_presence()
        try:
            await self.xmpp.get_roster()
        except Exception as exc:                                 # noqa: BLE001
            self.transcript.note("roster fetch failed: %s" % exc)

    async def connect(self, timeout=30):
        self.build()
        done = asyncio.get_event_loop().create_future()

        def _started(event):
            if not done.done():
                done.set_result(None)

        self.xmpp.add_event_handler("session_start", _started)
        self.xmpp.connect()
        try:
            await asyncio.wait_for(done, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("XMPP connect/session_start timed out after %ss" % timeout)
        self.transcript.note("connected as %s" % self.bare)
        await asyncio.sleep(1)
        return self

    async def close(self):
        if self.xmpp is not None:
            self.xmpp.disconnect()

    # -- inbound -----------------------------------------------------------
    def roster_jids(self) -> list[str]:
        """The roster as the server gave it (empty when it is locked down)."""
        roster = getattr(self.xmpp, "client_roster", None)
        try:
            return sorted(str(jid) for jid in roster.keys())
        except Exception:                                        # noqa: BLE001
            return []

    def _on_message(self, msg):
        mtype = msg.get("type")
        sender = str(msg["from"])
        body = msg["body"] or ""
        form = None
        for x in _elem(msg).iter():
            parsed = parse_form(x)
            if parsed is not None:
                form = parsed
                break
        extra = []
        for elem in _elem(msg).iter("{%s}oob" % NS_OOB):
            for url in elem:
                if _tag(url) == "url":
                    extra.append("OOB URL: %s" % url.text)
        text = body if body else ""
        if form:
            text = (text + "\n" if text else "") + form_text(form)
        if extra:
            text = (text + "\n" if text else "") + "\n".join(extra)
        if not text:
            text = "(empty %s stanza)" % mtype
        self.transcript.add("IN", text.strip(), raw=tostring(msg))
        self.cursor.feed(text)

    def _on_iq_result(self, iq):
        """Passive observer: log unsolicited IQs (server pushes, errors)."""
        self.transcript.note("unsolicited iq: type=%s from=%s" % (iq.get("type"), iq.get("from")))

    # -- outbound ----------------------------------------------------------
    def send_text(self, body, to=None):
        to = to or self.bot
        msg = self.xmpp.make_message(mto=to, mbody=body, mtype="chat")
        msg.send()
        self.transcript.add("OUT", "message -> %s\n%s" % (to, body), raw=tostring(msg))
        return msg

    def send_command(self, node, to=None, action="execute", sessionid=None, fields=None,
                     base_form=None, extra_xml=None, iq_type="set",
                     timeout=DEFAULT_STEP_TIMEOUT):
        """Send an XEP-0050 command stanza. Returns the iq id."""
        to = to or self.bot
        iq = self.xmpp.Iq()
        iq["type"] = iq_type
        iq["to"] = to
        cmd = ET.Element("{%s}command" % NS_COMMANDS)
        cmd.set("node", node)
        if sessionid:
            cmd.set("sessionid", sessionid)
        if action:
            cmd.set("action", action)
        if fields is not None:
            cmd.append(build_submit(fields, base=base_form))
        if extra_xml:
            for element in extra_xml:
                cmd.append(element)
        iq.append(cmd)
        self._pending[iq["id"]] = iq.send(timeout=timeout)
        shown = "command node=%s action=%s%s%s" % (
            node, action,
            " sessionid=%s" % sessionid if sessionid else "",
            "\nfields=%s" % json.dumps(fields, sort_keys=True) if fields else "")
        self.transcript.add("OUT", "%s -> %s" % (shown, to), raw=tostring(iq))
        return iq["id"]

    # -- command-plane helpers --------------------------------------------
    async def discover_commands(self, to=None, timeout=30):
        """disco#items (node=commands) -> [(node, name)] advertised by a bot."""
        to = to or self.bot
        iq = self.xmpp.Iq()
        iq["type"] = "get"
        iq["to"] = to
        query = ET.Element("{%s}query" % NS_DISCO_ITEMS)
        query.set("node", NS_COMMANDS)
        iq.append(query)
        self.transcript.add("OUT", "disco#items node=commands -> %s" % to, raw=tostring(iq))
        try:
            res = await iq.send(timeout=timeout)
        except Exception as exc:                                 # noqa: BLE001
            self.transcript.add("IN", "disco#items failed: %s: %s" % (type(exc).__name__, exc))
            raise
        items = []
        for item in _elem(res).iter("{%s}item" % NS_DISCO_ITEMS):
            items.append((item.get("node"), item.get("name")))
        self.commands = items
        for node, name in items:
            self.transcript.add("IN", "command: %s  (%s)" % (node, name))
        return items

    async def describe_command(self, node, to=None, timeout=30):
        """disco#info for one command node -> parsed form of its fields."""
        to = to or self.bot
        iq = self.xmpp.Iq()
        iq["type"] = "get"
        iq["to"] = to
        query = ET.Element("{%s}query" % NS_DISCO_INFO)
        query.set("node", node)
        iq.append(query)
        self.transcript.add("OUT", "disco#info node=%s -> %s" % (node, to), raw=tostring(iq))
        try:
            res = await iq.send(timeout=timeout)
        except Exception as exc:                                 # noqa: BLE001
            self.transcript.add("IN", "disco#info failed: %s: %s" % (type(exc).__name__, exc))
            raise
        for x in _elem(res).iter():
            parsed = parse_form(x)
            if parsed is not None:
                self.transcript.add("IN", form_text(parsed), raw=tostring(res))
                return parsed
        self.transcript.add("IN", "(no form in disco#info)", raw=tostring(res))
        return None

    async def run_command(self, node, to=None, action="execute", fields=None, sessionid=None,
                          record_form=True, base_form=None, timeout=DEFAULT_STEP_TIMEOUT):
        """Execute/continue a command and return (iq, form, sessionid)."""
        to = to or self.bot
        iq_id = self.send_command(node, to=to, action=action, sessionid=sessionid,
                                  fields=fields, base_form=base_form, timeout=timeout)
        pending = self._pending.pop(iq_id, None)
        if pending is None:
            raise RuntimeError("internal: command iq not registered")
        try:
            res = await pending
        except Exception as exc:                                 # noqa: BLE001
            self.transcript.add("IN", "command failed: %s: %s" % (type(exc).__name__, exc))
            raise
        if res.get("type") == "error":
            self.transcript.add("IN", "ERROR from bot", raw=tostring(res))
            raise RuntimeError("command %s failed: %s" % (node, tostring(res)))
        cmd = None
        for element in _elem(res).iter("{%s}command" % NS_COMMANDS):
            cmd = element
            break
        status = cmd.get("status") if cmd is not None else None
        new_session = cmd.get("sessionid") if cmd is not None else None
        form = None
        if cmd is not None:
            for x in cmd.iter():
                parsed = parse_form(x)
                if parsed is not None:
                    form = parsed
                    break
            for note in cmd:
                if _tag(note) == "note":
                    self.transcript.add("IN", "[%s] %s" % (note.get("type"), note.text))
            for element in cmd.iter("{%s}url" % NS_OOB):
                self.transcript.add("IN", "OOB URL: %s" % element.text)
        if form is not None:
            self.transcript.add("IN", "status=%s sessionid=%s\n%s"
                                % (status, new_session, form_text(form)), raw=tostring(res))
        else:
            self.transcript.add("IN", "status=%s sessionid=%s (no form)"
                                % (status, new_session), raw=tostring(res))
        return res, form, (new_session or sessionid)


# --------------------------------------------------------------------------
# step-script runner
# --------------------------------------------------------------------------
KNOWN_STEPS = {"text", "command", "submit", "action", "expect", "sleep", "note",
               "pick", "capture_field", "expect_form"}
KNOWN_MODIFIERS = {"label", "to", "timeout", "capture", "sessionid"}


class ExpectFailed(RuntimeError):
    """Raised when a form-level expectation is not met (ad-hoc replies)."""


def validate_script(steps):
    """Static validation of a step script — pure, unit-tested."""
    if not isinstance(steps, list):
        raise ValueError("script must be a JSON list of steps")
    for n, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError("step %d is not an object" % n)
        keys = set(step)
        unknown = keys - KNOWN_STEPS - KNOWN_MODIFIERS
        if unknown:
            raise ValueError("step %d: unknown step type(s) %s" % (n, sorted(unknown)))
        if not (keys & KNOWN_STEPS):
            raise ValueError("step %d has no action" % n)
        if "command" in step and "node" not in (step["command"] or {}):
            raise ValueError("step %d: command needs a node" % n)
        if "pick" in step and "field" not in (step["pick"] or {}):
            raise ValueError("step %d: pick needs a field" % n)
        if "capture_field" in step and "field" not in (step["capture_field"] or {}):
            raise ValueError("step %d: capture_field needs a field" % n)
    return steps


def pick_option(form, field, index=None, match=None):
    """Choose an option value out of ``form``'s list-single field (pure)."""
    found = find_field(form or {}, field)
    if found is None:
        raise ExpectFailed("no field %r in form: %s" % (field, form_text(form)))
    options = found.get("options") or []
    if not options:
        raise ExpectFailed("field %r offers no options" % field)
    if match:
        rx = re.compile(match, re.I)
        for option in options:
            if rx.search("%s %s" % (option["value"], option["label"] or "")):
                return option["value"]
        raise ExpectFailed("no option of %r matches %r" % (field, match))
    if index is None:
        index = 0
    try:
        return options[index]["value"]
    except IndexError:
        raise ExpectFailed("option %r out of range (%d options)" % (index, len(options)))


def field_value(form, field):
    """First value of ``field`` in ``form`` (for capturing bot-supplied data)."""
    found = find_field(form or {}, field)
    if found is None or not found.get("values"):
        raise ExpectFailed("field %r has no value in: %s" % (field, form_text(form)))
    return found["values"][0]


async def run_script(client, steps, stop_before=None, stop_after=None,
                     default_timeout=DEFAULT_STEP_TIMEOUT):
    """Execute steps against a connected client. Returns (captured, last form).

    A step may do one *action* (command/pick/submit/action/text) and, on the
    same step, one *assertion* (expect_form) and one *capture*
    (capture_field), which are evaluated against the form the action returned.
    """
    validate_script(steps)
    captured = {}
    session = None
    form = None
    node = None
    for n, step in enumerate(steps):
        label = step.get("label", "step %d" % n)
        if stop_before and stop_before in label:
            client.transcript.note("STOP before %s (as requested)" % label)
            break
        if "note" in step:
            client.transcript.note(step["note"])
        if "sleep" in step:
            await asyncio.sleep(float(step["sleep"]))
        if "expect" in step:
            text = await client.cursor.wait_for(
                step["expect"], timeout=float(step.get("timeout", default_timeout)), label=label)
            client.transcript.note("MATCHED %r in %s" % (step["expect"], label))
            if "capture" in step:
                captured[step["capture"]] = text

        # ---- one action per step ----------------------------------------
        if "command" in step:
            spec = step["command"]
            node = spec.get("node", node)
            _, form, session = await client.run_command(
                node, to=spec.get("to"), action=spec.get("action", "execute"),
                fields=spec.get("fields"), sessionid=spec.get("sessionid"),
                timeout=float(step.get("timeout", default_timeout)))
        elif "pick" in step:
            spec = step["pick"]
            value = pick_option(form, spec["field"], index=spec.get("index"),
                                match=spec.get("match"))
            client.transcript.note("PICKED %s=%s" % (spec["field"], value))
            _, form, session = await client.run_command(
                node, to=step.get("to"), action="next", fields={spec["field"]: value},
                sessionid=session, base_form=form,
                timeout=float(step.get("timeout", default_timeout)))
        elif "submit" in step:
            _, form, session = await client.run_command(
                node, to=step.get("to"), action=step.get("action", "next"),
                fields=step["submit"], sessionid=step.get("sessionid", session),
                base_form=form, timeout=float(step.get("timeout", default_timeout)))
        elif "action" in step:
            _, form, session = await client.run_command(
                node, action=step["action"], sessionid=session,
                timeout=float(step.get("timeout", default_timeout)))
            client.transcript.note("sessionid=%s after action=%s" % (session, step["action"]))
        elif "text" in step:
            client.send_text(step["text"], to=step.get("to"))

        # ---- assertions / captures on the form just received -------------
        text = form_text(form) if form else "(no form)"
        if "expect_form" in step:
            if not re.search(step["expect_form"], text, re.I | re.S):
                raise ExpectFailed("form did not match %r in %s; saw:\n%s"
                                   % (step["expect_form"], label, text))
            client.transcript.note("FORM MATCHED %r (%s)" % (step["expect_form"], label))
        if "capture_field" in step:
            spec = step["capture_field"]
            value = field_value(form, spec["field"])
            captured[spec.get("as", spec["field"])] = value
            client.transcript.note("CAPTURED %s = %s"
                                   % (spec.get("as", spec["field"]), value))
        if stop_after and stop_after in label:
            client.transcript.note("STOP after %s (as requested)" % label)
            break
    return captured, form


# --------------------------------------------------------------------------
# the JMP funding flow — ported verbatim from flows/jmp-register-phase-a.json
# --------------------------------------------------------------------------
#: Steps that drive cheogram.com from nothing to the funding facts. It selects
#: the **bitcoin** activation method (which is what makes the bot print the
#: deposit address and amount) and then stops. Selecting a payment method in
#: this bot is not a payment: the address is only ever funded by a human who
#: sends BTC to it. Card selection and card submission are absent by design.
JMP_FUNDING_STEPS = [
    {"note": ("JMP/Cheogram signup — drive to the FUNDING step only. The bot is "
              "cheogram.com, command node 'jabber:iq:register'. NOTHING here can "
              "pay: no card method, no card form, no OOB payment URL.")},

    {"command": {"node": JMP_REGISTER_NODE, "action": "execute"},
     "label": "execute-register",
     "expect_form": "Choose a phone number provider", "timeout": 180},

    {"submit": {"gateway-jid": "jmp.chat"},
     "label": "choose-provider-jmp",
     "expect_form": "Search Telephone Numbers", "timeout": 180},

    {"submit": {"q": "", ACTIONS_FIELD: "feelinglucky"},
     "label": "search-any-numbers",
     "expect_form": "choose one of the following numbers", "timeout": 180},

    {"pick": {"field": "tel", "index": 0},
     "label": "choose-number",
     "expect_form": "You've selected", "timeout": 180},

    {"submit": {"activation_method": "bitcoin", "plan_name": "USD"},
     "label": "select-bitcoin",
     "expect_form": "Bitcoin address", "timeout": 180},

    {"capture_field": {"field": "btc_addresses", "as": "btc_address"},
     "label": "capture-btc-address"},

    {"capture_field": {"field": "amount", "as": "btc_amount"},
     "label": "capture-btc-amount"},

    {"note": ("STOP — funding facts captured. Nothing was paid; a human sends the "
              "BTC to the captured address.")},
]

#: The number the bot reserved in this session, as the bot prints it.
SELECTED_NUMBER_RE = re.compile(
    r"You've selected\s+(\+?1?[\s-]*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})", re.I)

#: Shapes that mean money could move. A step matching any of these must never be
#: emitted by this driver, and `assert_no_payment_emitted` enforces that on the
#: transcript (not on the step list alone — the transcript is what actually went
#: out on the wire).
PAYMENT_MARKERS = (
    "credit_card", "creditcard", "card_number", "card-number", "cvv", "cvc",
    "braintree", "payment_method", "pay-by", "save_card", "auto_top_up",
)

#: Values that are a payment *method* selection rather than a funding request.
#: `bitcoin` is deliberately NOT here: it is the step that *reveals* the deposit
#: address and moves no money.
FORBIDDEN_ACTIVATION_METHODS = ("credit_card", "creditcard", "card", "bch", "code", "mail")


def looks_like_payment_step(step) -> bool:
    """Pure predicate: would executing this step be a payment action?"""
    blob = json.dumps(step, sort_keys=True).lower()
    if any(marker in blob for marker in PAYMENT_MARKERS):
        return True
    submit = step.get("submit") or {}
    method = str(submit.get("activation_method", "")).lower()
    if method and method in FORBIDDEN_ACTIVATION_METHODS:
        return True
    return False


def assert_no_payment_emitted(transcript, steps=JMP_FUNDING_STEPS) -> None:
    """Raise if the flow, or what it sent, contains a payment action.

    Two independent checks: the step list is static, the transcript is what
    actually left the process. Checking both is what makes "incapable of
    spending" a fact about this run rather than a claim about the source.
    """
    for n, step in enumerate(steps):
        if looks_like_payment_step(step):
            raise AssertionError(
                "step %d of the JMP flow looks like a payment action and must "
                "never be emitted: %s" % (n, json.dumps(step, sort_keys=True)))
    for line in transcript.outbound():
        lowered = line.lower()
        for marker in PAYMENT_MARKERS:
            if marker in lowered:
                raise AssertionError(
                    "a payment-shaped stanza went out on the wire (%r): %r"
                    % (marker, line))
        if "oob url" in lowered or "pay.jmp.chat" in lowered:
            raise AssertionError(
                "an out-of-band payment URL was emitted or followed: %r" % (line,))


def selected_number(transcript):
    """The number the bot said it selected, or None (never guessed)."""
    for text in reversed(transcript.texts()):
        match = SELECTED_NUMBER_RE.search(text or "")
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


#: The USD minimum the bot states in its own instructions ("deposit $20.00 to
#: your balance"). Read, never re-derived from a BTC/USD rate we cannot see.
USD_MINIMUM_RE = re.compile(r"deposit\s+\$(\d+(?:\.\d{2})?)", re.I)
#: The bot says this only after a funded activation.
ACTIVATED_RE = re.compile(r"has been activated as", re.I)


def funding_facts_from_transcript(transcript, captured=None) -> dict:
    """Assemble the funding payload from what the bot actually said.

    Every field is either read out of the bot's own output (`captured`, the
    transcript's "You've selected ..." line) or reported as absent — nothing is
    invented, and no default is substituted for a value the flow did not
    produce. `unobserved` names the fields this run could not read, so a caller
    never has to guess whether `null` means "zero" or "unknown".
    """
    captured = captured or {}
    texts = "\n".join(transcript.texts())
    usd = USD_MINIMUM_RE.search(texts)
    facts = {
        "number": selected_number(transcript),
        "number_state": "live" if ACTIVATED_RE.search(texts) else "reserved",
        "btc_address": captured.get("btc_address"),
        "amount_btc": captured.get("btc_amount"),
        "amount_usd_min": usd.group(1) if usd else None,
        "balance_usd": None,          # the bot only states this once funded
        "activation_state": "unfunded" if not ACTIVATED_RE.search(texts) else "active",
        "as_of": now_iso(),
        "source": "live-flow",
    }
    facts["unobserved"] = sorted(k for k, v in facts.items()
                                 if v is None and k not in ("as_of", "source"))
    facts["number_note"] = NUMBER_CAVEAT
    return facts


#: The caveat that must travel with the number everywhere it is served: the
#: reservation is per-session and the observed value has genuinely changed
#: between sessions (see docs/jmp-cvm-tools.md for the three transcripts).
NUMBER_CAVEAT = (
    "Session-scoped reservation, not ownership: JMP numbers are held for the "
    "session that selected them and the offer changes between runs (three live "
    "sessions observed three different option-1 numbers). Re-drive the flow to "
    "confirm the current one before relying on it."
)


# --------------------------------------------------------------------------
# the two live entry points the CVM tools call
# --------------------------------------------------------------------------
async def read_only_probe(client, bot=JMP_BOT, timeout=30) -> dict:
    """Roster + ad-hoc command discovery + the register command's form text.

    "Read-only" is enforced by construction: every stanza this sends is an IQ
    `get` (disco#items / disco#info), it never sends a chat message and never
    runs a command, so it cannot change the account's state. The register form's
    *text* is the only thing read from the command plane, and it is read, not
    answered.
    """
    probe: dict = {"bot": bot, "roster": [], "commands": [], "register_form": None}
    roster = getattr(client, "roster_jids", None)
    if callable(roster):
        probe["roster"] = list(roster())
    commands = await client.discover_commands(bot, timeout=timeout)
    probe["commands"] = [{"node": node, "name": name} for node, name in commands]
    form = await client.describe_command(JMP_REGISTER_NODE, bot, timeout=timeout)
    probe["register_form"] = form_text(form) if form else None
    return probe


async def drive_funding_live(jid, password, bot=JMP_BOT, transcript_path=None,
                             redact=(), client_factory=XMPPProvisioner) -> dict:
    """Connect and drive the flow to the funding step. Returns the live facts.

    Safe by construction and checked: `assert_no_payment_emitted` runs on the
    transcript before the facts are returned, so a payment-shaped stanza cannot
    leave without failing the call. `client_factory` is injectable so the whole
    drive is exercised offline against a fake bot.
    """
    transcript = Transcript(transcript_path, echo=False, redact=redact)
    client = client_factory(jid, password, transcript, bot=bot)
    await client.connect()
    try:
        captured, _form = await run_script(client, JMP_FUNDING_STEPS)
        assert_no_payment_emitted(transcript, JMP_FUNDING_STEPS)
        return funding_facts_from_transcript(transcript, captured)
    finally:
        await client.close()


async def probe_status_live(jid, password, bot=JMP_BOT, transcript_path=None,
                            redact=(), client_factory=XMPPProvisioner) -> dict:
    """The read-only live reading `jmp.status` performs when a rail is wired."""
    transcript = Transcript(transcript_path, echo=False, redact=redact)
    client = client_factory(jid, password, transcript, bot=bot)
    await client.connect()
    try:
        probe = await read_only_probe(client, bot=bot)
        return cached_payload_with_probe(probe)
    finally:
        await client.close()


def cached_payload_with_probe(probe: dict) -> dict:
    """Cached funding facts plus what the read-only probe saw.

    The facts stay `cached` and keep their capture timestamp: a roster lookup
    does not produce a deposit address, so calling them `live-flow` would be a
    lie. What the probe *does* prove — that the account is reachable and what
    the bot currently advertises — is attached as `read_only_probe`.
    """
    from .jmp_facts import cached_payload
    payload = cached_payload()
    payload["read_only_probe"] = {
        "bot": probe.get("bot"),
        "roster_size": len(probe.get("roster") or []),
        "commands": probe.get("commands") or [],
        "register_form": probe.get("register_form"),
        "note": ("Read-only: roster + disco + the register form's text. It does "
                 "not reserve a number and does not produce a funding address, "
                 "so the facts above remain the cached capture."),
    }
    return payload
