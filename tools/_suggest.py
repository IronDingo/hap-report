# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""_suggest.py — draft config candidates from the DXF (backs `openhap suggest`).

Modes (arg 1):
  floors   modelspace INSERTs ranked by block bbox area — floor blocks are the
           huge, rarely-placed ones. Candidates for [floors.blocks].
  windows  block names (with usage counts) matching window-family keywords.
           Candidates for [[windows.include]]; category is a keyword guess
           the user must verify.

Output: human table on stdout + extracts/_suggest_<mode>.json for `hap
suggest --write`. Suggestions are drafts — the engineer confirms every one.
"""
from __future__ import annotations
import json
import re
import sys
from collections import Counter

import ezdxf
from ezdxf.bbox import extents

from hap_config import cfg

UNIT_TO_M = {0: 0.001, 1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}

WINDOWISH = re.compile(
    r"window|fen[eê]tre|vitr|glaz|slid|hing|pivot|casement|awning|curtain|"
    r"mur[_ ]?rideau|cw[_\-]|panel", re.I)
NOISE = re.compile(r"Door|Mullion|Frame\b|Louver|\bTag\b|\bSchedule\b|\bSymbol\b", re.I)

CATEGORY_GUESS = [  # first keyword hit wins; "?" = user must decide
    (re.compile(r"slid", re.I), "slide"),
    (re.compile(r"hing|casement|pivot|swing", re.I), "hinge"),
    (re.compile(r"awning", re.I), "cw_awning"),
    (re.compile(r"curtain|mur[_ ]?rideau|cw[_\-]|panel", re.I), "cw_panel"),
]


def guess_category(name: str) -> str:
    for pat, cat in CATEGORY_GUESS:
        if pat.search(name):
            return cat
    return "?"


def suggest_floors(doc, scale: float) -> list[dict]:
    msp_counts = Counter(e.dxf.name for e in doc.modelspace().query("INSERT"))
    out = []
    for name, count in msp_counts.items():
        if count > 20:  # floor blocks are placed once or twice, not en masse
            continue
        blk = doc.blocks.get(name)
        if blk is None:
            continue
        ents = list(blk)
        try:
            bb = extents(ents)
        except Exception:
            continue
        if not bb.has_data:
            continue
        w = (bb.extmax.x - bb.extmin.x) * scale
        h = (bb.extmax.y - bb.extmin.y) * scale
        if max(w, h) < 5.0:  # smaller than 5 m — not a floor plan
            continue
        out.append({"name": name, "msp_count": count,
                    "w_m": round(w, 1), "h_m": round(h, 1), "entities": len(ents)})
    out.sort(key=lambda c: -(c["w_m"] * c["h_m"]))
    return out[:15]


def suggest_windows(doc) -> list[dict]:
    uses: Counter = Counter(e.dxf.name for e in doc.modelspace().query("INSERT"))
    for blk in doc.blocks:
        for e in blk:
            if e.dxftype() == "INSERT":
                uses[e.dxf.name] += 1
    out = []
    for name, n in uses.items():
        if not WINDOWISH.search(name) or NOISE.search(name):
            continue
        out.append({"name": name, "uses": n, "guess": guess_category(name)})
    out.sort(key=lambda c: -c["uses"])
    return out[:25]


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("floors", "windows"):
        sys.exit("usage: _suggest.py floors|windows")
    mode = sys.argv[1]
    # dxf path comes from config/env only — arg 1 is the mode, so bypass
    # cfg.dxf_path()'s argv fallback.
    import os
    raw = os.environ.get("DXF_PATH") or cfg.get("drawing.dxf", "")
    if not raw:
        sys.exit("No drawing: set [drawing].dxf in hap-project.toml (or DXF_PATH)")
    from pathlib import Path
    dxf = Path(raw).expanduser()
    if not dxf.exists():
        sys.exit(f"drawing not found: {dxf}")

    print(f"Scanning {dxf.name} ...")
    doc = ezdxf.readfile(str(dxf))
    scale = UNIT_TO_M.get(doc.header.get("$INSUNITS", 4), 0.001)

    if mode == "floors":
        cands = suggest_floors(doc, scale)
        print(f"\n{len(cands)} floor-block candidate(s) "
              "(large modelspace blocks, biggest first):")
        print(f"  {'block name':<42} {'placed':>6} {'size (m)':>14} {'entities':>9}")
        for c in cands:
            print(f"  {c['name']:<42} {c['msp_count']:>5}× "
                  f"{c['w_m']:>6.1f}×{c['h_m']:<6.1f} {c['entities']:>9}")
    else:
        cands = suggest_windows(doc)
        print(f"\n{len(cands)} window-family candidate(s) (by usage count):")
        print(f"  {'block name':<52} {'uses':>6}  category guess")
        for c in cands:
            print(f"  {c['name']:<52} {c['uses']:>6}  {c['guess']}")

    out = cfg.extracts_dir() / f"_suggest_{mode}.json"
    out.write_text(json.dumps(cands, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
