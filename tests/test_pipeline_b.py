# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""tests/test_pipeline_b.py — end-to-end known-answer tests for chain B, the
window takeoff, north mapping, and window orientation.

Builds the fixture_b DXF (a floor block with room texts + window inserts, plus a
footprint and a landmark), runs `openhap run seed orient` (which pulls in
inventory → rooms → seed → windows → north → orient), and checks every result
against make_fixture_b's construction-fixed answers.

Needs uv. SKIPS (not fails) if a dependency isn't cached and there's no network.
Run: `uv run tests/test_pipeline_b.py`.
"""
from __future__ import annotations
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HAP = REPO / "openhap.py"
MAKEB = REPO / "tests" / "make_fixture_b.py"
_NET = ("no solution", "not found in", "network", "error sending request", "offline")

TOML = """\
[project]
name = "Fixture B"
[drawing]
dxf = "{dxf}"
extracts = "extracts"
[layers]
label = "A-AREA-IDEN"
[floors]
in_scope = ["L1"]
[floors.blocks]
L1 = "FLOOR_L1"
[[windows.include]]
pattern = "^WIN_SLIDE"
category = "slide"
[[windows.include]]
pattern = "^WIN_HINGE"
category = "hinge"
[north]
footprint_block = "FOOTPRINT"
landmark_block = "SITE"
landmark_quadrant = ["N", "W"]
"""


class Skip(Exception):
    pass


def _uv_run(args: list[str]) -> subprocess.CompletedProcess:
    off = subprocess.run(["uv", "run", "--offline", *args], capture_output=True, text=True)
    if off.returncode == 0:
        return off
    if any(h in off.stderr.lower() for h in _NET):
        return subprocess.run(["uv", "run", *args], capture_output=True, text=True)
    return off


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_chain_b_windows_north_orient():
    if shutil.which("uv") is None:
        raise Skip("uv not installed")
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        proj.mkdir()
        dxf = proj / "fixture_b.dxf"

        r = _uv_run([str(MAKEB), str(dxf)])
        if r.returncode != 0 and any(h in (r.stderr + r.stdout).lower() for h in _NET):
            raise Skip("cannot obtain ezdxf (cold cache, no network)")
        assert dxf.exists(), r.stderr
        (proj / "hap-project.toml").write_text(TOML.format(dxf=dxf))

        r = subprocess.run([sys.executable, str(HAP), "run", "seed", "orient"],
                           cwd=proj, capture_output=True, text=True)
        blob = (r.stdout + r.stderr).lower()
        if r.returncode != 0 and any(h in blob for h in _NET):
            raise Skip("cannot obtain shapely (cold cache, no network)")
        assert r.returncode == 0, r.stdout + r.stderr
        ex = proj / "extracts"

        # --- chain B: seed with ASHRAE 62.1 rates + matched areas ---
        seed = {row["space_type"]: (float(row["area_m2"]),
                                    int(row["density_ppl_per_100m2"]),
                                    float(row["oa_cfm_per_person"]),
                                    float(row["oa_cfm_per_m2"]))
                for row in _rows(ex / "hap_seed.csv")}
        assert seed["classroom"] == (40.0, 35, 10.0, 0.65), seed
        assert seed["office"] == (24.0, 5, 5.0, 0.30), seed
        assert seed["wc"] == (6.0, 0, 0.0, 0.0), seed
        assert seed["lab"] == (60.0, 10, 10.0, 0.90), seed

        # --- windows: 2 slide @1.8 m² → Classroom 1, 1 hinge @1.2 m² → Office 2 ---
        wins = sorted((row["class"], round(float(row["area_m2"]), 2), row["assigned_room"])
                      for row in _rows(ex / "window_takeoff_v2.csv"))
        assert wins == sorted([("slide", 1.8, "Classroom 1"),
                               ("slide", 1.8, "Classroom 1"),
                               ("hinge", 1.2, "Office 2")]), wins

        # --- north: landmark NW of footprint centre → N = +Y (math 90°) ---
        nm = json.loads((ex / "north_mapping.json").read_text())
        assert nm["north_world_math_deg"] == 90.0, nm["north_world_math_deg"]
        assert nm["north_label_in_world"] == "+Y", nm["north_label_in_world"]

        # --- orient: rot-0 windows read "N", the rot-90 window reads "W" ---
        ori = sorted((row["room"], row["compass"])
                     for row in _rows(ex / "window_orientation_per_window.csv"))
        assert ori == sorted([("Classroom 1", "N"),
                              ("Classroom 1", "N"),
                              ("Office 2", "W")]), ori


if __name__ == "__main__":
    try:
        test_chain_b_windows_north_orient()
        print("  PASS  test_chain_b_windows_north_orient")
        print("\n1/1 passed")
        sys.exit(0)
    except Skip as e:
        print(f"  SKIP  test_chain_b_windows_north_orient: {e}")
        print("\n0 passed, 1 skipped")
        sys.exit(0)
    except AssertionError as e:
        print(f"  FAIL  test_chain_b_windows_north_orient: {e}")
        sys.exit(1)
