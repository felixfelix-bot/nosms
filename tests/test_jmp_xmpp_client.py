"""Offline tests for `XMPPProvisioner` itself — the slixmpp-driven client.

`tests/test_jmp_flow.py` proves the pure layer (forms, cursor, script runner,
the no-payment guarantee) and drives the *flow* against a fake bot object. What
it never does is exercise `XMPPProvisioner` — the class that actually talks to
the wire — because `jmp_flow.py` imports `slixmpp` lazily and the class was
mocked away. These tests close that gap: a fake `xmpp` object stands in for the
slixmpp client, so every method of the class runs for real (build, the event
handlers, send_text/send_command, discover/describe/run_command, roster_jids)
and the assertions are made on what the class actually emitted — the IQ id, the
`<command>` node/action/sessionid, the XEP-0004 submit form, the transcript.

No network, no sockets, no real slixmpp session: `build()` is exercised against
a patched `slixmpp.ClientXMPP`, and every other method against `FakeXmpp`.
"""
from __future__ import annotations

import asyncio
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from app.jmp_flow import (
    NS_COMMANDS,
    NS_DISCO_INFO,
    NS_DISCO_ITEMS,
    NS_OOB,
    NS_XDATA,
    Transcript,
    XMPPProvisioner,
    build_submit,
    parse_form,
)

CLIENT_NS = "jabber:client"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class Stanza:
    """A received stanza: an ElementTree element wearing the mapping API the
    slixmpp stanzas expose (`msg['from']`, `msg.get('type')`)."""

    def __init__(self, xml):
        self.xml = xml

    def get(self, key, default=None):
        return self.xml.get(key, default)

    def __getitem__(self, key):
        if key == "body":
            for child in self.xml:
                if child.tag.rsplit("}", 1)[-1] == "body":
                    return child.text
            return None
        return self.xml.get(key)


def _tag(elem):
    return elem.tag.rsplit("}", 1)[-1]


def message_stanza(body=None, mtype="chat", sender="cheogram.com", extra=()):
    m = ET.Element("{%s}message" % CLIENT_NS)
    m.set("type", mtype)
    m.set("from", sender)
    if body is not None:
        b = ET.SubElement(m, "{%s}body" % CLIENT_NS)
        b.text = body
    for child in extra:
        m.append(child)
    return Stanza(m)


def iq_stanza(iq_type="result", command=None, extra=()):
    iq = ET.Element("{%s}iq" % CLIENT_NS)
    iq.set("type", iq_type)
    iq.set("from", "cheogram.com")
    if command is not None:
        iq.append(command)
    for child in extra:
        iq.append(child)
    return Stanza(iq)


def command_element(status=None, sessionid=None, children=()):
    cmd = ET.Element("{%s}command" % NS_COMMANDS)
    cmd.set("node", "jabber:iq:register")
    if status:
        cmd.set("status", status)
    if sessionid:
        cmd.set("sessionid", sessionid)
    for child in children:
        cmd.append(child)
    return cmd


class _Send:
    """The awaitable `send()` hands back — resolving only when awaited.

    A plain `async def send` would leave an un-awaited coroutine behind whenever
    a test calls `send_command` without following up with `run_command` (which
    is exactly what the real code does: it parks the awaitable in `_pending`).
    """

    def __init__(self, iq):
        self.iq = iq

    def __await__(self):
        async def _run():
            self.iq.sent = True
            if self.iq.owner.raise_on_send is not None:
                raise self.iq.owner.raise_on_send
            return self.iq.owner.response_for(self.iq)
        return _run().__await__()


class FakeIq:
    """Stand-in for `xmpp.Iq()`: attribute-mapping, appendable, awaitable."""

    counter = 0

    def __init__(self, owner):
        FakeIq.counter += 1
        self.owner = owner
        self.attrs = {"id": "iq-%d" % FakeIq.counter}
        self.children = []
        self.xml = ET.Element("{%s}iq" % CLIENT_NS)
        self.xml.set("id", self.attrs["id"])
        self.sent = False
        self.sent_timeout = None

    def __setitem__(self, key, value):
        self.attrs[key] = value
        self.xml.set(key, value)

    def __getitem__(self, key):
        return self.attrs[key]

    def get(self, key, default=None):
        return self.attrs.get(key, default)

    def append(self, child):
        self.children.append(child)
        self.xml.append(child)

    def send(self, timeout=None):
        self.sent_timeout = timeout
        return _Send(self)


class FakeMessage:
    def __init__(self, mto, mbody, mtype):
        self.xml = ET.Element("{%s}message" % CLIENT_NS)
        self.xml.set("to", mto or "")
        self.xml.set("type", mtype or "")
        body = ET.SubElement(self.xml, "{%s}body" % CLIENT_NS)
        body.text = mbody
        self.sent = False

    def send(self):
        self.sent = True


class FakeXmpp:
    """A slixmpp-shaped client: plugins, handlers, roster, IQs, messages."""

    def __init__(self, client_roster=None):
        self.plugins = []
        self.handlers = {}
        self.presence_sent = 0
        self.roster_calls = 0
        self.roster_exc = None
        self.connected = False
        self.disconnected = False
        self.raise_on_send = None
        self.responses = []
        self.client_roster = {} if client_roster is None else client_roster
        self.messages = []
        self.iqs = []

    # -- registration ----------------------------------------------------
    def register_plugin(self, name):
        self.plugins.append(name)

    def add_event_handler(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    # -- session ---------------------------------------------------------
    def connect(self):
        self.connected = True
        for handler in self.handlers.get("session_start", []):
            handler(None)

    def disconnect(self):
        self.disconnected = True

    def send_presence(self):
        self.presence_sent += 1

    async def get_roster(self):
        self.roster_calls += 1
        if self.roster_exc is not None:
            raise self.roster_exc
        return self.client_roster

    # -- stanzas ---------------------------------------------------------
    def make_message(self, mto=None, mbody=None, mtype=None):
        msg = FakeMessage(mto, mbody, mtype)
        self.messages.append(msg)
        return msg

    def Iq(self):
        iq = FakeIq(self)
        self.iqs.append(iq)
        return iq

    def response_for(self, iq):
        return self.responses.pop(0) if self.responses else None


def provisioner(**kw):
    transcript = kw.pop("transcript", None) or Transcript(None, echo=False)
    client = XMPPProvisioner("alice@cheogram.com/resource", "secret", transcript, **kw)
    return client, transcript


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------
class TestConstruction(unittest.TestCase):
    def test_defaults_and_bare_jid(self):
        client, _ = provisioner(transcript=Transcript(None, echo=False, redact=["secret"]))
        self.assertEqual(client.bare, "alice@cheogram.com")
        self.assertEqual(client.bot, "alice@cheogram.com")   # bot defaults to the bare jid
        self.assertEqual(client.jid, "alice@cheogram.com/resource")
        self.assertIsNone(client.xmpp)
        self.assertEqual(client._pending, {})
        self.assertEqual(client.forms, {})
        self.assertEqual(client.commands, [])
        self.assertTrue(client.cursor.system())              # cursor is a live one

    def test_explicit_bot_and_log_are_kept(self):
        seen = []
        client, _ = provisioner(bot="cheogram.com", log=seen.append)
        self.assertEqual(client.bot, "cheogram.com")
        client.log("hello")
        self.assertEqual(seen, ["hello"])


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
class TestBuildAndLifecycle(unittest.TestCase):
    def test_build_registers_plugins_and_handlers(self):
        client, _ = provisioner()
        fake = FakeXmpp()
        with mock.patch("slixmpp.ClientXMPP", return_value=fake) as factory:
            built = client.build()
        factory.assert_called_once_with(client.jid, client.password)
        self.assertIs(built, fake)
        self.assertIs(client.xmpp, fake)
        self.assertEqual(fake.plugins, ["xep_0030", "xep_0050", "xep_0004", "xep_0077", "xep_0199"])
        for event in ("message", "iq", "session_start", "disconnected"):
            self.assertIn(event, fake.handlers, event)
        self.assertEqual(fake.handlers["message"], [client._on_message])
        self.assertEqual(fake.handlers["iq"], [client._on_iq_result])
        self.assertEqual(fake.handlers["disconnected"], [client._on_disconnect])

    def test_on_disconnect_notes_the_event(self):
        client, transcript = provisioner()
        client._on_disconnect("stream closed")
        self.assertEqual(transcript.records[-1]["dir"], "NOTE")
        self.assertIn("DISCONNECTED", transcript.records[-1]["summary"])
        self.assertIn("stream closed", transcript.records[-1]["summary"])

    def test_close_disconnects_only_when_there_is_a_client(self):
        client, _ = provisioner()
        asyncio.run(client.close())                     # xmpp is None -> no-op, no raise
        fake = FakeXmpp()
        client.xmpp = fake
        asyncio.run(client.close())
        self.assertTrue(fake.disconnected)

    def test_on_iq_result_logs_unsolicited_iqs(self):
        client, transcript = provisioner()
        client._on_iq_result(iq_stanza(iq_type="error"))
        self.assertIn("unsolicited iq", transcript.records[-1]["summary"])
        self.assertIn("type=error", transcript.records[-1]["summary"])


class TestSessionStart(unittest.IsolatedAsyncioTestCase):
    async def test_session_start_sends_presence_and_fetches_roster(self):
        client, transcript = provisioner()
        fake = FakeXmpp(client_roster={"cheogram.com": None})
        client.xmpp = fake
        await client._on_session_start(None)
        self.assertEqual(fake.presence_sent, 1)
        self.assertEqual(fake.roster_calls, 1)
        self.assertEqual(transcript.texts(), [])

    async def test_a_locked_roster_is_noted_not_fatal(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        fake.roster_exc = RuntimeError("forbidden")
        client.xmpp = fake
        await client._on_session_start(None)
        self.assertEqual(fake.presence_sent, 1)
        self.assertIn("roster fetch failed: forbidden", transcript.records[-1]["summary"])


class TestConnect(unittest.IsolatedAsyncioTestCase):
    async def test_connect_waits_for_session_start(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        client.build = lambda: (setattr(client, "xmpp", fake), fake)[1]
        with mock.patch("app.jmp_flow.asyncio.sleep", new=_async_noop):
            returned = await client.connect(timeout=5)
        self.assertIs(returned, client)
        self.assertTrue(fake.connected)
        self.assertIn("connected as alice@cheogram.com", transcript.records[-1]["summary"])

    async def test_connect_times_out_when_session_start_never_fires(self):
        client, _ = provisioner()
        fake = FakeXmpp()
        fake.connect = lambda: None                      # never triggers session_start
        client.build = lambda: (setattr(client, "xmpp", fake), fake)[1]
        with self.assertRaises(RuntimeError) as ctx:
            await client.connect(timeout=0.01)
        self.assertIn("timed out after 0.01s", str(ctx.exception))


async def _async_noop(*_args, **_kwargs):
    return None


# --------------------------------------------------------------------------
# inbound
# --------------------------------------------------------------------------
class TestRosterJids(unittest.TestCase):
    def test_roster_is_sorted_strings(self):
        client, _ = provisioner()
        client.xmpp = FakeXmpp(client_roster={"b@x": None, "a@x": None})
        self.assertEqual(client.roster_jids(), ["a@x", "b@x"])

    def test_a_missing_or_broken_roster_is_an_empty_list(self):
        client, _ = provisioner()
        client.xmpp = FakeXmpp()
        client.xmpp.client_roster = None                 # getattr -> None -> .keys() raises
        self.assertEqual(client.roster_jids(), [])

        class Broken:
            def keys(self):
                raise ValueError("nope")

        client.xmpp.client_roster = Broken()
        self.assertEqual(client.roster_jids(), [])

    def test_no_xmpp_yet_is_also_empty(self):
        client, _ = provisioner()
        self.assertEqual(client.roster_jids(), [])


class TestOnMessage(unittest.TestCase):
    def test_plain_text_is_recorded_and_fed_to_the_cursor(self):
        client, transcript = provisioner()
        client._on_message(message_stanza("Bitcoin address: bc1qexample"))
        self.assertEqual(transcript.records[-1]["dir"], "IN")
        self.assertIn("bc1qexample", transcript.records[-1]["summary"])
        self.assertEqual(client.cursor.pending(), ["Bitcoin address: bc1qexample"])

    def test_form_and_oob_url_are_rendered_into_the_text(self):
        client, transcript = provisioner()
        form = ET.fromstring(
            '<x xmlns="jabber:x:data" type="form"><title>Choose</title>'
            '<field var="tel"><value>+141****0123</value></field></x>')
        oob = ET.Element("{%s}oob" % NS_OOB)
        url = ET.SubElement(oob, "{%s}url" % NS_OOB)
        url.text = "https://pay.jmp.chat/x/bitcoin"
        client._on_message(message_stanza("hi", extra=[form, oob]))
        summary = transcript.records[-1]["summary"]
        self.assertIn("[tel]", summary)
        self.assertIn("OOB URL: https://pay.jmp.chat/x/bitcoin", summary)

    def test_an_empty_stanza_is_named_by_its_type(self):
        client, transcript = provisioner()
        client._on_message(message_stanza(None, mtype="groupchat"))
        self.assertEqual(transcript.records[-1]["summary"], "(empty groupchat stanza)")


# --------------------------------------------------------------------------
# outbound
# --------------------------------------------------------------------------
class TestSendText(unittest.TestCase):
    def test_send_text_defaults_to_the_bot_and_records_raw_xml(self):
        client, transcript = provisioner(bot="cheogram.com")
        fake = FakeXmpp()
        client.xmpp = fake
        msg = client.send_text("register jmp.chat")
        self.assertTrue(msg.sent)
        self.assertEqual(msg.xml.get("to"), "cheogram.com")
        self.assertEqual(transcript.records[-1]["dir"], "OUT")
        self.assertIn("message -> cheogram.com", transcript.records[-1]["summary"])
        self.assertIn("register jmp.chat", transcript.records[-1]["summary"])
        self.assertIsNotNone(transcript.records[-1]["raw"])

    def test_send_text_honours_an_explicit_recipient(self):
        client, _ = provisioner()
        client.xmpp = FakeXmpp()
        msg = client.send_text("hi", to="other@x")
        self.assertEqual(msg.xml.get("to"), "other@x")


class TestSendCommand(unittest.TestCase):
    def test_command_stanza_carries_node_action_sessionid_and_fields(self):
        client, transcript = provisioner(bot="cheogram.com")
        fake = FakeXmpp()
        client.xmpp = fake
        base = parse_form(ET.fromstring(
            '<x xmlns="jabber:x:data" type="form">'
            '<field var="FORM_TYPE" type="hidden"><value>http://jabber.org/protocol/commands</value></field>'
            '<field var="activation_method"><value>bitcoin</value></field></x>'))
        extra = ET.Element("{%s}extra" % NS_COMMANDS)
        iq_id = client.send_command("jabber:iq:register", action="next", sessionid="s-1",
                                    fields={"activation_method": "bitcoin"}, base_form=base,
                                    extra_xml=[extra])
        command = fake.iqs[-1].children[0]
        self.assertEqual(iq_id, fake.iqs[-1].attrs["id"])
        self.assertEqual(command.get("node"), "jabber:iq:register")
        self.assertEqual(command.get("action"), "next")
        self.assertEqual(command.get("sessionid"), "s-1")
        # the submit form: FORM_TYPE inherited from base + the submitted field
        submit = command.find("{%s}x" % NS_XDATA)
        self.assertIsNotNone(submit)
        vars_ = [f.get("var") for f in submit.findall("field")]
        self.assertEqual(set(vars_), {"FORM_TYPE", "activation_method"})
        values = {f.get("var"): f.find("value").text for f in submit.findall("field")}
        self.assertEqual(values["activation_method"], "bitcoin")
        self.assertEqual(values["FORM_TYPE"], "http://jabber.org/protocol/commands")
        # extra_xml lands inside the command
        self.assertIsNotNone(command.find("{%s}extra" % NS_COMMANDS))
        self.assertIn("command node=jabber:iq:register action=next", transcript.records[-1]["summary"])
        self.assertIn("sessionid=s-1", transcript.records[-1]["summary"])
        # the pending awaitable was registered under the iq id
        self.assertIn(iq_id, client._pending)

    def test_command_without_session_or_fields_omits_them(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        client.xmpp = fake
        client.send_command("node.x", action="execute")
        command = fake.iqs[-1].children[0]
        self.assertIsNone(command.get("sessionid"))
        self.assertIsNone(command.find("{%s}x" % NS_XDATA))
        self.assertNotIn("fields=", transcript.records[-1]["summary"])


# --------------------------------------------------------------------------
# the command plane
# --------------------------------------------------------------------------
class TestDiscoverAndDescribe(unittest.IsolatedAsyncioTestCase):
    async def test_discover_commands_parses_discovery_items(self):
        client, transcript = provisioner(bot="cheogram.com")
        fake = FakeXmpp()
        iq = ET.Element("{%s}iq" % CLIENT_NS)
        query = ET.SubElement(iq, "{%s}query" % NS_DISCO_ITEMS)
        ET.SubElement(query, "{%s}item" % NS_DISCO_ITEMS,
                      {"node": "jabber:iq:register", "name": "Register"})
        ET.SubElement(query, "{%s}item" % NS_DISCO_ITEMS,
                      {"node": "urn:x:balance", "name": "Balance"})
        fake.responses = [Stanza(iq)]
        client.xmpp = fake
        items = await client.discover_commands()
        self.assertEqual(items, [("jabber:iq:register", "Register"), ("urn:x:balance", "Balance")])
        self.assertEqual(client.commands, items)
        self.assertIn("command: jabber:iq:register  (Register)", transcript.texts())
        self.assertIn("command: urn:x:balance  (Balance)", transcript.texts())
        sent = fake.iqs[-1].children[0]
        self.assertEqual(sent.get("node"), NS_COMMANDS)

    async def test_discover_commands_failure_is_noted_and_reraised(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        fake.raise_on_send = TimeoutError("slow")
        client.xmpp = fake
        with self.assertRaises(TimeoutError):
            await client.discover_commands()
        self.assertIn("disco#items failed: TimeoutError: slow", transcript.records[-1]["summary"])

    async def test_describe_command_returns_the_parsed_form(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        iq = ET.Element("{%s}iq" % CLIENT_NS)
        iq.append(ET.fromstring(
            '<x xmlns="jabber:x:data" type="form"><title>Register</title>'
            '<field var="gateway-jid"><value>jmp.chat</value></field></x>'))
        fake.responses = [Stanza(iq)]
        client.xmpp = fake
        form = await client.describe_command("jabber:iq:register")
        self.assertIsNotNone(form)
        self.assertEqual(form["title"], "Register")
        self.assertIn("[gateway-jid]", transcript.texts()[-1])
        self.assertEqual(fake.iqs[-1].children[0].get("node"), "jabber:iq:register")

    async def test_describe_command_without_a_form_returns_none(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        fake.responses = [iq_stanza(command=command_element())]
        client.xmpp = fake
        self.assertIsNone(await client.describe_command("node.x"))
        self.assertEqual(transcript.texts()[-1], "(no form in disco#info)")

    async def test_describe_command_failure_is_noted_and_reraised(self):
        client, transcript = provisioner()
        fake = FakeXmpp()
        fake.raise_on_send = RuntimeError("boom")
        client.xmpp = fake
        with self.assertRaises(RuntimeError):
            await client.describe_command("node.x")
        self.assertIn("disco#info failed: RuntimeError: boom", transcript.records[-1]["summary"])


class TestRunCommand(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        client, transcript = provisioner(bot="cheogram.com")
        fake = FakeXmpp()
        client.xmpp = fake
        return client, transcript, fake

    async def test_unregistered_iq_id_is_an_internal_error(self):
        client, _, _ = self._client()
        client.send_command = lambda *a, **k: "never-registered"
        with self.assertRaises(RuntimeError) as ctx:
            await client.run_command("node.x")
        self.assertIn("not registered", str(ctx.exception))

    async def test_a_failed_send_is_noted_and_reraised(self):
        client, transcript, fake = self._client()
        fake.raise_on_send = OSError("pipe")
        with self.assertRaises(OSError):
            await client.run_command("node.x")
        self.assertIn("command failed: OSError: pipe", transcript.records[-1]["summary"])

    async def test_an_error_type_response_becomes_a_runtime_error(self):
        client, transcript, fake = self._client()
        fake.responses = [iq_stanza(iq_type="error")]
        with self.assertRaises(RuntimeError) as ctx:
            await client.run_command("node.x")
        self.assertIn("ERROR from bot", transcript.records[-1]["summary"])
        self.assertIn("command node.x failed", str(ctx.exception))

    async def test_a_response_without_a_command_element_reports_no_form(self):
        client, transcript, fake = self._client()
        fake.responses = [iq_stanza()]
        res, form, session = await client.run_command("node.x", sessionid="s-7")
        self.assertIsNone(form)
        self.assertEqual(session, "s-7")                 # session falls back to what we sent
        self.assertIn("status=None sessionid=None (no form)", transcript.texts()[-1])

    async def test_form_note_oob_and_new_session_are_all_recorded(self):
        client, transcript, fake = self._client()
        note = ET.Element("{%s}note" % NS_COMMANDS, {"type": "info"})
        note.text = "choose a number"
        oob = ET.Element("{%s}url" % NS_OOB)
        oob.text = "https://pay.jmp.chat/x/bitcoin"
        form = ET.fromstring(
            '<x xmlns="jabber:x:data" type="result"><title>Number</title>'
            '<field var="tel"><value>+141****0123</value></field></x>')
        command = command_element(status="completed", sessionid="s-2",
                                  children=[note, oob, form])
        fake.responses = [iq_stanza(command=command)]
        res, parsed, session = await client.run_command("node.x", sessionid="s-1")
        self.assertEqual(session, "s-2")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "Number")
        texts = transcript.texts()
        self.assertIn("[info] choose a number", texts)
        self.assertIn("OOB URL: https://pay.jmp.chat/x/bitcoin", texts)
        self.assertIn("[tel]", texts[-1])
        self.assertIn("status=completed sessionid=s-2", texts[-1])


# --------------------------------------------------------------------------
# the pure helpers whose gaps the flow tests left open
# --------------------------------------------------------------------------
class TestHelperEdges(unittest.TestCase):
    def test_echo_transcript_writes_to_stdout(self):
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            t = Transcript(None, echo=True)
            t.add("IN", "the bot speaks")
        self.assertIn("the bot speaks", buffer.getvalue())

    def test_parse_form_rejects_a_non_data_x_element(self):
        self.assertIsNone(parse_form(ET.fromstring('<x xmlns="something-else"/>')))

    def test_parse_form_keeps_desc_and_option_label_children(self):
        form = parse_form(ET.fromstring(
            '<x xmlns="jabber:x:data" type="form">'
            '<field var="tel" label="Available">'
            '<desc>pick one</desc>'
            '<option label="Toronto"><value>+141****0123</value></option>'
            '<option><value>+121****0124</value></option>'
            '</field></x>'))
        field = form["fields"][0]
        self.assertEqual(field["desc"], "pick one")
        self.assertEqual(field["options"][0], {"value": "+141****0123", "label": "Toronto"})
        # no <label> child -> the option falls back to its value
        self.assertEqual(field["options"][1], {"value": "+121****0124", "label": "+121****0124"})

    def test_form_text_of_nothing_and_of_a_described_field(self):
        from app.jmp_flow import form_text
        self.assertEqual(form_text(None), "(no form)")
        rendered = form_text({"title": None, "instructions": None, "fields": [
            {"var": "q", "type": "text-single", "label": "Search",
             "desc": "leave empty for any", "values": [], "options": []}]})
        self.assertIn("  [q] Search  (leave empty for any)", rendered)

    def test_tostring_of_a_real_slixmpp_stanza(self):
        import slixmpp
        from app.jmp_flow import tostring
        stanza = slixmpp.Message()
        stanza["to"] = "cheogram.com"
        stanza["body"] = "hi"
        rendered = tostring(stanza)
        self.assertIn("cheogram.com", rendered)
        self.assertIn("hi", rendered)

    def test_tostring_falls_back_when_nothing_can_render_it(self):
        from app.jmp_flow import tostring
        class Unrenderable:
            xml = 42
        self.assertIn("unprintable int", tostring(Unrenderable()))

    def test_validate_script_rejects_a_non_object_step(self):
        from app.jmp_flow import validate_script
        with self.assertRaises(ValueError) as ctx:
            validate_script(["not-a-step"])
        self.assertIn("step 0 is not an object", str(ctx.exception))

    def test_validate_script_requires_field_on_pick_and_capture(self):
        from app.jmp_flow import validate_script
        with self.assertRaises(ValueError) as ctx:
            validate_script([{"pick": {}}])
        self.assertIn("pick needs a field", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_script([{"capture_field": {}}])
        self.assertIn("capture_field needs a field", str(ctx.exception))

    def test_pick_option_without_options_raises(self):
        from app.jmp_flow import pick_option
        form = {"fields": [{"var": "tel", "options": []}]}
        with self.assertRaises(Exception) as ctx:
            pick_option(form, "tel")
        self.assertIn("no options", str(ctx.exception))

    def test_a_forbidden_activation_method_is_a_payment_step(self):
        from app.jmp_flow import looks_like_payment_step
        self.assertTrue(looks_like_payment_step({"submit": {"activation_method": "bch"}}))
        self.assertFalse(looks_like_payment_step({"submit": {"activation_method": "bitcoin"}}))

    def test_an_oob_url_on_the_wire_is_caught_by_the_guarantee(self):
        from app.jmp_flow import assert_no_payment_emitted
        transcript = Transcript(None, echo=False)
        transcript.add("OUT", "OOB URL: https://pay.jmp.chat/x/bitcoin")
        with self.assertRaises(AssertionError) as ctx:
            assert_no_payment_emitted(transcript)
        self.assertIn("out-of-band payment URL", str(ctx.exception))


class TestScriptRunnerEdges(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_and_stop_after_are_honoured(self):
        from app.jmp_flow import run_script

        class Client:
            def __init__(self):
                self.transcript = Transcript(None, echo=False)
                self.cursor = __import__("app.jmp_flow", fromlist=["Cursor"]).Cursor()
                self.calls = []

            async def run_command(self, node, **kw):
                self.calls.append(kw.get("action"))
                return (None, None, "s")

            def send_text(self, body, to=None):
                self.calls.append("text")

        client = Client()
        steps = [
            {"sleep": 0, "label": "wait-a-moment"},
            {"text": "hello", "label": "say-hi"},
            {"text": "never", "label": "unreached"},
        ]
        await run_script(client, steps, stop_after="say-hi")
        self.assertEqual(client.calls, ["text"])
        self.assertIn("STOP after say-hi", client.transcript.records[-1]["summary"])
