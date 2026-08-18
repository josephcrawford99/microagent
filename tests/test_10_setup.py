"""Step 1: the home directory layout — the ground every other test stands on.
The first test is the sandbox tripwire: if it fails, the suite is pointed at
the user's real home and nothing else can be trusted.
"""

from __future__ import annotations

import stat

from lib import paths
from lib.handle import Handle, describe_tree
from lib.message import Message


def test_home_is_sandboxed(home, repo):
    assert paths.HOME == home
    assert paths.HOME != repo / ".microagent"
    assert paths.CONFIG_ENV == home / ".env", (
        "CONFIG_ENV fell back to the repo .env — config tests would clobber real secrets"
    )


def test_home_layout(home):
    for d in (paths.STATE_DIR, paths.SPACE_DIR, paths.RUN_DIR):
        assert d.is_dir()
    defaults = paths.REPO_DIR / "src" / "defaults"
    assert paths.CONFIG_TOML.read_bytes() == (defaults / "config.default.toml").read_bytes()
    assert paths.SOUL_PATH.read_bytes() == (defaults / "soul.default.md").read_bytes()


def test_ensure_home_wipes_run_but_preserves_config(home):
    junk_file = paths.RUN_DIR / "junk"
    junk_dir = paths.RUN_DIR / "junkdir"
    junk_file.write_text("x")
    junk_dir.mkdir()
    (junk_dir / "nested").write_text("y")

    sentinel = "\n# test sentinel: ensure_home must not re-seed an existing config\n"
    paths.CONFIG_TOML.write_text(paths.CONFIG_TOML.read_text() + sentinel)

    paths.ensure_home()

    assert paths.RUN_DIR.is_dir()
    assert not junk_file.exists()
    assert not junk_dir.exists()
    assert sentinel in paths.CONFIG_TOML.read_text()
    assert paths.SOUL_PATH.exists()


def test_handle_dir_creates_real_fifos():
    handle = Handle("setup_test")
    for fifo in (handle.in_path, handle.out_path):
        st = fifo.lstat()
        assert stat.S_ISFIFO(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o600
    assert stat.S_IMODE(handle.path.lstat().st_mode) == 0o700
    assert not handle.log_path.exists(), "log is created lazily, not at boot"


def test_second_handle_attaches_to_same_fifos():
    first = Handle("setup_test")
    inode = first.in_path.lstat().st_ino
    second = Handle("setup_test")
    assert second.in_path.lstat().st_ino == inode, (
        "re-constructing a Handle must not replace live FIFOs"
    )


def test_describe_tree_reports_handles_and_log_tail():
    handle = Handle("setup_test")
    handle.append_log({"from": "x", "body": "hello tree"})

    tree = describe_tree()
    entry = next(e for e in tree if e["name"] == "setup_test")
    by_name = {h["name"]: h for h in entry["handles"]}
    assert by_name["in"]["type"] == "fifo"
    assert by_name["out"]["type"] == "fifo"
    assert by_name["log"]["type"] == "file"
    assert any("hello tree" in line for line in by_name["log"]["tail"])


def test_write_without_reader_drops_not_raises():
    handle = Handle("setup_test_noreader")
    assert handle.write("out", Message("setup_test_noreader", "", "into the void")) is False


def test_snapshot_is_atomic_replace():
    handle = Handle("setup_test")
    handle.snapshot("status", "v1\n")
    handle.snapshot("status", "v2\n")
    assert (handle.path / "status").read_text() == "v2\n"
    assert not (handle.path / ".status.tmp").exists()
