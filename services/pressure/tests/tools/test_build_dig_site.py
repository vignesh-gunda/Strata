import ast
import json
import subprocess
import sys

import pytest

from blackbox.client import BlackBoxClient, BlackBoxError
from tools.build_dig_site import REMOVED_FUNCTIONS, SECRET_STRINGS, BuildError, build, partial_source


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("build")
    out, sealed = root / "dig-site", root / "sealed"
    (out).mkdir()
    (out / "README.md").write_text("keep me")
    result = build(out, seed=42, mutations=2, games=6, run_id="r-test", sealed_dir=sealed)
    return out, sealed, result


def test_every_artifact_is_written(site):
    out, _, _ = site
    for path in ("bin/pressure.pyz", "docs/pressure-manual-v1.md", "source/engine.py", "source/state.py",
                 "traces/seed-corpus/traces.jsonl", "traces/seed-corpus/games.jsonl", "MANIFEST.json"):
        assert (out / path).is_file(), path
    assert (out / "README.md").read_text() == "keep me"  # the builder never deletes what it did not make


def test_manifest_lists_four_kinds_with_correct_hashes(site):
    import hashlib
    out, _, _ = site
    manifest = json.loads((out / "MANIFEST.json").read_text())
    kinds = {artifact["kind"]: artifact["trust_tier"] for artifact in manifest["artifacts"]}
    assert kinds == {"binary": "observed", "runtime_traces": "observed",
                     "partial_code": "read_from_code", "documentation": "documented"}
    for artifact in manifest["artifacts"]:
        for entry in artifact["files"]:
            assert hashlib.sha256((out / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_manifest_and_dig_site_never_name_the_mutations(site):
    out, _, result = site
    planted = result["sealed"]
    mutations = json.loads(planted.read_text())["mutations"]
    for path in out.rglob("*"):
        if path.is_file() and path.suffix != ".pyz":
            text = path.read_text(encoding="utf-8")
            assert not any(name in text for name in mutations), path


def test_binary_has_no_readable_secrets_and_no_source(site):
    import zipfile
    out, _, _ = site
    binary = out / "bin" / "pressure.pyz"
    raw = binary.read_bytes()
    assert not any(name.encode() in raw for name in SECRET_STRINGS)
    names = zipfile.ZipFile(binary).namelist()
    assert [name for name in names if name.endswith(".py")] == ["__main__.py"]
    assert "blackbox/client.pyc" not in names and "blackbox/http_server.pyc" not in names


def test_binary_matches_service_and_cannot_reveal(site):
    out, _, _ = site
    binary = out / "bin" / "pressure.pyz"
    with BlackBoxClient(seed=42, mutations=2) as service, BlackBoxClient(command=[sys.executable, str(binary)]) as compiled:
        script = service.random_script(5)
        assert compiled.new_game(3, script) == service.new_game(3, script)
        legal = service.legal_actions("g-0001")
        assert compiled.legal_actions("g-0001") == legal
        assert compiled.act("g-0001", legal[0]) == service.act("g-0001", legal[0])
        with pytest.raises(BlackBoxError) as error:
            compiled.reveal()
        assert error.value.kind == "forbidden"


def test_binary_ignores_command_line_flags(site):
    out, _, _ = site
    request = '{"id": 1, "method": "reveal"}\n'
    reply = subprocess.run([sys.executable, str(out / "bin" / "pressure.pyz"), "--allow-reveal", "--mutations", "0"],
                           input=request, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
    assert json.loads(reply.stdout)["error"]["kind"] == "forbidden"


def test_partial_source_loses_exactly_the_mutation_functions(site):
    out, _, _ = site
    tree = ast.parse((out / "source" / "engine.py").read_text())
    lost = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)
            and isinstance(node.body[0], ast.Raise) and len(node.body) == 1}
    assert lost == set(REMOVED_FUNCTIONS)
    assert "step" in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not (out / "source" / "rules.py").exists()


def test_partial_source_rejects_unknown_function():
    with pytest.raises(BuildError):
        partial_source("def kept():\n    return 1\n", ("missing",))


def test_seed_corpus_uses_neutral_scenario_ids(site):
    out, _, _ = site
    labels = {json.loads(line)["label"] for line in (out / "traces" / "seed-corpus" / "games.jsonl").open()}
    scenarios = {label for label in labels if label.startswith("scenario:")}
    assert scenarios and all(label.split(":")[1].startswith("s") for label in scenarios)
    manifest = json.loads((out / "traces" / "seed-corpus" / "manifest.json").read_text())
    assert "black_box_seed" not in manifest and "mutations" not in manifest


def test_sealed_file_records_the_planted_mutations(site):
    _, sealed, _ = site
    record = json.loads((sealed / "r-test.json").read_text())
    assert record["seed"] == 42 and len(record["mutations"]) == 2


@pytest.mark.parametrize("kwargs", [{"mutations": 7}, {"games": 0}, {"run_id": "../escape"}])
def test_invalid_build_arguments(tmp_path, kwargs):
    arguments = {"seed": 1, "mutations": 1, "games": 1, "run_id": "r-x"} | kwargs
    with pytest.raises(ValueError):
        build(tmp_path / "out", sealed_dir=tmp_path / "sealed", **arguments)
