"""T1 tests: the file jail, the binary runner and the agent tool surface."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from sandbox.main.binary import (BinaryCrashed, BinaryError, BinaryProcess,
                                 BinaryRequestError, BinaryTimeout)
from sandbox.main.digsite_fs import AccessDenied, DigSiteFS
from sandbox.main.metadata import binary_metadata
from sandbox.main.tools import AgentTools

REAL_DIG_SITE = Path(__file__).resolve().parents[3] / "dig-site"
needs_binary = pytest.mark.skipif(
    not (REAL_DIG_SITE / "bin" / "pressure.pyz").exists(),
    reason="dig-site not built (run services/pressure tools.build_dig_site)")


@pytest.fixture()
def site(tmp_path):
    """A small fake dig-site next to a 'services' folder the jail must not reach."""
    root = tmp_path / "dig-site"
    (root / "bin").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "bin" / "pressure.pyz").write_bytes(b"binary-bytes")
    (root / "docs" / "manual.md").write_text("hello manual")
    (root / "data.jsonl").write_text('{"a": 1}\n\n{"a": 2}\n')
    (root / "MANIFEST.json").write_text(json.dumps({"artifacts": []}))
    services = tmp_path / "services"
    services.mkdir()
    (services / "secret.txt").write_text("answer key")
    return root


# ---------- file jail ----------

def test_reads_allowed_files(site):
    fs = DigSiteFS(site)
    assert fs.read_text("docs/manual.md") == "hello manual"
    assert [r["a"] for r in fs.iter_jsonl("data.jsonl")] == [1, 2]
    assert fs.exists("docs/manual.md") and not fs.exists("docs/nope.md")


@pytest.mark.parametrize("path", [
    "../services/secret.txt",
    "docs/../../services/secret.txt",
    "..",
    "/etc/passwd",
    "",
    "docs/\x00x",
])
def test_escape_attempts_denied(site, path):
    with pytest.raises(AccessDenied):
        DigSiteFS(site).read_text(path)


def test_absolute_path_to_services_denied(site):
    with pytest.raises(AccessDenied):
        DigSiteFS(site).read_text(str(site.parent / "services" / "secret.txt"))


@pytest.mark.parametrize("path", ["bin/pressure.pyz", "./bin/pressure.pyz", "BIN/pressure.pyz",
                                  "docs/../bin/pressure.pyz", "bin"])
def test_bin_is_never_readable(site, path):
    fs = DigSiteFS(site)
    with pytest.raises(AccessDenied):
        fs.read_text(path)
    assert not fs.exists(path)


def test_symlink_out_of_jail_denied(site):
    os.symlink(site.parent / "services" / "secret.txt", site / "docs" / "link.txt")
    with pytest.raises(AccessDenied):
        DigSiteFS(site).read_text("docs/link.txt")


def test_symlink_into_bin_denied(site):
    os.symlink(site / "bin" / "pressure.pyz", site / "docs" / "sneaky")
    with pytest.raises(AccessDenied):
        DigSiteFS(site).read_text("docs/sneaky")


def test_listing_hides_bin(site):
    names = DigSiteFS(site).listdir()
    assert "bin" not in names and "docs" in names
    with pytest.raises(AccessDenied):
        DigSiteFS(site).listdir("bin")


def test_size_cap(site):
    (site / "big.txt").write_text("x" * 100)
    with pytest.raises(AccessDenied):
        DigSiteFS(site).read_text("big.txt", max_bytes=10)


# ---------- binary metadata ----------

def test_metadata_has_hash_and_size_but_no_bytes(site):
    meta = binary_metadata(DigSiteFS(site))
    assert meta["bytes"] == len(b"binary-bytes") and len(meta["sha256"]) == 64
    assert "binary-bytes" not in json.dumps(meta)


@needs_binary
def test_real_binary_metadata_matches_manifest():
    meta = binary_metadata(DigSiteFS(REAL_DIG_SITE))
    assert meta["matches_manifest"] is True


# ---------- process runner (generic JSON-lines programs) ----------

ECHO = ("import sys, json\n"
        "for line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        "    if r['method'] == 'boom':\n"
        "        print(json.dumps({'id': r['id'], 'ok': False, 'error': {'kind': 'illegal_action', 'message': 'no'}}), flush=True)\n"
        "    else:\n"
        "        print(json.dumps({'id': r['id'], 'ok': True, 'result': r['params']}), flush=True)\n")


def test_send_request_roundtrip_and_error_mapping():
    with BinaryProcess(command=[sys.executable, "-c", ECHO]) as proc:
        assert proc.send_request("echo", {"x": 1}) == {"x": 1}
        with pytest.raises(BinaryRequestError) as info:
            proc.send_request("boom")
        assert info.value.kind == "illegal_action"
        assert proc.send_request("echo", {"y": 2}) == {"y": 2}  # still usable


def test_timeout_kills_and_restart_recovers():
    slow = "import time; time.sleep(30)"
    proc = BinaryProcess(command=[sys.executable, "-c", slow], timeout=0.3)
    proc.start()
    with pytest.raises(BinaryTimeout):
        proc.send_request("x")
    assert not proc.alive
    proc._command = [sys.executable, "-c", ECHO]
    proc.restart()
    assert proc.send_request("echo", {"ok": True}) == {"ok": True}
    proc.close()


def test_crash_is_reported():
    proc = BinaryProcess(command=[sys.executable, "-c", "pass"], timeout=2)
    proc.start()
    with pytest.raises(BinaryCrashed):
        proc.send_request("x")
    assert not proc.alive


def test_must_start_first():
    with pytest.raises(BinaryError):
        BinaryProcess(command=[sys.executable, "-c", ECHO]).send_request("x")


def test_child_environment_has_no_api_keys(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "should-not-leak")
    monkeypatch.setenv("SUBQ_API_KEY", "should-not-leak")
    prog = ("import sys, json, os\n"
            "for line in sys.stdin:\n"
            "    r = json.loads(line)\n"
            "    keys = [k for k in os.environ if 'API_KEY' in k]\n"
            "    print(json.dumps({'id': r['id'], 'ok': True, 'result': keys}), flush=True)\n")
    with BinaryProcess(command=[sys.executable, "-c", prog]) as proc:
        assert proc.send_request("env") == []


# ---------- the real binary ----------

@needs_binary
def test_real_binary_plays_a_full_game():
    fs = DigSiteFS(REAL_DIG_SITE)
    with BinaryProcess(fs) as box:
        game = box.new_game(3, box.random_script(7))
        game_id = game["game_id"]
        assert game["view"]["players"] == 3
        for _ in range(2000):
            view = box.view(game_id)
            if view["over"]:
                break
            actions = box.legal_actions(game_id)
            assert actions, "a live game always has a legal action"
            result = box.act(game_id, actions[0])
            assert "events" in result and "view" in result
        else:
            pytest.fail("game did not finish in 2000 steps")
        assert box.view(game_id)["over"] is True


@needs_binary
def test_real_binary_rejects_illegal_action_and_unknown_game():
    with BinaryProcess(DigSiteFS(REAL_DIG_SITE)) as box:
        game_id = box.new_game(2, box.random_script(1))["game_id"]
        with pytest.raises(BinaryRequestError) as info:
            box.act(game_id, {"type": "play", "card": "not-a-card"})
        assert info.value.kind in ("illegal_action", "invalid_request")
        with pytest.raises(BinaryRequestError) as info:
            box.view("g-9999")
        assert info.value.kind == "game_not_found"


# ---------- agent tool surface ----------

def test_tool_surface_cannot_open_binary_or_services(site):
    tools = AgentTools(DigSiteFS(site), BinaryProcess(command=[sys.executable, "-c", ECHO]))
    public = {n for n in dir(tools) if not n.startswith("_")}
    assert public == {"read_file", "list_dir", "binary_metadata", "start_binary",
                      "send_request", "close"}
    with pytest.raises(AccessDenied):
        tools.read_file("bin/pressure.pyz")
    with pytest.raises(AccessDenied):
        tools.read_file("../services/secret.txt")
    tools.start_binary()
    assert tools.send_request("echo", {"a": 1}) == {"a": 1}
    tools.close()
