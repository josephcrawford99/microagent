"""Step 6: the dashboard's HTTP surface against a live server on an ephemeral
port. /api/restart (os._exit) and /api/update (git reset --hard) are never
called — they would kill pytest / rewrite the working tree.
"""

from __future__ import annotations

import json
import shutil

import pytest

from dashboard.server import DashboardServer
from lib import paths
from lib.handle import Handle
from lib.settings import Config, read_env

from helpers import http_request

CF = {"CF-Connecting-IP": "1.2.3.4"}


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "t-owner")
    shutil.rmtree(paths.RUN_DIR / "web_chat", ignore_errors=True)
    Handle("web_chat")
    config = Config(raw={
        "agents": {"primary": {"provider": "ping"}},
        "dashboard": {"enabled": True},
        "services": {"web_chat": {"enabled": True}, "telegram": {"enabled": False}},
    })
    srv = DashboardServer(config, ("127.0.0.1", 0))
    srv.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_healthz_needs_no_auth(server):
    status, _, body = http_request(server, "GET", "/healthz", headers=CF)
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_lan_requests_are_trusted(server):
    status, _, body = http_request(server, "GET", "/api/bootstrap")
    assert status == 200
    payload = json.loads(body)
    assert payload["agent"]["provider"] == "ping"
    names = {s["name"]: s for s in payload["services"]}
    assert names["web_chat"]["enabled"] is True
    assert names["telegram"]["required_env"] == ["TELEGRAM_BOT_TOKEN"]
    assert isinstance(payload["env"], dict)
    assert "[agents.primary]" in payload["config_toml"]


def test_cloudflare_requests_need_a_token(server):
    status, _, _ = http_request(server, "GET", "/api/bootstrap", headers=CF)
    assert status == 401

    status, _, _ = http_request(
        server, "GET", "/api/bootstrap",
        headers=CF | {"Authorization": "Bearer wrong"},
    )
    assert status == 401

    status, _, body = http_request(
        server, "GET", "/api/bootstrap",
        headers=CF | {"Authorization": "Bearer t-owner"},
    )
    assert status == 200
    assert json.loads(body)["agent"]["provider"] == "ping"


def test_login_sets_cookie(server):
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    status, headers, _ = http_request(server, "POST", "/login", body="token=t-owner", headers=form)
    assert status == 302
    assert headers.get("Set-Cookie", "").startswith("dash_token=t-owner")

    status, _, body = http_request(server, "POST", "/login", body="token=nope", headers=form)
    assert status == 200
    assert b"invalid token" in body


def test_handles_endpoint_reflects_run_tree(server):
    status, _, body = http_request(server, "GET", "/api/handles")
    assert status == 200
    tree = json.loads(body)
    assert any(entry["name"] == "web_chat" for entry in tree)


def test_chat_send_and_poll_round_trip(server):
    status, _, body = http_request(server, "POST", "/api/chat/send", body={"body": "hi agent"})
    assert status == 200

    chat = Handle("web_chat")
    assert any(m.get("body") == "hi agent" for m in chat.read_log())

    # An agent reply is a log line with an `address` key (delivery via `in`).
    chat.append_log({"address": "", "body": "hi user"})

    status, _, body = http_request(server, "GET", "/api/chat/poll?after=0")
    payload = json.loads(body)
    roles = {(m["role"], m["body"]) for m in payload["messages"]}
    assert ("user", "hi agent") in roles
    assert ("agent", "hi user") in roles
    assert payload["offset"] > 0

    # Polling from the returned offset yields nothing new.
    status, _, body = http_request(
        server, "GET", f"/api/chat/poll?after={payload['offset']}"
    )
    assert json.loads(body)["messages"] == []


def test_owner_env_write_lands_in_sandboxed_env(server, home):
    status, _, body = http_request(
        server, "POST", "/api/env",
        body={"entries": [{"key": "ROTATED_KEY", "value": "fresh"}, {"key": "", "value": "skipped"}]},
    )
    assert status == 200
    assert read_env().get("ROTATED_KEY") == "fresh"


def test_space_serving_and_traversal_guard(server):
    status, _, body = http_request(server, "GET", "/space/")
    assert status == 200
    assert b"empty" in body.lower()

    (paths.SPACE_DIR / "index.html").write_text("<h1>agent was here</h1>")
    try:
        status, _, body = http_request(server, "GET", "/space/")
        assert status == 200
        assert b"agent was here" in body

        status, _, _ = http_request(server, "GET", "/space/../config.toml")
        assert status == 404
    finally:
        (paths.SPACE_DIR / "index.html").unlink()


def test_unknown_routes_404(server):
    assert http_request(server, "GET", "/api/nope")[0] == 404
    assert http_request(server, "POST", "/api/nope")[0] == 404
