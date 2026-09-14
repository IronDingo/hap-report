# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""tests/test_runner.py — unit tests for openHAP's runner logic.

Covers openhap.py (config access, stage DAG, state machine, template) and
hap_config.py (resolution order). Dependency-free and offline: run with
`python3 tests/test_runner.py` or `uv run tests/test_runner.py`. The test_*
functions are also plain enough to collect under pytest.
"""
from __future__ import annotations
import os
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
import openhap        # noqa: E402
import hap_config     # noqa: E402


@contextmanager
def env(**kw):
    """Temporarily set/clear env vars (value None = unset)."""
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@contextmanager
def clean_argv():
    """Neutralise sys.argv so hap_config's arg-1 fallback doesn't grab junk."""
    old = sys.argv
    sys.argv = [old[0] if old else "test"]
    try:
        yield
    finally:
        sys.argv = old


# ---- openhap.py: config access -------------------------------------------------
def test_get_and_unset():
    d = {"a": {"b": 1}, "e": ""}
    assert openhap.get(d, "a.b") == 1
    assert openhap.get(d, "a.missing", "dflt") == "dflt"
    assert openhap.get(d, "nope.deep") is None
    assert openhap.unset(d, "e")            # empty string = unset
    assert openhap.unset(d, "missing")
    assert not openhap.unset(d, "a.b")      # a real value is set


def test_missing_keys_and_env_override():
    with env(DXF_PATH=None, SPACES_CSV=None):
        assert openhap.missing_keys("audit", {}) == ["drawing.dxf"]
        assert "gbxml.spaces_csv" in openhap.missing_keys("gbxml", {})
    with env(DXF_PATH="/tmp/x.dxf"):
        assert openhap.missing_keys("audit", {}) == []   # env satisfies the key
    with env(SPACES_CSV="/tmp/s.csv"):
        assert "gbxml.spaces_csv" not in openhap.missing_keys("gbxml", {})


# ---- openhap.py: stage DAG + state machine ------------------------------------
def test_order_is_topological():
    idx = {s: i for i, s in enumerate(openhap.ORDER)}
    for stage, spec in openhap.STAGES.items():
        for pre in spec["prereqs"]:
            assert pre in openhap.STAGES, f"{stage} lists unknown prereq {pre}"
            assert idx[pre] < idx[stage], f"{pre} must come before {stage}"


def test_resolve_run_list_pulls_prereqs_in_order():
    assert openhap._resolve_run_list(["takeoff"]) == ["geometry", "takeoff"]
    assert openhap._resolve_run_list(["orient"]) == \
        ["inventory", "rooms", "windows", "north", "orient"]


def test_stage_outputs_dynamic():
    with env(HAP_EXTRACTS=None, HAP_GBXML_OUT=None):
        data = {"drawing": {"extracts": "ex"},
                "floors": {"blocks": {"L1": "b", "L2": "c"}}}
        names = [p.name for p in openhap.stage_outputs("inventory", data)]
        assert names == ["floor_L1.json", "floor_L2.json"]
        assert openhap.stage_outputs("gbxml", {"gbxml": {"out": "gbxml/b.xml"}})[0].name == "b.xml"


def test_stage_state_blocked_todo_done():
    with tempfile.TemporaryDirectory() as td, env(DXF_PATH=None, HAP_EXTRACTS=None):
        data = {"drawing": {"dxf": "/x.dxf", "extracts": td}}
        assert openhap.stage_state("audit", data)[0] == "todo"     # no output yet
        Path(td, "layer_audit.txt").write_text("x")
        assert openhap.stage_state("audit", data)[0] == "done"     # output present
        assert openhap.stage_state("audit", {})[0] == "blocked"    # drawing.dxf unset


# ---- openhap.py: template + helpers -------------------------------------------
def test_template_parses_and_has_sections():
    t = tomllib.loads(openhap.TEMPLATE)
    for sec in ("project", "drawing", "layers", "floors", "windows",
                "cw", "north", "seed", "site", "gbxml"):
        assert sec in t, f"template missing [{sec}]"


def test_insert_after_line():
    txt = "[floors.blocks]\nL1 = \"x\"\n"
    out = openhap._insert_after_line(txt, "[floors.blocks]", "# NEW\n")
    assert out == "[floors.blocks]\n# NEW\nL1 = \"x\"\n"


def test_network_hint_regex():
    R = openhap.NETWORK_HINT_RE
    assert R.search("× No solution found when resolving script dependencies:")
    assert R.search("error sending request for url (https://pypi.org/...)")
    assert not R.search("Traceback (most recent call last): KeyError: 'foo'")


# ---- hap_config.py: resolution order --------------------------------------
def _write_project(td: str) -> Path:
    real = Path(td, "real_toml.dxf"); real.write_text("dxf")
    toml = Path(td, "hap-project.toml")
    toml.write_text(
        '[drawing]\n'
        f'dxf = "{real}"\n'
        '[layers]\nlabel = "X-LBL"\n'
        '[floors.blocks]\nL1 = "blk"\n'
        '[[windows.include]]\npattern = "^M_Sl"\ncategory = "slide"\n'
    )
    return toml


def test_hapconfig_reads_toml():
    with tempfile.TemporaryDirectory() as td:
        toml = _write_project(td)
        with env(HAP_PROJECT=str(toml), DXF_PATH=None), clean_argv():
            c = hap_config.Cfg()
            assert c.get("layers.label") == "X-LBL"
            assert c.require("floors.blocks") == {"L1": "blk"}
            inc = c.window_includes()
            assert inc[0][1] == "slide" and inc[0][0].search("M_Sliding")
            assert c.dxf_path().name == "real_toml.dxf"   # from the toml


def test_hapconfig_env_overrides_toml():
    with tempfile.TemporaryDirectory() as td:
        toml = _write_project(td)
        envdxf = Path(td, "from_env.dxf"); envdxf.write_text("dxf")
        with env(HAP_PROJECT=str(toml), DXF_PATH=str(envdxf)), clean_argv():
            c = hap_config.Cfg()
            assert c.dxf_path() == envdxf                  # env wins over toml


def test_hapconfig_require_exits_when_missing():
    with tempfile.TemporaryDirectory() as td:
        toml = _write_project(td)
        with env(HAP_PROJECT=str(toml)), clean_argv():
            c = hap_config.Cfg()
            try:
                c.require("north.footprint_block")
                assert False, "require() should sys.exit on a missing key"
            except SystemExit:
                pass


def _tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    fails = 0
    for name, fn in _tests():
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    total = len(_tests())
    print(f"\n{total - fails}/{total} passed")
    sys.exit(1 if fails else 0)
