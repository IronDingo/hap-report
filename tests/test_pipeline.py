# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""tests/test_pipeline.py — end-to-end known-answer test (chain A).

Generates the synthetic fixture, runs `openhap run takeoff`, and checks the
extracted room areas equal make_fixture's hand-computed w×h values (an
independent cross-check — the fixture never imports shapely).

Needs uv. If a dependency isn't cached and there's no network, the test
SKIPS rather than fails. Run: `uv run tests/test_pipeline.py`.
"""
from __future__ import annotations
import csv
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HAP = REPO / "openhap.py"
MAKE = REPO / "tests" / "make_fixture.py"
EXPECTED = {"Classroom 1": 40.0, "Office 2": 24.0, "WC 3": 6.0, "Laboratory 4": 60.0}

_NET = ("no solution", "not found in", "network", "error sending request", "offline")


class Skip(Exception):
    pass


def _uv_run(args: list[str]) -> subprocess.CompletedProcess:
    """uv run, offline first then online — mirrors how the runner behaves."""
    off = subprocess.run(["uv", "run", "--offline", *args],
                         capture_output=True, text=True)
    if off.returncode == 0:
        return off
    if any(h in off.stderr.lower() for h in _NET):
        return subprocess.run(["uv", "run", *args], capture_output=True, text=True)
    return off


def test_chain_a_known_answer():
    if shutil.which("uv") is None:
        raise Skip("uv not installed")
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        r = subprocess.run([sys.executable, str(HAP), "init", str(proj)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

        dxf = proj / "fixture.dxf"
        r = _uv_run([str(MAKE), str(dxf)])
        if r.returncode != 0 and any(h in (r.stderr + r.stdout).lower() for h in _NET):
            raise Skip("cannot obtain ezdxf (cold cache, no network)")
        assert dxf.exists(), r.stderr

        r = subprocess.run([sys.executable, str(HAP), "run", "takeoff"],
                           cwd=proj, env={**os.environ, "DXF_PATH": str(dxf)},
                           capture_output=True, text=True)
        blob = (r.stdout + r.stderr).lower()
        if r.returncode != 0 and any(h in blob for h in _NET):
            raise Skip("cannot obtain shapely (cold cache, no network)")
        assert r.returncode == 0, r.stdout + r.stderr

        rows = list(csv.DictReader((proj / "extracts" / "takeoff_rooms.csv").open()))
        got = {row["name"]: round(float(row["area_m2"]), 2) for row in rows}
        assert got == EXPECTED, f"{got} != {EXPECTED}"


if __name__ == "__main__":
    try:
        test_chain_a_known_answer()
        print("  PASS  test_chain_a_known_answer")
        print("\n1/1 passed")
        sys.exit(0)
    except Skip as e:
        print(f"  SKIP  test_chain_a_known_answer: {e}")
        print("\n0 passed, 1 skipped")
        sys.exit(0)
    except AssertionError as e:
        print(f"  FAIL  test_chain_a_known_answer: {e}")
        sys.exit(1)
