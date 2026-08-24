"""Step 3: the envelope contract between provider replies and service handles
(lib/message.py) — the format every file intersection in the system speaks.
"""

from __future__ import annotations

import json

from lib.message import ADDRESS, SENDER, Batch, Message, parse_reply
from agents.default import Agent, prompt_line
from providers.ping import Ping
from services.base_service import Service
from services.cron import Cron


def test_parse_reply_valid_array():
    raw = json.dumps([
        {"channel": "socket", "body": "hi"},
        {"channel": "email", "address": "a@b.c", "body": "yo"},
    ])
    msgs = parse_reply(raw)
    assert msgs == [
        Message(channel="socket", address="", body="hi"),
        Message(channel="email", address="a@b.c", body="yo"),
    ]


def test_parse_reply_tolerates_surrounding_prose():
    raw = 'Sure! Here you go:\n[{"channel": "socket", "body": "ok"}]\nDone.'
    msgs = parse_reply(raw)
    assert msgs == [Message(channel="socket", address="", body="ok")]


def test_parse_reply_empty_array_means_silence():
    assert parse_reply("[]") == []


def test_parse_reply_non_array_becomes_channel_less_message():
    msgs = parse_reply("pong")
    assert msgs == [Message(channel="", body="pong")]
    assert msgs[0].channel == "", "raw text carries no channel; routing is the harness's call"


def test_parse_reply_drops_malformed_items_keeps_valid():
    raw = json.dumps([
        {"channel": "socket", "body": "good"},
        {"body": "no channel"},
        {"channel": "socket", "body": 42},
        "not an object",
        {"channel": "socket", "address": 7, "body": "bad address"},
    ])
    msgs = parse_reply(raw)
    assert msgs == [Message(channel="socket", address="", body="good")]


def test_parse_reply_malformed_json_becomes_raw():
    raw = '[{"channel": "socket", "body": "oops"'
    msgs = parse_reply(raw)
    assert msgs[0].channel == ""
    assert msgs[0].body == raw


def test_to_wire_carries_only_address_and_body():
    msg = Message(channel="socket", address="x", body="b")
    assert msg.to_wire(ADDRESS) == {"address": "x", "body": "b"}
    assert msg.to_wire(SENDER) == {"from": "x", "body": "b"}


def test_from_wire_is_lenient():
    msg = Message.from_wire("socket", {}, ADDRESS)
    assert (msg.channel, msg.address, msg.body) == ("socket", "", "")


def test_from_wire_ignores_extra_fields():
    msg = Message.from_wire("email", {
        "from": "a@b.c", "body": "hello", "ts": "2026-08-15T10:00:00",
    }, SENDER)
    assert msg == Message("email", "a@b.c", "hello")


def test_prompt_line():
    msg = Message("email", "a@b.c", "hello")
    assert prompt_line(msg) == "[email] from a@b.c: hello"


def test_batch_channels_and_reply_to():
    batch = Batch([
        Message("socket", "a", "1"),
        Message("email", "x@y.z", "2"),
        Message("socket", "b", "3"),
    ])
    assert batch.channels == ["socket", "email"]
    assert batch.reply_to("socket") == "b"
    assert batch.reply_to("nope") == ""


class _Plain(Service):
    name = "plain_test"

    async def handle_in(self, msg):
        pass


def test_contract_lists_every_channel():
    cron = Cron("t-contract", {})
    plain = _Plain("t-contract", {})
    contract = Agent(Ping("t-contract", {}), [cron, plain]).contract
    assert json.dumps(cron.message_schema()) in contract
    assert json.dumps(plain.message_schema()) in contract
    assert "cancel <id>" in contract, "cron's BODY_HINT placeholder is its grammar"


def test_message_schema_field_order():
    class Addressed(Service):
        name = "addr_test"
        ADDRESS_HINT = "<addr>"

        async def handle_in(self, msg):
            pass

    schema = Addressed("t", {}).message_schema()
    assert list(schema) == ["channel", "address", "body"]
    assert _Plain("t", {}).message_schema() == {
        "channel": "plain_test", "body": "<text to send>",
    }
