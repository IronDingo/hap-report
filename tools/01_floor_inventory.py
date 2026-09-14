# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""01_floor_inventory.py — per-floor-block inventory.

For each configured floor block:
  - Bounding box of all geometry (in metres)
  - Entity counts by DXF type
  - Layers in use with counts
  - All MTEXT/TEXT content with insert points + layer
  - INSERT attributes carrying ROOM_*/RM_*/NAME tags (if any)

Walks the block definition AND any nested INSERTs (virtual_entities)
so we see the real geometry, not just top-level proxies.

Writes one JSON per floor → extracts/floor_*.json
"""
from __future__ import annotations
from pathlib import Path
import os, sys
from collections import Counter, defaultdict
import json
import math
import ezdxf
from ezdxf.math import BoundingBox

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

DXF = cfg.dxf_path()
OUT_DIR = cfg.extracts_dir()

MM_TO_M = 0.001

# {floor label -> floor block name in the DXF} — [floors.blocks].
FLOORS: dict[str, str] = cfg.require(
    "floors.blocks", hint="`openhap suggest floors --write` drafts candidates")
# ==============================================================================

ROOM_TAGS = {"ROOM_NAME", "RM_NAME", "NAME", "ROOM_NUMBER", "NUMBER", "RM_NUM",
             "RM_NUMBER", "ROOMNAME", "ROOMNUMBER"}


def safe_point(e, attr="insert"):
    try:
        p = getattr(e.dxf, attr)
        return float(p.x), float(p.y), float(getattr(p, "z", 0.0))
    except Exception:
        return None


def update_bbox(bbox: BoundingBox, e) -> None:
    """Best-effort: extend bbox using common entity-extents methods.
    Falls back silently if entity has no usable extent."""
    try:
        eb = e.bbox()  # available on many entity types in ezdxf 1.4
        if eb.has_data:
            bbox.extend([eb.extmin, eb.extmax])
            return
    except Exception:
        pass
    # fallback: try insert point
    for attr in ("insert", "location", "start", "center"):
        try:
            p = getattr(e.dxf, attr)
            bbox.extend([(float(p.x), float(p.y), float(getattr(p, "z", 0.0)))])
            return
        except Exception:
            continue


def walk_block(blk, type_counts, per_layer, texts, room_attr_inserts, bbox, depth=0, max_depth=4):
    """Recurse through INSERTs to gather everything below this block."""
    for e in blk:
        et = e.dxftype()
        type_counts[et] += 1
        per_layer[e.dxf.layer][et] += 1
        update_bbox(bbox, e)

        if et in ("TEXT", "MTEXT"):
            try:
                txt = e.dxf.text if et == "TEXT" else e.plain_text()
            except Exception:
                txt = ""
            ins = safe_point(e, "insert")
            texts.append({
                "type": et,
                "layer": e.dxf.layer,
                "text": txt,
                "x_mm": ins[0] if ins else None,
                "y_mm": ins[1] if ins else None,
                "x_m": ins[0] * MM_TO_M if ins else None,
                "y_m": ins[1] * MM_TO_M if ins else None,
            })

        if et == "INSERT":
            # capture room-relevant attribs
            attribs = {att.dxf.tag: att.dxf.text for att in e.attribs}
            relevant = {k: v for k, v in attribs.items() if k.upper() in ROOM_TAGS}
            if relevant:
                ip = safe_point(e, "insert")
                room_attr_inserts.append({
                    "block": e.dxf.name,
                    "layer": e.dxf.layer,
                    "x_m": ip[0] * MM_TO_M if ip else None,
                    "y_m": ip[1] * MM_TO_M if ip else None,
                    "attribs": attribs,
                })
            # recurse into the referenced block definition (but cap depth — some
            # families nest deeply and we don't need every leaf)
            if depth < max_depth:
                try:
                    sub = e.doc.blocks[e.dxf.name]
                    walk_block(sub, type_counts, per_layer, texts, room_attr_inserts, bbox, depth+1, max_depth)
                except Exception:
                    pass


def process_floor(doc, label, block_name):
    blk = doc.blocks.get(block_name)
    if blk is None:
        return {"floor": label, "block": block_name, "error": "block not found"}

    type_counts: Counter = Counter()
    per_layer: dict[str, Counter] = defaultdict(Counter)
    texts: list[dict] = []
    room_attr_inserts: list[dict] = []
    bbox = BoundingBox()

    walk_block(blk, type_counts, per_layer, texts, room_attr_inserts, bbox)

    if bbox.has_data:
        xmin, ymin = bbox.extmin.x, bbox.extmin.y
        xmax, ymax = bbox.extmax.x, bbox.extmax.y
        bbox_m = {
            "xmin_m": xmin * MM_TO_M, "ymin_m": ymin * MM_TO_M,
            "xmax_m": xmax * MM_TO_M, "ymax_m": ymax * MM_TO_M,
            "width_m": (xmax - xmin) * MM_TO_M,
            "height_m": (ymax - ymin) * MM_TO_M,
        }
    else:
        bbox_m = None

    out = {
        "floor": label,
        "block": block_name,
        "bbox_local_m": bbox_m,
        "entity_count_total": sum(type_counts.values()),
        "entity_counts_by_type": dict(type_counts),
        "layer_counts": {L: dict(c) for L, c in sorted(per_layer.items())},
        "n_texts": len(texts),
        "texts": texts,
        "room_attribute_inserts": room_attr_inserts,
    }
    return out


def main():
    print(f"Loading {DXF.name} ...")
    doc = ezdxf.readfile(str(DXF))
    for label, block_name in FLOORS.items():
        print(f"  processing {label} ({block_name}) ...")
        result = process_floor(doc, label, block_name)
        outpath = OUT_DIR / f"floor_{label}.json"
        outpath.write_text(json.dumps(result, indent=2, default=str))
        if "error" in result:
            print(f"    ERROR: {result['error']}")
        else:
            print(f"    entities={result['entity_count_total']:>6}  texts={result['n_texts']:>4}  bbox={result['bbox_local_m']}")
    print("done.")


if __name__ == "__main__":
    main()
