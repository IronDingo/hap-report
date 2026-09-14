# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""hap_config.py — per-project config resolution shared by all openHAP tools.

Resolution order, first hit wins:
  1. CLI arg (only where a tool already accepts one, e.g. the DXF path)
  2. Environment variable (the documented DXF_PATH / HAP_EXTRACTS / ... set)
  3. hap-project.toml — $HAP_PROJECT if set, else walked up from CWD
  4. the tool's guard-exit, now carrying the next move

Import from a sibling tool:  from hap_config import cfg
"""
from __future__ import annotations
import os, re, sys, tomllib
from pathlib import Path
from typing import Any


def _find_project_file() -> Path | None:
    env = os.environ.get("HAP_PROJECT")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            sys.exit(f"$HAP_PROJECT points at {p} but there is no file there")
        return p
    for d in [Path.cwd(), *Path.cwd().parents]:
        p = d / "hap-project.toml"
        if p.is_file():
            return p
    return None


class Cfg:
    def __init__(self) -> None:
        self.path = _find_project_file()
        self.data: dict = {}
        if self.path:
            try:
                self.data = tomllib.loads(self.path.read_text())
            except tomllib.TOMLDecodeError as e:
                sys.exit(f"{self.path}: TOML parse error — {e}")

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str, hint: str = "") -> Any:
        val = self.get(dotted)
        if val in (None, "", [], {}):
            where = self.path or "hap-project.toml (none found — run `openhap init`)"
            msg = f"[{dotted}] is not set in {where}"
            sys.exit(f"{msg}. {hint}" if hint else msg)
        return val

    # --- the two paths every tool needs -----------------------------------
    def dxf_path(self) -> Path:
        raw = (os.environ.get("DXF_PATH")
               or (sys.argv[1] if len(sys.argv) > 1 else "")
               or self.get("drawing.dxf", ""))
        p = Path(raw).expanduser()
        if not raw or not p.exists():
            sys.exit("No drawing: set [drawing].dxf in hap-project.toml, "
                     "or DXF_PATH, or pass the path as arg 1")
        return p

    def extracts_dir(self, create: bool = True) -> Path:
        raw = os.environ.get("HAP_EXTRACTS") or self.get("drawing.extracts", "extracts")
        p = Path(raw).expanduser()
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    # --- typed helpers ----------------------------------------------------
    def regex(self, dotted: str, default: str) -> re.Pattern:
        return re.compile(self.get(dotted, default))

    def window_includes(self) -> list[tuple[re.Pattern, str]]:
        rows = self.require("windows.include",
                            hint="run `openhap suggest windows` to draft the patterns")
        return [(re.compile(r["pattern"]), r["category"]) for r in rows]

    def window_excludes(self) -> list[re.Pattern]:
        return [re.compile(p) for p in self.get("windows.exclude", [])]


cfg = Cfg()
