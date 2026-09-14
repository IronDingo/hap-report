# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""
01_layer_audit.py — entity-type inventory per layer.

Tells us whether room labels are TEXT/MTEXT or block attributes, what
geometry types live on each architectural layer, and which paper-space
layouts (likely floors / pavilions) exist. This is the diagnostic that
determines what the extraction script needs to handle.

Output:
  - stdout: summary
  - layer_audit.txt: full breakdown
"""

from collections import Counter, defaultdict
from pathlib import Path
import os, sys
import ezdxf

# ===== CONFIG — resolved via hap_config: CLI arg > env > hap-project.toml =====
from hap_config import cfg

DXF = cfg.dxf_path()
EXTRACTS = cfg.extracts_dir()
OUT = EXTRACTS / "layer_audit.txt"
# Room-label layer (AIA NCS standard default — [layers].label overrides).
LABEL_LAYER = cfg.get("layers.label", "A-AREA-IDEN")
# ==============================================================================

UNIT_NAMES = {0: "unspecified", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}


def audit_space(space, label):
    """Return {layer: Counter(entity_types)} for one layout."""
    per_layer = defaultdict(Counter)
    for e in space:
        per_layer[e.dxf.layer][e.dxftype()] += 1
    return per_layer


def main():
    doc = ezdxf.readfile(str(DXF))
    units = doc.header.get("$INSUNITS", 0)
    layouts = [lay.name for lay in doc.layouts]

    lines = []
    lines.append(f"DXF: {DXF.name}")
    lines.append(f"Units: {UNIT_NAMES.get(units, units)} (INSUNITS={units})")
    lines.append(f"Layer count: {len(list(doc.layers))}")
    lines.append(f"Layouts: {layouts}")
    lines.append("")

    # Modelspace
    msp = doc.modelspace()
    msp_layers = audit_space(msp, "Model")
    lines.append(f"=== Modelspace ({len(list(msp))} entities) ===")
    for layer in sorted(msp_layers):
        counts = msp_layers[layer]
        breakdown = ", ".join(f"{t}={c}" for t, c in counts.most_common())
        lines.append(f"  {layer}: {breakdown}")
    lines.append("")

    # Paper-space layouts (floors / pavilions live here on multi-sheet sets)
    for name in layouts:
        if name == "Model":
            continue
        psp = doc.layout(name)
        psp_layers = audit_space(psp, name)
        total = sum(sum(c.values()) for c in psp_layers.values())
        lines.append(f"=== Layout '{name}' ({total} entities) ===")
        for layer in sorted(psp_layers):
            counts = psp_layers[layer]
            breakdown = ", ".join(f"{t}={c}" for t, c in counts.most_common())
            lines.append(f"  {layer}: {breakdown}")
        lines.append("")

    # Deep-dive on A-AREA-IDEN: are room labels block attributes?
    lines.append("=== A-AREA-IDEN deep dive (likely room labels) ===")
    for e in msp.query(f"INSERT[layer=='{LABEL_LAYER}']"):
        attrs = {att.dxf.tag: att.dxf.text for att in e.attribs}
        lines.append(f"  block={e.dxf.name} attrs={attrs}")
        if len([x for x in lines if "block=" in x]) >= 5:
            lines.append("  ... (truncated, showing first 5)")
            break
    for e in msp.query("TEXT MTEXT")[:5]:
        if e.dxf.layer == LABEL_LAYER:
            txt = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
            lines.append(f"  {e.dxftype()}: {txt!r}")

    OUT.write_text("\n".join(lines))
    print("\n".join(lines[:25]))
    print(f"... full report written to {OUT}")


if __name__ == "__main__":
    main()
