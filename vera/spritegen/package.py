"""package.py — assemble the exportable Character/ package (pure filesystem)
============================================================================
Bundles the produced assets into the engine-friendly layout from the spec and
zips it, so a game (Godot / Unity / GameMaker) can combine animations at runtime
instead of needing one baked sheet per combination:

    Character/
      definition.yaml         # the source-of-truth definition
      reference.png           # bg-removed reference
      sprite/<anim>.png       # sprite sheets
      sprite/<anim>.json      # per-sheet atlas (frame rects + tag)
      preview/<anim>.gif      # animated preview
      visemes/<v>.png         # mouth shapes (companion package)
      metadata.json           # summary manifest
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List

log = logging.getLogger("vera.spritegen")

try:
    import yaml as _yaml
    _HAS_YAML = True
except Exception:
    _yaml = None
    _HAS_YAML = False


def _copy(src: Path, dst: Path) -> bool:
    if src and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def build_package(defn, char_root: Path) -> Dict:
    """Build (or rebuild) the Character/ package under char_root/package and zip it.

    `defn` is a CharacterDefinition; `char_root` is _chars/<char_id>/. Returns a
    manifest dict with the package dir, zip path, and relative file list (all
    relative to char_root, so the asset route can serve them)."""
    pkg = char_root / "package" / "Character"
    if (char_root / "package").exists():
        shutil.rmtree(char_root / "package", ignore_errors=True)
    pkg.mkdir(parents=True, exist_ok=True)

    files: List[str] = []

    # reference
    if defn.reference:
        if _copy(char_root / defn.reference, pkg / "reference.png"):
            files.append("reference.png")

    # animation sheets + atlas + preview
    summary_anims = {}
    for anim, meta in (defn.sheets or {}).items():
        sfile = meta.get("file")
        if sfile and _copy(char_root / sfile, pkg / "sprite" / f"{anim}.png"):
            files.append(f"sprite/{anim}.png")
        if meta.get("json") and _copy(char_root / meta["json"], pkg / "sprite" / f"{anim}.json"):
            files.append(f"sprite/{anim}.json")
        if meta.get("gif") and _copy(char_root / meta["gif"], pkg / "preview" / f"{anim}.gif"):
            files.append(f"preview/{anim}.gif")
        summary_anims[anim] = {k: meta.get(k) for k in
                               ("columns", "rows", "frame_width", "frame_height",
                                "count", "fps", "loop")}

    # visemes (companion package) — stored as the special 'visemes' frame group
    for i, fn in enumerate(defn.frame_files.get("visemes", [])):
        vname = (defn.visemes[i] if i < len(defn.visemes) else f"v{i}")
        if _copy(char_root / fn, pkg / "visemes" / f"{vname}.png"):
            files.append(f"visemes/{vname}.png")

    # definition.(yaml|json)
    ddict = defn.to_dict()
    if _HAS_YAML:
        (pkg / "definition.yaml").write_text(
            _yaml.safe_dump(ddict, sort_keys=False, allow_unicode=True), encoding="utf-8")
        files.append("definition.yaml")
    else:
        (pkg / "definition.json").write_text(
            json.dumps(ddict, indent=2), encoding="utf-8")
        files.append("definition.json")

    # metadata.json summary manifest
    metadata = {
        "name": defn.name, "char_id": defn.char_id, "style": defn.style,
        "sprite_size": defn.sprite_size, "colors": defn.colors,
        "agent_id": defn.agent_id or None,
        "animations": summary_anims,
        "visemes": defn.visemes,
        "files": files,
    }
    (pkg / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    files.append("metadata.json")

    # zip the whole Character/ dir
    zip_rel = "package/Character.zip"
    zip_path = char_root / zip_rel
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            zf.write(pkg / rel, arcname=f"Character/{rel}")

    return {
        "package_dir": "package/Character",
        "zip": zip_rel,
        "files": files,
        "metadata": metadata,
    }
