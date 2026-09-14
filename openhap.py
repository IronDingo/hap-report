# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""openhap.py — runner for the openHAP pipeline.

Wraps the tools/ scripts behind five commands:

  openhap init [dir]                  scaffold a project (hap-project.toml + dirs)
  openhap doctor                      preflight: uv, dep cache, drawing, config
  openhap status                      project map, rendered from the filesystem
  openhap run <stage...> | --all      dependency-aware execution, skips done stages
  openhap suggest floors|windows      draft [floors.blocks] / [[windows.include]]
                                  candidates from the DXF (--write = insert as
                                  commented lines into hap-project.toml)

Stage names:  audit geometry takeoff inventory rooms seed windows cw north
orient gbxml.  `--all` runs everything except gbxml (it needs the hand-curated
spaces register — run it explicitly).

Done-ness is by output files on disk: a stage whose outputs all exist is "done"
and `openhap run` skips it. There is no freshness tracking — pass --force to rerun
after you change the drawing or the config.

Offline: every tool is launched `uv run --offline` first (warm cache = zero
network); on a cache miss uv is retried online. Suits unstable connections.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent
TOOLS = REPO / "tools"

# --------------------------------------------------------------------------
# Stage table — the TOOLS.md run-order table, machine-readable.
# keys: config keys that must be set before the stage can run.
# --------------------------------------------------------------------------
STAGES: dict[str, dict] = {
    "audit": dict(file="01_layer_audit.py", prereqs=[],
                  outputs=["layer_audit.txt"], keys=["drawing.dxf"]),
    "geometry": dict(file="02_extract_geometry.py", prereqs=[],
                     outputs=["polylines.json", "inserts.json", "texts.json"],
                     keys=["drawing.dxf"]),
    "takeoff": dict(file="03_build_takeoff.py", prereqs=["geometry"],
                    outputs=["takeoff_rooms.csv"], keys=[]),
    "inventory": dict(file="01_floor_inventory.py", prereqs=[],
                      outputs="floor_json",  # dynamic: floor_<label>.json
                      keys=["drawing.dxf", "floors.blocks"]),
    "rooms": dict(file="04_layers_and_rooms.py", prereqs=["inventory"],
                  outputs=["room_inventory.csv", "layers.csv", "a_area_presence.json"],
                  keys=["floors.blocks"]),
    "seed": dict(file="05_hap_seed.py", prereqs=["rooms"],
                 outputs=["hap_seed.csv"], keys=["floors.in_scope"]),
    "windows": dict(file="07d_count_windows_v2.py", prereqs=["rooms"],
                    outputs=["window_takeoff_v2.csv", "window_summary_v2.csv",
                             "window_summary_v2.md"],
                    keys=["drawing.dxf", "floors.blocks", "windows.include"]),
    "cw": dict(file="08_cw_assemblies.py", prereqs=["rooms"],
               outputs=["cw_assemblies.csv", "cw_assemblies.md", "cw_panes.csv"],
               keys=["drawing.dxf", "floors.blocks", "windows.include"]),
    "north": dict(file="09_north_mapping.py", prereqs=[],
                  outputs=["north_mapping.json"],
                  keys=["drawing.dxf", "north.footprint_block", "north.landmark_block"]),
    "orient": dict(file="10_window_orientation.py", prereqs=["windows", "north"],
                   outputs=["window_orientation_per_window.csv",
                            "window_orientation_per_room.csv",
                            "window_orientation_summary.md"],
                   keys=[]),
    "gbxml": dict(file="generate_hap_gbxml.py", prereqs=[],
                  outputs="gbxml_out",  # dynamic: [gbxml].out
                  keys=["gbxml.spaces_csv", "gbxml.floor_to_floor_m",
                        "gbxml.storey_z", "site.latitude", "site.longitude",
                        "site.elevation_m", "gbxml.materials", "gbxml.layers",
                        "gbxml.constructions", "gbxml.person_sensible_w",
                        "gbxml.person_latent_w"]),
}
ORDER = list(STAGES)  # canonical topological order
CHAINS = [("A", ["audit", "geometry", "takeoff"]),
          ("B", ["inventory", "rooms", "seed"]),
          ("W", ["windows", "cw", "north", "orient"]),
          ("X", ["gbxml"])]

NETWORK_HINT_RE = re.compile(
    r"no solution|not found in (the )?cache|offline mode|failed to (fetch|"
    r"download|resolve)|error sending request|could not connect|connection|"
    r"network|dns error|operation timed out", re.I)

TEMPLATE = r'''# hap-project.toml — single source of truth for one project.
# Commented key = unset: the owning tool will stop and tell you.
# Env vars (DXF_PATH, HAP_EXTRACTS, SPACES_CSV, HAP_GBXML_OUT) still override.

[project]
name = ""                       # e.g. "Riverside School"
site = ""

[drawing]
dxf = ""                        # /abs/path/to/drawing.dxf
extracts = "extracts"

[layers]                        # AIA NCS defaults — change if the drawing differs
room     = "A-AREA"
label    = "A-AREA-IDEN"
prefixes = ["A-", "S-", "E-", "P-", "C-"]

[floors]
in_scope = []                   # e.g. ["L1", "L2", "L3"]

[floors.blocks]                 # label = "floor block name in the DXF"
# L1 = ""                       # `openhap suggest floors` drafts these for you

[windows]
room_radius_m = 8.0             # label-to-window spatial-join radius
max_depth     = 8               # nested-INSERT recursion limit
dim_pattern   = '(\d{2,5})\s*[xX]\s*(\d{2,5})'
exclude = ['Door', 'Mullion', 'Frame\b', 'Louver', '\bTag\b|\bSchedule\b|\bSymbol\b']
# Room filter for the spatial join; unset = each tool's built-in default.
# room_name_pattern = '(?i)^(Classroom|Office|Bureau|Bath\.?|WC|Lobby|Laboratory|Library)\s*\d*'

# One block per window family; category ∈ slide | hinge | cw_panel | cw_awning.
# slide/hinge parse dims from the block NAME; cw_* measure block geometry.
# [[windows.include]]           # `openhap suggest windows` drafts these
# pattern  = '^M_Sliding'
# category = "slide"

[cw]                            # curtain-wall assembly clustering (stage `cw`)
lateral_tol_mm = 400            # same-wall lateral tolerance
gap_tol_mm     = 7500           # end-to-end pane gap that still joins an assembly
angle_bin_deg  = 15
room_radius_m  = 15.0           # assembly centroid → room-label join radius

[north]
# footprint_block = ""          # block whose long axis is a cardinal axis
# landmark_block  = ""          # block searched for the landmark (may equal footprint)
landmark_pattern  = 'chainlink|fence|court|playground|canopy|parking'
landmark_quadrant = ["N", "W"]  # landmark's compass quadrant: [N|S, E|W]

[seed]
area_match_radius_m = 8.0       # room label ↔ "X m²" tag distance tolerance

[site]
# latitude    = 0.0             # decimal degrees
# longitude   = 0.0
# elevation_m = 0

[gbxml]
spaces_csv    = ""              # hand-curated spaces register (stage `gbxml` input)
out           = "gbxml/building.xml"
gap_m         = 2.0
north_azimuth = "auto"          # "auto" = derive from extracts/north_mapping.json
# floor_to_floor_m  = 0.0
# person_sensible_w = 0         # W/person, ASHRAE for the dominant activity
# person_latent_w   = 0

[gbxml.storey_z]                # label = base elevation (m)
# L1 = 0.0

# Envelope — from your locked design values. No "typical" numbers.
# Construction ids are canonical, keep them:
#   cons-extwall, cons-intwall, cons-roof, cons-intfloor, cons-slab
# [[gbxml.materials]]
# id = "mat-wall"
# name = ""
# thickness_m = 0.0
# conductivity_w_mk = 0.0
# density_kg_m3 = 0
# specific_heat_j_kgk = 0
# [[gbxml.layers]]
# id = "layer-extwall"
# materials = ["mat-wall"]
# [[gbxml.constructions]]
# id = "cons-extwall"
# name = ""
# u_w_m2k = 0.0
# ext_solar_abs = 0.0
# layer = "layer-extwall"
'''


# --------------------------------------------------------------------------
# config plumbing
# --------------------------------------------------------------------------
def find_config() -> Path | None:
    env = os.environ.get("HAP_PROJECT")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    for d in [Path.cwd(), *Path.cwd().parents]:
        p = d / "hap-project.toml"
        if p.is_file():
            return p
    return None


def load_config() -> tuple[Path | None, dict]:
    p = find_config()
    if not p:
        return None, {}
    try:
        return p, tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{p}: TOML parse error — {e}")


def get(data: dict, dotted: str, default=None):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def unset(data: dict, dotted: str) -> bool:
    return get(data, dotted) in (None, "", [], {})


def dxf_path(data: dict) -> Path | None:
    raw = os.environ.get("DXF_PATH") or get(data, "drawing.dxf", "")
    return Path(raw).expanduser() if raw else None


def extracts_dir(data: dict) -> Path:
    raw = os.environ.get("HAP_EXTRACTS") or get(data, "drawing.extracts", "extracts")
    return Path(raw).expanduser()


def stage_outputs(name: str, data: dict) -> list[Path]:
    spec = STAGES[name]
    ext = extracts_dir(data)
    if spec["outputs"] == "floor_json":
        blocks = get(data, "floors.blocks", {}) or {}
        return [ext / f"floor_{label}.json" for label in blocks]
    if spec["outputs"] == "gbxml_out":
        raw = os.environ.get("HAP_GBXML_OUT") or get(data, "gbxml.out", "gbxml/building.xml")
        return [Path(raw).expanduser()]
    return [ext / f for f in spec["outputs"]]


def missing_keys(name: str, data: dict) -> list[str]:
    keys = list(STAGES[name]["keys"])
    if name == "gbxml" and get(data, "gbxml.north_azimuth", "auto") != "auto":
        pass  # explicit azimuth: nothing extra needed
    out = [k for k in keys if unset(data, k)]
    # drawing.dxf may come from the environment instead
    if "drawing.dxf" in out and os.environ.get("DXF_PATH"):
        out.remove("drawing.dxf")
    if "gbxml.spaces_csv" in out and os.environ.get("SPACES_CSV"):
        out.remove("gbxml.spaces_csv")
    return out


def stage_state(name: str, data: dict) -> tuple[str, str]:
    """Return (state, detail). state ∈ blocked | done | todo.

    done = every output file present on disk. No freshness tracking — after a
    drawing/config change, rerun with `openhap run <stage> --force`.
    """
    miss = missing_keys(name, data)
    if miss:
        return "blocked", ", ".join(miss)
    outs = stage_outputs(name, data)
    if outs and all(o.is_file() for o in outs):
        return "done", ""
    return "todo", ""


# --------------------------------------------------------------------------
# uv execution — offline first, online fallback
# --------------------------------------------------------------------------
def _dep_cached_offline(pkg: str) -> bool:
    """True if `pkg` resolves offline via the same PEP 723 path the tools use."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(f"# /// script\n# dependencies = [\"{pkg}\"]\n# ///\nimport {pkg}\n")
        tmp = f.name
    try:
        r = subprocess.run(["uv", "run", "--offline", tmp],
                           capture_output=True, text=True, timeout=180)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        os.unlink(tmp)


def run_tool(tool_file: str, cfg_path: Path | None, extra_args: list[str] | None = None,
             quiet: bool = False) -> int:
    tool = TOOLS / tool_file
    env = dict(os.environ)
    if cfg_path:
        env["HAP_PROJECT"] = str(cfg_path)
    args = extra_args or []

    with tempfile.TemporaryFile(mode="w+") as errf:
        rc = subprocess.run(
            ["uv", "run", "--offline", str(tool), *args],
            env=env, stderr=errf,
            stdout=subprocess.DEVNULL if quiet else None,
        ).returncode
        errf.seek(0)
        err = errf.read()

    if rc == 0:
        return 0
    if NETWORK_HINT_RE.search(err):
        print("  (dependency cache miss — retrying online)")
        return subprocess.run(["uv", "run", str(tool), *args], env=env).returncode
    if err.strip():
        print(err.rstrip(), file=sys.stderr)
    return rc


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    target = Path(args.dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    toml = target / "hap-project.toml"
    if toml.exists():
        sys.exit(f"{toml} already exists — not overwriting")
    toml.write_text(TEMPLATE)
    (target / "extracts").mkdir(exist_ok=True)
    (target / "gbxml").mkdir(exist_ok=True)
    print(f"Scaffolded {target.resolve()}/")
    print("  hap-project.toml   extracts/   gbxml/")
    print("\n→ next:")
    print("  1. set [drawing].dxf in hap-project.toml (DWG? convert with ODA File Converter first)")
    print("  2. openhap doctor")
    print("  3. openhap suggest floors --write   then uncomment/label the real floor blocks")
    print("     openhap suggest windows --write  then uncomment the real window families")
    print("  4. openhap run --all")
    return 0


def cmd_doctor(args) -> int:
    cfg_path, data = load_config()
    failures = 0

    uv = shutil.which("uv")
    if uv:
        v = subprocess.run(["uv", "--version"], capture_output=True, text=True).stdout.strip()
        print(f"✔ uv          {v}")
    else:
        print("✖ uv          not on PATH — install: https://docs.astral.sh/uv/")
        return 1

    if cfg_path:
        print(f"✔ config      {cfg_path}")
        unset_all: list[str] = []
        for st in ORDER:
            for k in missing_keys(st, data):
                if k not in unset_all:
                    unset_all.append(k)
        if unset_all:
            print(f"  ◦ unset keys: {', '.join(unset_all)}")
    else:
        print("✖ config      no hap-project.toml here — run `openhap init`")
        failures += 1

    print("… deps        probing uv cache (offline)", flush=True)
    # Probe with the SAME mechanism the tools use — a PEP 723 inline-dep script
    # resolved by `uv run --offline` — so the verdict matches real runs. `uv
    # --with` resolves differently and can mislead.
    ez = _dep_cached_offline("ezdxf")
    sh = _dep_cached_offline("shapely")
    if ez and sh:
        print("✔ deps        ezdxf + shapely cached — fully offline-capable")
    else:
        need = [p for p, ok in (("ezdxf", ez), ("shapely", sh)) if not ok]
        it = "it" if len(need) == 1 else "them"
        print(f"◦ deps        {', '.join(need)} not cached — the first `openhap run` "
              f"that needs {it} fetches once online, then stays offline")

    dxf = dxf_path(data)
    if not dxf:
        print("◦ drawing     [drawing].dxf unset")
    elif not dxf.exists():
        print(f"✖ drawing     {dxf} does not exist")
        failures += 1
    elif dxf.suffix.lower() == ".dwg":
        print(f"✖ drawing     {dxf.name} is a DWG — convert to DXF with ODA File "
              "Converter first (this is the one external step)")
        failures += 1
    else:
        print(f"… drawing     opening {dxf.name} ({dxf.stat().st_size >> 20} MB) — can take a while", flush=True)
        code = ("import ezdxf, sys, json\n"
                "d = ezdxf.readfile(sys.argv[1])\n"
                "print(json.dumps({'insunits': d.header.get('$INSUNITS', 0),"
                " 'layers': len(list(d.layers)), 'blocks': len(list(d.blocks))}))")
        p = subprocess.run(["uv", "run", "--offline", "--with", "ezdxf",
                            "python", "-c", code, str(dxf)],
                           capture_output=True, text=True, timeout=900)
        if p.returncode != 0 and NETWORK_HINT_RE.search(p.stderr):
            p = subprocess.run(["uv", "run", "--with", "ezdxf", "python", "-c", code, str(dxf)],
                               capture_output=True, text=True, timeout=900)
        if p.returncode == 0:
            info = json.loads(p.stdout.strip().splitlines()[-1])
            units = {0: "unspecified", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
            u = info["insunits"]
            note = "" if u == 4 else "  ⚠ chain B (floor blocks) assumes mm"
            print(f"✔ drawing     opens: INSUNITS={u} ({units.get(u, u)}), "
                  f"{info['layers']} layers, {info['blocks']} blocks{note}")
        else:
            print(f"✖ drawing     ezdxf cannot open it:\n{p.stderr.strip()[:400]}")
            failures += 1

    oda = shutil.which("ODAFileConverter")
    print(f"{'✔' if oda else '◦'} oda         {'found' if oda else 'not on PATH (only needed for DWG → DXF)'}")

    print("\nall good ✓" if failures == 0 else f"\n{failures} check(s) failed")
    return 1 if failures else 0


GLYPH = {"done": "✔", "todo": "○", "blocked": "⛔"}


def _counts(name: str, data: dict) -> str:
    ext = extracts_dir(data)
    count_file = {"takeoff": "takeoff_rooms.csv", "rooms": "room_inventory.csv",
                  "seed": "hap_seed.csv", "windows": "window_takeoff_v2.csv",
                  "cw": "cw_assemblies.csv", "orient": "window_orientation_per_room.csv"}
    if name in count_file:
        p = ext / count_file[name]
        if p.is_file():
            n = max(0, sum(1 for _ in p.open()) - 1)
            unit = {"takeoff": "rooms", "rooms": "rows", "seed": "spaces",
                    "windows": "windows", "cw": "assemblies", "orient": "rooms"}[name]
            return f" {n} {unit}"
    if name == "north":
        p = ext / "north_mapping.json"
        if p.is_file():
            try:
                j = json.loads(p.read_text())
                azi = (90.0 - float(j["north_world_math_deg"])) % 360.0
                return f" N={j['north_label_in_world']} azi={azi:.0f}°"
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    return ""


def cmd_status(args) -> int:
    cfg_path, data = load_config()
    if not cfg_path:
        sys.exit("no hap-project.toml here — run `openhap init` (or cd into a project)")

    dxf = dxf_path(data)
    if dxf and dxf.is_file():
        dxf_disp = f"{dxf.name} ({dxf.stat().st_size >> 20} MB)"
    else:
        dxf_disp = "no drawing set" if not dxf else f"{dxf.name} MISSING"
    name = get(data, "project.name") or cfg_path.parent.name
    print(f"HAP · {name} · {dxf_disp}")
    print(f"config {cfg_path}")

    states: dict[str, tuple[str, str]] = {s: stage_state(s, data) for s in ORDER}
    for chain, members in CHAINS:
        cells = []
        for s in members:
            st, detail = states[s]
            cell = f"{s} {GLYPH[st]}"
            if st == "done":
                cell += _counts(s, data)
            elif st == "blocked":
                cell += f" needs {detail}"
            cells.append(cell)
        print(f"  {chain}  " + "   ".join(cells))

    # → next: config gaps first, then runnable work, then the finish line
    nxt = None
    # Only push the drawing if a stage is actually blocked on it (a gbxml-only
    # project legitimately needs no drawing).
    if not dxf and any(st == "blocked" and "drawing.dxf" in det
                       for st, det in states.values()):
        nxt = "set [drawing].dxf in hap-project.toml"
    if not nxt:
        for s in ORDER:
            st, detail = states[s]
            if st == "blocked" and all(states[p][0] != "blocked" for p in STAGES[s]["prereqs"]):
                hint = ""
                if "floors.blocks" in detail:
                    hint = "  (openhap suggest floors --write)"
                elif "windows.include" in detail:
                    hint = "  (openhap suggest windows --write)"
                nxt = f"fill {detail} in hap-project.toml{hint}"
                break
    if not nxt:
        runnable = [s for s in ORDER if s != "gbxml"
                    and states[s][0] == "todo"
                    and all(states[p][0] == "done" for p in STAGES[s]["prereqs"])]
        if runnable:
            nxt = "openhap run " + " ".join(runnable)
    if not nxt:
        if states["gbxml"][0] == "done":
            nxt = "all stages done ✓"
        elif states["gbxml"][0] == "blocked":
            nxt = ("extracts done — curate the spaces register CSV, fill [gbxml]/"
                   "[site], then `openhap run gbxml`")
        else:
            nxt = "openhap run gbxml"
    print(f"  → next: {nxt}")
    return 0


def _resolve_run_list(requested: list[str]) -> list[str]:
    seen: list[str] = []

    def add(s: str):
        for p in STAGES[s]["prereqs"]:
            add(p)
        if s not in seen:
            seen.append(s)

    for s in requested:
        add(s)
    return sorted(seen, key=ORDER.index)


def cmd_run(args) -> int:
    cfg_path, data = load_config()
    if not cfg_path:
        sys.exit("no hap-project.toml here — run `openhap init` first")
    requested = ORDER[:-1] if args.all else args.stages
    if not requested:
        sys.exit("name stages (e.g. `openhap run takeoff`) or use --all")
    bad = [s for s in requested if s not in STAGES]
    if bad:
        sys.exit(f"unknown stage(s): {', '.join(bad)} — valid: {' '.join(ORDER)}")

    plan = _resolve_run_list(requested)
    done_ok: set[str] = {s for s in ORDER if stage_state(s, data)[0] == "done"}

    for s in plan:
        state, detail = stage_state(s, data)
        if state == "done" and not args.force:
            print(f"— {s}: outputs already exist, skipping (--force to rerun)")
            continue
        if state == "blocked":
            hint = ""
            if "floors.blocks" in detail:
                hint = " — try `openhap suggest floors --write`"
            elif "windows.include" in detail:
                hint = " — try `openhap suggest windows --write`"
            print(f"⛔ {s}: fill {detail} in {cfg_path.name}{hint}")
            if s in requested:
                return 1
            print(f"   (skipping {s} and anything depending on it)")
            continue
        miss_pre = [p for p in STAGES[s]["prereqs"] if p not in done_ok]
        if miss_pre:
            print(f"⛔ {s}: blocked by {', '.join(miss_pre)}")
            if s in requested:
                return 1
            continue

        print(f"▶ {s}  (uv run tools/{STAGES[s]['file']})")
        rc = run_tool(STAGES[s]["file"], cfg_path)
        if rc != 0:
            print(f"✖ {s} failed (exit {rc}) — stopping")
            return rc
        outs = stage_outputs(s, data)
        missing = [o for o in outs if not o.is_file()]
        if missing:
            print(f"✖ {s} exited 0 but did not produce: "
                  + ", ".join(str(m) for m in missing))
            return 1
        done_ok.add(s)
        print(f"✔ {s}")
    print("\ndone — `openhap status` for the map")
    return 0


def _insert_after_line(text: str, anchor: str, insert: str) -> str:
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip() == anchor:
            return "".join(lines[: i + 1]) + insert + "".join(lines[i + 1:])
    return text + ("\n" if not text.endswith("\n") else "") + anchor + "\n" + insert


def cmd_suggest(args) -> int:
    cfg_path, data = load_config()
    if not cfg_path:
        sys.exit("no hap-project.toml here — run `openhap init` first")
    rc = run_tool("_suggest.py", cfg_path, extra_args=[args.what])
    if rc != 0:
        return rc
    if not args.write:
        print("\n(re-run with --write to insert these as commented lines in hap-project.toml)")
        return 0

    jpath = extracts_dir(data) / f"_suggest_{args.what}.json"
    cands = json.loads(jpath.read_text())
    text = cfg_path.read_text()
    stamp = f"# ── openhap suggest {args.what} ({date.today()}) — uncomment & edit ──\n"
    n = 0
    if args.what == "floors":
        block = stamp
        for c in cands:
            if f'"{c["name"]}"' in text:
                continue
            block += (f'# L? = "{c["name"]}"'.ljust(46)
                      + f'# {c["msp_count"]}×, {c["w_m"]:.1f}×{c["h_m"]:.1f} m, '
                        f'{c["entities"]} entities\n')
            n += 1
        if n:
            text = _insert_after_line(text, "[floors.blocks]", block)
    else:
        block = "\n" + stamp
        for c in cands:
            pat = "^" + re.escape(c["name"])
            if pat in text:
                continue
            block += ("# [[windows.include]]\n"
                      f"# pattern  = '{pat}'\n"
                      f'# category = "{c["guess"]}"'.ljust(34)
                      + f'# {c["uses"]} uses — verify category\n')
            n += 1
        if n:
            text += block
    if n:
        cfg_path.write_text(text)
        print(f"\nInserted {n} commented candidate(s) into {cfg_path.name} — "
              "uncomment the real ones, fix labels/categories.")
    else:
        print("\nNothing new to insert (all candidates already present).")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="openhap", description="Runner for the openHAP pipeline.")
    ap.add_argument("-C", metavar="DIR", help="cd to project dir first")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="scaffold a project")
    p.add_argument("dir", nargs="?", default=".")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="preflight checks")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("status", help="live project map")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("run", help="run stages (with prerequisites)")
    p.add_argument("stages", nargs="*", metavar="stage",
                   help=f"one of: {' '.join(ORDER)}")
    p.add_argument("--all", action="store_true", help="every stage except gbxml")
    p.add_argument("--force", action="store_true", help="rerun even if outputs exist")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("suggest", help="draft config candidates from the DXF")
    p.add_argument("what", choices=["floors", "windows"])
    p.add_argument("--write", action="store_true",
                   help="insert candidates into hap-project.toml as comments")
    p.set_defaults(fn=cmd_suggest)

    args = ap.parse_args()
    if args.C:
        os.chdir(Path(args.C).expanduser())
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
