"""Offline tests for the ported XEP-0050 driver — no network, no slixmpp.

The first seven classes are the 37 tests of the original tool
(`cashu-sms/tools/test_xmpp_provision.py` @ soveng-archive `3a179dc`), ported
1:1 with only the import path changed — same class names, same test names, same
assertions — so this file is a proof of the port rather than a fresh claim.
`python -m pytest tests/test_jmp_flow.py -k 'not Jmp'` collects exactly those.

The `*Jmp*` classes after them are new and cover what did NOT exist before: the
JMP flow constant, the no-payment guarantee, the number's session-scoped
caveat, and the two live entry points (driven against a fake bot).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from app.jmp_flow import (
    Cursor,
    ExpectFailed,
    ExpectTimeout,
    JMP_BOT,
    JMP_FUNDING_STEPS,
    JMP_REGISTER_NODE,
    NUMBER_CAVEAT,
    Transcript,
    assert_no_payment_emitted,
    build_submit,
    cached_payload_with_probe,
    drive_funding_live,
    field_value,
    find_field,
    form_text,
    funding_facts_from_transcript,
    looks_like_payment_step,
    now_iso,
    parse_form,
    pick_option,
    probe_status_live,
    read_only_probe,
    run_script,
    selected_number,
    tostring,
    validate_script,
)

# A real XEP-0004 form as the JMP bot sends it (shape taken from sgx-jmp's
# forms/registration/activate.rb + tn_list.rb).
ACTIVATE_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Activate JMP</title>
  <instructions>You've selected +14165550123 as your JMP number.
To activate your account, you can either deposit $15.00 to your balance or enter your referral code if you have one.</instructions>
  <field var="FORM_TYPE" type="hidden"><value>http://jabber.org/protocol/commands</value></field>
  <field var="activation_method" type="list-single" label="Activate using">
    <option label="Credit Card"><value>credit_card</value></option>
    <option label="Bitcoin"><value>bitcoin</value></option>
    <option label="Bitcoin Cash"><value>bch</value></option>
    <option label="Referral Code"><value>code</value></option>
    <option label="Mail or Interac e-Transfer"><value>mail</value></option>
  </field>
  <field var="plan_name" type="list-single" label="What currency should your account balance be in?">
    <option label="Canadian Dollars"><value>CAD</value></option>
    <option label="United States Dollars"><value>USD</value></option>
  </field>
</x>"""

TN_LIST_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Choose a number</title>
  <field var="tel" type="list-single" label="Available numbers">
    <option label="+14165550123 (Toronto, ON)"><value>+14165550123</value></option>
    <option label="+12125550124 (New York, NY)"><value>+12125550124</value></option>
  </field>
</x>"""


def _form(xml: str):
    return parse_form(ET.fromstring(xml))


# --------------------------------------------------------------------------
# 1. the ported 37 (unchanged apart from the import path)
# --------------------------------------------------------------------------
class TestCursorOrdering(unittest.TestCase):
    """The engine must not let a later expectation be satisfied out of order."""

    def test_consume_advances_and_enforces_order(self):
        c = Cursor()
        for m in ["welcome", "choose a number", "payment menu"]:
            c.feed(m)
        self.assertEqual(c.consume("choose a number"), "choose a number")
        self.assertIsNone(c.consume("welcome"))
        self.assertEqual(c.consume("payment menu"), "payment menu")
        self.assertIsNone(c.consume("payment menu"))

    def test_skipped_messages_are_reported(self):
        c = Cursor()
        for m in ["noise one", "noise two", "the prompt"]:
            c.feed(m)
        i = c.find("the prompt")
        self.assertEqual(c.skipped(i), ["noise one", "noise two"])

    def test_regex_and_multiline(self):
        c = Cursor()
        c.feed("Activate JMP\nActivate using:\n  Credit Card\n  Bitcoin")
        self.assertIsNotNone(c.consume(r"Activate using"))
        c2 = Cursor()
        c2.feed("Balance: $0.00")
        self.assertIsNotNone(c2.consume(r"balance:\s*\$?\d+\.\d\d"))

    def test_pending_reflects_unconsumed(self):
        c = Cursor()
        c.feed("a")
        c.feed("b")
        c.consume("a")
        self.assertEqual(c.pending(), ["b"])


class TestCursorTimeout(unittest.TestCase):
    def test_offline_wait_for_raises_immediately_with_context(self):
        c = Cursor()
        c.feed("something else")
        with self.assertRaises(ExpectTimeout) as ctx:
            asyncio.run(c.wait_for("never appears", timeout=0.1))
        self.assertIn("never appears", str(ctx.exception))

    def test_online_wait_for_times_out_and_reports_pending(self):
        c = Cursor(system=lambda: True)
        c.feed("only this arrived")

        async def go():
            return await c.wait_for("never appears", timeout=0.2)

        with self.assertRaises(ExpectTimeout) as ctx:
            asyncio.run(go())
        self.assertIn("only this arrived", str(ctx.exception))

    def test_wait_for_returns_when_message_arrives_later(self):
        c = Cursor(system=lambda: True)

        async def go():
            task = asyncio.ensure_future(c.wait_for("late prompt", timeout=2))

            async def feed_later():
                await asyncio.sleep(0.05)
                c.feed("late prompt")

            await feed_later()
            return await task

        self.assertEqual(asyncio.run(go()), "late prompt")


class TestFormParsing(unittest.TestCase):
    def setUp(self):
        self.form = parse_form(ET.fromstring(ACTIVATE_FORM_XML))

    def test_fields_and_options(self):
        self.assertEqual(self.form["title"], "Activate JMP")
        self.assertIn("deposit $15.00", self.form["instructions"])
        method = find_field(self.form, "activation_method")
        self.assertEqual(method["type"], "list-single")
        self.assertEqual(method["label"], "Activate using")
        self.assertEqual([o["value"] for o in method["options"]],
                         ["credit_card", "bitcoin", "bch", "code", "mail"])
        self.assertEqual([o["label"] for o in method["options"]][1], "Bitcoin")

    def test_form_text_is_the_prompt_verbatim(self):
        text = form_text(self.form)
        self.assertIn("Activate JMP", text)
        self.assertIn("[activation_method] Activate using", text)
        self.assertIn("bitcoin=Bitcoin", text)

    def test_hidden_fields_are_hidden_in_rendering(self):
        self.assertNotIn("FORM_TYPE", form_text(self.form))

    def test_non_form_returns_none(self):
        self.assertIsNone(parse_form(None))
        self.assertIsNone(parse_form(ET.fromstring("<body>hello</body>")))

    def test_tn_list_offers_real_numbers(self):
        form = parse_form(ET.fromstring(TN_LIST_FORM_XML))
        self.assertEqual(find_field(form, "tel")["values"], [])
        self.assertEqual(find_field(form, "tel")["options"][0]["value"], "+14165550123")


class TestSubmitBuilding(unittest.TestCase):
    def test_submit_carries_requested_fields(self):
        base = parse_form(ET.fromstring(ACTIVATE_FORM_XML))
        x = build_submit({"activation_method": "bitcoin", "plan_name": "USD"}, base=base)
        parsed = parse_form(x)
        values = {f["var"]: f["values"] for f in parsed["fields"]}
        self.assertEqual(values["activation_method"], ["bitcoin"])
        self.assertEqual(values["plan_name"], ["USD"])
        self.assertEqual(values["FORM_TYPE"], ["http://jabber.org/protocol/commands"])

    def test_submit_without_base_omits_form_type(self):
        x = build_submit({"q": "416"})
        parsed = parse_form(x)
        self.assertEqual([f["var"] for f in parsed["fields"]], ["q"])
        self.assertEqual(x.get("type"), "submit")

    def test_list_value_is_repeated(self):
        x = build_submit({"tags": ["a", "b"]})
        parsed = parse_form(x)
        self.assertEqual(parsed["fields"][0]["values"], ["a", "b"])
        self.assertIn("<value>a</value>", tostring(x))


class TestScriptValidation(unittest.TestCase):
    def test_valid_script(self):
        steps = [
            {"text": "register jmp.chat"},
            {"expect": "Choose", "timeout": 30},
            {"command": {"node": "jabber:iq:register"}},
            {"submit": {"activation_method": "bitcoin"}, "action": "next"},
            {"action": "prev"},
            {"note": "why"},
            {"sleep": 1},
        ]
        self.assertEqual(validate_script(steps), steps)

    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            validate_script({"text": "hi"})

    def test_rejects_unknown_step(self):
        with self.assertRaises(ValueError) as ctx:
            validate_script([{"texxxt": "hi"}])
        self.assertIn("unknown step", str(ctx.exception))

    def test_rejects_command_without_node(self):
        with self.assertRaises(ValueError):
            validate_script([{"command": {"action": "execute"}}])

    def test_rejects_empty_step(self):
        with self.assertRaises(ValueError):
            validate_script([{}])


class FakeClient:
    """Stand-in for XMPPProvisioner: records what the runner asked it to do."""

    def __init__(self, forms=None):
        self.transcript = Transcript(None, echo=False)
        self.cursor = Cursor()
        self.calls = []
        self.forms = list(forms) if forms else []
        self.next_form = self.forms.pop(0) if self.forms else None

    async def run_command(self, node, to=None, action="execute", fields=None,
                          sessionid=None, record_form=True, base_form=None, timeout=30):
        self.calls.append({"node": node, "action": action, "fields": fields,
                           "sessionid": sessionid})
        new_session = "sess-1" if action == "execute" else sessionid
        form = self.next_form
        self.next_form = self.forms.pop(0) if self.forms else None
        return (None, form, new_session)

    def send_text(self, body, to=None):
        self.calls.append({"text": body, "to": to})


class TestScriptRunner(unittest.IsolatedAsyncioTestCase):
    async def test_runner_sequences_and_passes_session(self):
        client = FakeClient()
        steps = [
            {"text": "register jmp.chat", "to": "cheogram.com"},
            {"command": {"node": "jabber:iq:register", "to": "cheogram.com"}},
            {"submit": {"activation_method": "bitcoin"}, "action": "next"},
            {"action": "prev"},
        ]
        await run_script(client, steps)
        self.assertEqual(client.calls[0], {"text": "register jmp.chat", "to": "cheogram.com"})
        self.assertEqual(client.calls[1]["action"], "execute")
        self.assertIsNone(client.calls[1]["sessionid"])
        self.assertEqual(client.calls[2]["sessionid"], "sess-1")
        self.assertEqual(client.calls[2]["fields"], {"activation_method": "bitcoin"})
        self.assertEqual(client.calls[3]["action"], "prev")
        self.assertEqual(client.calls[3]["sessionid"], "sess-1")

    async def test_runner_expect_capture_and_stop_before(self):
        client = FakeClient()
        client.cursor.feed("Bitcoin address: bc1qexample")
        steps = [
            {"expect": r"address:\s*(\S+)", "capture": "btc_address", "label": "addr"},
            {"submit": {"activation_method": "credit_card"}, "label": "pay-by-card"},
        ]
        captured, _ = await run_script(client, steps, stop_before="pay-by-card")
        self.assertEqual(captured["btc_address"], "Bitcoin address: bc1qexample")
        self.assertEqual(client.calls, [])

    async def test_runner_capture_uses_matching_message_only(self):
        client = FakeClient()
        client.cursor.feed("noise")
        client.cursor.feed("Bitcoin address: bc1qexample")
        captured, _ = await run_script(client, [{"expect": r"address:\s*\S+", "capture": "addr"}])
        self.assertEqual(captured["addr"], "Bitcoin address: bc1qexample")

    async def test_runner_expect_timeout_is_fatal_and_clear(self):
        client = FakeClient()
        with self.assertRaises(ExpectTimeout):
            await run_script(client, [{"expect": "never", "timeout": 0.1}])


class TestTranscript(unittest.TestCase):
    def test_records_both_directions_and_redacts(self):
        t = Transcript(None, echo=False, redact=["hunter2"])
        t.add("OUT", "iq set with password hunter2", raw="<x>hunter2</x>")
        t.add("IN", "hello")
        self.assertEqual(t.texts(), ["hello"])
        self.assertNotIn("hunter2", json.dumps(t.records))
        self.assertIn("redacted", json.dumps(t.records))

    def test_writes_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "transcript.txt")
            t = Transcript(path, echo=False)
            t.add("IN", "bot said something")
            with open(path) as fh:
                blob = fh.read()
            self.assertIn("bot said something", blob)
            self.assertIn("IN", blob)


class TestOptionPicking(unittest.TestCase):
    """Picking an option out of a bot-supplied list (numbers, payment menus)."""

    def setUp(self):
        self.form = parse_form(ET.fromstring(TN_LIST_FORM_XML))

    def test_pick_first_by_default(self):
        self.assertEqual(pick_option(self.form, "tel"), "+14165550123")

    def test_pick_by_index(self):
        self.assertEqual(pick_option(self.form, "tel", index=1), "+12125550124")

    def test_pick_by_regex_over_value_and_label(self):
        self.assertEqual(pick_option(self.form, "tel", match=r"New York"), "+12125550124")
        self.assertEqual(pick_option(self.form, "tel", match=r"\+1416"), "+14165550123")

    def test_pick_missing_field_is_clear(self):
        with self.assertRaises(ExpectFailed) as ctx:
            pick_option(self.form, "nope")
        self.assertIn("nope", str(ctx.exception))

    def test_pick_out_of_range_is_clear(self):
        with self.assertRaises(ExpectFailed):
            pick_option(self.form, "tel", index=9)

    def test_pick_no_options(self):
        form = parse_form(ET.fromstring(ACTIVATE_FORM_XML))
        with self.assertRaises(ExpectFailed):
            pick_option(form, "activation_method", match="paypal")

    def test_field_value_captures_bot_supplied_data(self):
        form = parse_form(ET.fromstring(
            '<x xmlns="jabber:x:data" type="result">'
            '<field var="btc_addresses"><value>bc1qexampledepositaddress</value></field>'
            '<field var="amount"><value>15.000000</value></field></x>'))
        self.assertEqual(field_value(form, "btc_addresses"), "bc1qexampledepositaddress")
        self.assertEqual(field_value(form, "amount"), "15.000000")
        with self.assertRaises(ExpectFailed):
            field_value(form, "nothing")


class TestFlowSteps(unittest.IsolatedAsyncioTestCase):
    """pick / capture_field / expect_form steps against a fake client."""

    async def test_pick_submits_chosen_option_and_continues(self):
        client = FakeClient(forms=[_form(TN_LIST_FORM_XML)])
        steps = [
            {"command": {"node": "jabber:iq:register"}},
            {"pick": {"field": "tel", "match": "New York"}},
        ]
        await run_script(client, steps)
        self.assertEqual(client.calls[1]["fields"], {"tel": "+12125550124"})
        self.assertEqual(client.calls[1]["action"], "next")

    async def test_capture_field_records_value(self):
        client = FakeClient(forms=[_form(
            '<x xmlns="jabber:x:data" type="result">'
            '<field var="btc_addresses"><value>bc1qexample</value></field>'
            '<field var="amount"><value>15.000000</value></field></x>')])
        steps = [
            {"command": {"node": "n"}},
            {"capture_field": {"field": "btc_addresses", "as": "btc_address"}},
            {"capture_field": {"field": "amount", "as": "btc_amount"}},
        ]
        captured, _ = await run_script(client, steps)
        self.assertEqual(captured, {"btc_address": "bc1qexample", "btc_amount": "15.000000"})

    async def test_expect_form_matches_and_fails(self):
        client = FakeClient(forms=[_form(ACTIVATE_FORM_XML)])
        captured, _ = await run_script(
            client, [{"command": {"node": "n"}},
                     {"expect_form": r"Activate JMP.*Bitcoin"}])
        client2 = FakeClient(forms=[_form(ACTIVATE_FORM_XML)])
        with self.assertRaises(ExpectFailed):
            await run_script(client2, [{"command": {"node": "n"}},
                                       {"expect_form": "PayPal"}])

    async def test_stop_before_payment_menu_pays_nothing(self):
        client = FakeClient(forms=[_form(ACTIVATE_FORM_XML), _form(ACTIVATE_FORM_XML)])
        steps = [
            {"command": {"node": "n"}, "label": "start"},
            {"submit": {"activation_method": "bitcoin"}, "label": "pick-bitcoin"},
        ]
        await run_script(client, steps, stop_before="pick-bitcoin")
        self.assertEqual(len(client.calls), 1)


# --------------------------------------------------------------------------
# 2. what is new: the JMP flow, the no-payment guarantee, the live entry points
# --------------------------------------------------------------------------
PROVIDER_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Register with backend</title>
  <instructions>Choose a phone number provider</instructions>
  <field var="gateway-jid" type="list-single" label="Provider">
    <option label="jmp.chat"><value>jmp.chat</value></option>
    <option label="Vonage SGX"><value>vonage</value></option>
  </field>
</x>"""

SEARCH_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Search Telephone Numbers</title>
  <field var="q" type="text-single" label="Area code (like 810)"/>
  <field var="http://jabber.org/protocol/commands#actions" type="list-single" label="Action">
    <option label="Feeling Lucky"><value>feelinglucky</value></option>
  </field>
</x>"""

NUMBER_LIST_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Choose a number</title>
  <instructions>Please choose one of the following numbers</instructions>
  <field var="tel" type="list-single" label="Telephone Number">
    <option label="(810) 258-3253 (RANKIN, MI)"><value>+18102583253</value></option>
    <option label="(810) 258-3279 (RANKIN, MI)"><value>+18102583279</value></option>
  </field>
</x>"""

#: the payment menu, verbatim in shape from the live capture: it names the
#: selected number, the $20 minimum, and offers five activation methods.
PAYMENT_MENU_FORM_XML = """<x xmlns="jabber:x:data" type="form">
  <title>Activate JMP</title>
  <instructions>You've selected (810) 258-3253 (RANKIN, MI) as your JMP number.
To activate your account, you can either deposit $20.00 to your balance or enter your referral code if you have one.
After payment is complete, your number will be activated for inbound calls and texts.</instructions>
  <field var="FORM_TYPE" type="hidden"><value>http://jabber.org/protocol/commands</value></field>
  <field var="activation_method" type="list-single" label="Activate Using">
    <option label="Credit Card"><value>credit_card</value></option>
    <option label="Bitcoin"><value>bitcoin</value></option>
    <option label="Bitcoin Cash"><value>bch</value></option>
    <option label="Referral Code"><value>code</value></option>
    <option label="Mail or Interac e-Transfer"><value>mail</value></option>
  </field>
  <field var="plan_name" type="list-single" label="What Currency Should Your Account Balance Be In?">
    <option label="United States Dollars"><value>USD</value></option>
  </field>
</x>"""

BTC_ADDRESS_FORM_XML = """<x xmlns="jabber:x:data" type="result">
  <title>Bitcoin Address</title>
  <field var="btc_addresses"><value>bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk</value></field>
  <field var="amount"><value>0.000244</value></field>
</x>"""


class FakeBotClient:
    """A fake cheogram.com: serves the five real forms, records every action.

    `client_factory`-shaped, so `drive_funding_live` / `probe_status_live` run
    the real code path offline. `calls` is what the driver actually asked for —
    that is where "no payment action was ever emitted" is asserted from.
    """

    def __init__(self, jid, password, transcript, bot=None, forms=None):
        self.jid, self.password, self.transcript = jid, password, transcript
        self.bot = bot or JMP_BOT
        self.calls = []
        self.forms = [_form(xml) for xml in (forms or [
            PROVIDER_FORM_XML, SEARCH_FORM_XML, NUMBER_LIST_FORM_XML,
            PAYMENT_MENU_FORM_XML, BTC_ADDRESS_FORM_XML])]
        self.connected = False

    async def connect(self, timeout=30):
        self.connected = True
        return self

    async def close(self):
        self.connected = False

    def roster_jids(self):
        return ["cheogram.com"]

    async def run_command(self, node, to=None, action="execute", fields=None,
                          sessionid=None, record_form=True, base_form=None, timeout=30):
        self.calls.append({"node": node, "action": action, "fields": fields,
                           "sessionid": sessionid, "to": to or self.bot})
        form = self.forms.pop(0) if self.forms else None
        if form is not None:
            self.transcript.add("IN", form_text(form))
        return (None, form, "sess-1" if action == "execute" else sessionid)

    def send_text(self, body, to=None):
        self.calls.append({"text": body, "to": to or self.bot})
        self.transcript.add("OUT", "message -> %s\n%s" % (to or self.bot, body))

    async def discover_commands(self, to=None, timeout=30):
        self.calls.append({"discover": to or self.bot})
        return [(JMP_REGISTER_NODE, "Register with backend")]

    async def describe_command(self, node, to=None, timeout=30):
        self.calls.append({"describe": node})
        return _form(PROVIDER_FORM_XML)


class TestJmpFlowDefinition(unittest.TestCase):
    """The shipped flow: valid, targeted, and payment-free by construction."""

    def test_the_flow_is_a_valid_script(self):
        self.assertEqual(validate_script(JMP_FUNDING_STEPS), JMP_FUNDING_STEPS)

    def test_the_flow_targets_the_cheogram_register_command(self):
        first_command = next(s["command"] for s in JMP_FUNDING_STEPS if "command" in s)
        self.assertEqual(first_command["node"], JMP_REGISTER_NODE)
        self.assertEqual(JMP_BOT, "cheogram.com")

    def test_no_step_in_the_flow_is_payment_shaped(self):
        for step in JMP_FUNDING_STEPS:
            self.assertFalse(looks_like_payment_step(step), step)

    def test_the_only_activation_method_the_flow_selects_is_bitcoin(self):
        methods = [s["submit"].get("activation_method") for s in JMP_FUNDING_STEPS
                   if "submit" in s and "activation_method" in s["submit"]]
        self.assertEqual(methods, ["bitcoin"])

    def test_the_flow_ends_at_the_funding_facts(self):
        labels = [s.get("label") for s in JMP_FUNDING_STEPS if "label" in s]
        self.assertEqual(labels[-1], "capture-btc-amount")
        self.assertIn("capture-btc-address", labels)

    def test_looks_like_payment_step_flags_card_shapes_only(self):
        self.assertTrue(looks_like_payment_step({"submit": {"activation_method": "credit_card"}}))
        self.assertTrue(looks_like_payment_step({"submit": {"card-number": "4111"}}))
        self.assertTrue(looks_like_payment_step({"note": "open the braintree page"}))
        self.assertFalse(looks_like_payment_step({"submit": {"activation_method": "bitcoin"}}))
        self.assertFalse(looks_like_payment_step({"capture_field": {"field": "btc_addresses"}}))


class TestNoPaymentGuarantee(unittest.TestCase):
    """`assert_no_payment_emitted` must be able to FAIL — otherwise it proves
    nothing. Each test feeds it a payment shape and requires a refusal."""

    def test_a_card_step_in_the_step_list_is_caught(self):
        poisoned = JMP_FUNDING_STEPS + [
            {"submit": {"activation_method": "credit_card"}, "label": "pay"}]
        with self.assertRaises(AssertionError) as ctx:
            assert_no_payment_emitted(Transcript(None, echo=False), poisoned)
        self.assertIn("payment action", str(ctx.exception))

    def test_a_card_submit_on_the_wire_is_caught(self):
        transcript = Transcript(None, echo=False)
        transcript.add("OUT", 'command node=x action=next\nfields={"activation_method": "credit_card"}')
        with self.assertRaises(AssertionError) as ctx:
            assert_no_payment_emitted(transcript)
        self.assertIn("wire", str(ctx.exception))

    def test_a_card_field_on_the_wire_is_caught(self):
        transcript = Transcript(None, echo=False)
        transcript.add("OUT", "fields={\"card-number\": \"4111111111111111\"}")
        with self.assertRaises(AssertionError):
            assert_no_payment_emitted(transcript)

    def test_an_oob_payment_url_is_caught(self):
        transcript = Transcript(None, echo=False)
        transcript.add("OUT", "OOB URL: https://pay.jmp.chat/x/credit_cards?amount=20")
        with self.assertRaises(AssertionError):
            assert_no_payment_emitted(transcript)

    def test_a_clean_transcript_passes(self):
        transcript = Transcript(None, echo=False)
        transcript.add("OUT", 'command node=jabber:iq:register action=execute -> cheogram.com')
        transcript.add("OUT", 'fields={"activation_method": "bitcoin", "plan_name": "USD"}')
        transcript.add("IN", "Bitcoin Address: bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk")
        assert_no_payment_emitted(transcript)

    def test_the_bot_offering_a_card_option_is_not_our_action(self):
        """The menu legitimately lists credit_card; only OUR stanzas matter."""
        transcript = Transcript(None, echo=False)
        transcript.add("IN", form_text(_form(PAYMENT_MENU_FORM_XML)))
        assert_no_payment_emitted(transcript)


class TestSelectedNumberAndFacts(unittest.TestCase):
    def test_selected_number_is_read_out_of_the_bot_line(self):
        transcript = Transcript(None, echo=False)
        transcript.add("IN", "You've selected (810) 258-3233 (RANKIN, MI) as your JMP number.")
        self.assertEqual(selected_number(transcript), "(810) 258-3233")

    def test_selected_number_handles_the_other_observed_session(self):
        transcript = Transcript(None, echo=False)
        transcript.add("IN", "You've selected (810) 258-3253 (RANKIN, MI) as your JMP number.")
        self.assertEqual(selected_number(transcript), "(810) 258-3253")

    def test_selected_number_is_none_when_the_bot_never_said_it(self):
        transcript = Transcript(None, echo=False)
        transcript.add("IN", "Please choose one of the following numbers")
        self.assertIsNone(selected_number(transcript))

    def test_facts_mark_unobserved_fields_instead_of_inventing_them(self):
        transcript = Transcript(None, echo=False)
        facts = funding_facts_from_transcript(transcript, {})
        self.assertIsNone(facts["btc_address"])
        self.assertIsNone(facts["number"])
        self.assertIn("btc_address", facts["unobserved"])
        self.assertIn("number", facts["unobserved"])
        self.assertEqual(facts["source"], "live-flow")

    def test_facts_read_the_usd_minimum_the_bot_states(self):
        transcript = Transcript(None, echo=False)
        transcript.add("IN", form_text(_form(PAYMENT_MENU_FORM_XML)))
        facts = funding_facts_from_transcript(transcript, {"btc_address": "bc1q", "btc_amount": "0.000244"})
        self.assertEqual(facts["amount_usd_min"], "20.00")
        self.assertEqual(facts["btc_address"], "bc1q")
        self.assertEqual(facts["amount_btc"], "0.000244")
        self.assertEqual(facts["activation_state"], "unfunded")
        self.assertEqual(facts["as_of"][-1], "Z")

    def test_facts_flip_to_live_only_when_the_bot_activated_the_number(self):
        transcript = Transcript(None, echo=False)
        transcript.add("IN", "Your JMP account has been activated as (810) 258-3253")
        facts = funding_facts_from_transcript(transcript)
        self.assertEqual(facts["number_state"], "live")
        self.assertEqual(facts["activation_state"], "active")

    def test_every_live_payload_carries_the_session_scope_caveat(self):
        facts = funding_facts_from_transcript(Transcript(None, echo=False))
        self.assertEqual(facts["number_note"], NUMBER_CAVEAT)
        self.assertIn("Session-scoped reservation", NUMBER_CAVEAT)
        self.assertIn("not ownership", NUMBER_CAVEAT)


class TestLiveEntryPoints(unittest.IsolatedAsyncioTestCase):
    """`drive_funding_live` / `probe_status_live` against a fake cheogram.com."""

    async def test_the_drive_reaches_the_funding_step_and_pays_nothing(self):
        captured_clients = []

        def factory(jid, password, transcript, bot=None):
            client = FakeBotClient(jid, password, transcript, bot=bot)
            captured_clients.append(client)
            return client

        facts = await drive_funding_live("hermes@example.test", "pw",
                                         client_factory=factory)
        client = captured_clients[0]
        self.assertTrue(client.connected is False)          # closed afterwards
        # the facts came out of the bot's own words
        self.assertEqual(facts["number"], "(810) 258-3253")
        self.assertEqual(facts["btc_address"], "bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk")
        self.assertEqual(facts["amount_btc"], "0.000244")
        self.assertEqual(facts["amount_usd_min"], "20.00")
        self.assertEqual(facts["source"], "live-flow")
        self.assertEqual(facts["number_state"], "reserved")
        # and it really drove: 5 command round-trips, no chat, no card
        self.assertEqual(len(client.calls), 5)
        self.assertEqual([c["action"] for c in client.calls],
                         ["execute", "next", "next", "next", "next"])
        self.assertEqual(client.calls[-1]["fields"],
                         {"activation_method": "bitcoin", "plan_name": "USD"})
        blob = json.dumps(client.calls).lower()
        for forbidden in ("credit_card", "card-number", "cvv", "braintree"):
            self.assertNotIn(forbidden, blob)

    async def test_the_drive_never_submits_a_payment_form(self):
        """The last action is the *bitcoin* selection; nothing follows it."""
        client = FakeBotClient("j@example.test", "pw", Transcript(None, echo=False))
        await drive_funding_live("j@example.test", "pw",
                                 client_factory=lambda *a, **k: client)
        self.assertEqual(client.forms, [])
        submitted = [c for c in client.calls if c.get("fields")]
        self.assertEqual(len(submitted), 4)      # provider, search, pick, bitcoin
        self.assertNotIn("credit_card", json.dumps(submitted))

    async def test_the_read_only_probe_sends_only_discovery_reads(self):
        client = FakeBotClient("j@example.test", "pw", Transcript(None, echo=False))
        probe = await read_only_probe(client, bot=JMP_BOT)
        self.assertEqual([list(c)[0] for c in client.calls], ["discover", "describe"])
        self.assertEqual(probe["commands"],
                         [{"node": JMP_REGISTER_NODE, "name": "Register with backend"}])
        self.assertIn("Choose a phone number provider", probe["register_form"])
        self.assertEqual(probe["roster"], ["cheogram.com"])

    async def test_the_probe_writes_nothing_to_the_transcript_but_gets(self):
        transcript = Transcript(None, echo=False)
        client = FakeBotClient("j@example.test", "pw", transcript)
        await read_only_probe(client, bot=JMP_BOT)
        for line in transcript.outbound():
            lowered = line.lower()
            self.assertNotIn("action=next", lowered)
            self.assertNotIn("action=execute", lowered)
            self.assertNotIn("message", lowered)

    async def test_probe_status_live_keeps_the_facts_cached_and_shows_the_probe(self):
        payload = await probe_status_live(
            "j@example.test", "pw",
            client_factory=lambda jid, pw, transcript, bot=None:
                FakeBotClient(jid, pw, transcript, bot=bot))
        self.assertEqual(payload["source"], "cached")
        self.assertEqual(payload["btc_address"], "bc1qnq4mm4sh2vm8mjh2fa7yxcqn2ymz637mudpkfk")
        self.assertEqual(payload["as_of"], "2026-09-26T21:20:00Z")
        self.assertEqual(payload["read_only_probe"]["roster_size"], 1)
        self.assertIn("Read-only", payload["read_only_probe"]["note"])

    async def test_the_drive_redacts_the_password_from_its_transcript(self):
        transcript_path = tempfile.mktemp(suffix=".txt")
        try:
            await drive_funding_live(
                "hermes@example.test", "s3cr3t-password",
                transcript_path=transcript_path, redact=["s3cr3t-password"],
                client_factory=lambda jid, pw, transcript, bot=None:
                    FakeBotClient(jid, pw, transcript, bot=bot))
            blob = open(transcript_path).read()
            self.assertNotIn("s3cr3t-password", blob)
        finally:
            os.unlink(transcript_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
