"""Step 2: config can be read, adjusted, and written back — Config accessors
over toml, and the .env read/write cycle the dashboard uses for key rotation.
All file I/O lands in the sandboxed home (guarded by test_10).
"""

from __future__ import annotations

from lib import settings
from lib.settings import Config, read_env, write_env


def test_config_reads_seeded_toml():
    cfg = Config()
    assert cfg.agent_id == "primary"
    assert cfg.provider == "claude"
    assert "web_chat" in cfg.services
    assert cfg.dashboard_port == 8767


def test_config_accessors_from_raw():
    cfg = Config(raw={
        "agents": {"main": {"provider": "gemini", "model": "m"}},
        "services": {
            "socket": {"enabled": True, "port": 1234},
            "email": {"enabled": False},
            "bogus": "not a dict",
        },
        "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 9999},
        "timezone": "America/New_York",
    })
    assert cfg.agent_id == "main"
    assert cfg.timezone == "America/New_York"
    assert cfg.provider == "gemini"
    assert cfg.agent["model"] == "m"
    assert set(cfg.services) == {"socket", "email"}, "non-dict sections dropped"
    assert cfg.enabled_services() == {"socket": {"enabled": True, "port": 1234}}
    assert (cfg.dashboard_enabled, cfg.dashboard_host, cfg.dashboard_port) == (
        True, "127.0.0.1", 9999,
    )


def test_config_defaults_when_empty():
    cfg = Config(raw={})
    assert cfg.agent_id == "primary"
    assert cfg.provider == "ping"
    assert cfg.services == {}
    assert cfg.dashboard_enabled is False
    assert cfg.dashboard_host == "0.0.0.0"
    assert cfg.timezone == "", "no timezone means whatever the host runs"


def test_config_missing_file_is_empty(monkeypatch, home):
    # settings binds CONFIG_TOML by value at import; patch its copy.
    monkeypatch.setattr(settings, "CONFIG_TOML", home / "nope.toml")
    cfg = Config()
    assert cfg.services == {}
    assert cfg.provider == "ping"


def test_env_write_read_round_trip(home):
    write_env({"ALPHA": "1", "BETA": "two"})
    assert read_env() == {"ALPHA": "1", "BETA": "two"}


def test_env_write_preserves_comments_and_order(home):
    (home / ".env").write_text(
        "# secrets live here\nALPHA=old\n\n# services\nBETA=b\nGAMMA=g\n"
    )
    write_env({"ALPHA": "new", "BETA": "b", "GAMMA": "g", "DELTA": "d"})
    text = (home / ".env").read_text()
    lines = [l for l in text.splitlines() if l]
    assert "# secrets live here" in lines
    assert "# services" in lines
    keys = [l.split("=")[0] for l in lines if "=" in l]
    assert keys == ["ALPHA", "BETA", "GAMMA", "DELTA"], "existing order kept, new appended"
    assert "ALPHA=new" in lines


def test_env_write_leaves_unnamed_keys_alone(home):
    """A one-key POST used to empty the file. Every other secret on the box
    has to survive it."""
    (home / ".env").write_text("KEEP=1\nOTHER=2\n")
    write_env({"NEW": "3"})
    assert read_env() == {"KEEP": "1", "OTHER": "2", "NEW": "3"}


def test_env_write_is_atomic(home):
    write_env({"A": "1"})
    assert not (home / ".env.tmp").exists()


def test_read_env_skips_junk_lines(home):
    (home / ".env").write_text("# comment\n\nnot a pair\nGOOD = spaced \n")
    assert read_env() == {"GOOD": "spaced"}
