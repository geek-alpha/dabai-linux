"""
Append English glosses to MMD material names: ``歯`` → ``歯 (teeth)``.

Keeps the original JP (or mixed) name; adds `` (english)`` for readability.
Skips ASCII-only names (``MT_Body``, ``mmd_tools_rigid_*``). Idempotent:
strips an existing trailing `` (…)`` before re-applying the map.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

import bpy

# Common MMD / PMX material name → short English gloss (lowercase).
# Extend as new models need; unmatched JP names are reported, not guessed.
MMD_MATERIAL_EN_GLOSS: Dict[str, str] = {
    # Face / head
    "顔": "face",
    "首": "neck",
    "口": "mouth",
    "歯": "teeth",
    "白目": "sclera",
    "瞳": "iris",
    "瞳EX": "iris ex",
    "はう目": "hau eyes",
    "まつげ": "eyelashes",
    "右まつげ": "right eyelashes",
    "まゆ": "eyebrows",
    "眼帯": "eyepatch",
    "青ざめ": "pallor",
    # Hair
    "前髪": "bangs",
    "後髪": "back hair",
    "カミカゲ": "hair shadow",
    # Hat
    "帽子": "hat",
    "帽子毛": "hat hair",
    "帽子前髪": "hat bangs",
    "帽子カミカゲ": "hat hair shadow",
    # Clothes
    "コート": "coat",
    "コート金": "coat gold",
    "スカート": "skirt",
    "スカートベルト": "skirt belt",
    "スカート金": "skirt gold",
    "服金": "clothes gold",
    "短袖": "short sleeves",
    "短袖金": "short sleeves gold",
    "長袖": "long sleeves",
    "長袖金": "long sleeves gold",
    "肩金": "shoulder gold",
    "革": "leather",
    "足": "feet",
    # Misc
    "新規材質1": "new material 1",
    "新規材質": "new material",
}

_EN_SUFFIX_RE = re.compile(r"\s+\([^)]*\)\s*$")
_ASCII_ONLY_RE = re.compile(r"^[A-Za-z0-9_ .\-]+$")


def strip_english_suffix(name: str) -> str:
    return _EN_SUFFIX_RE.sub("", name).rstrip()


def _has_cjk(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if (
            0x3040 <= o <= 0x30FF  # hiragana / katakana
            or 0x4E00 <= o <= 0x9FFF  # CJK
            or 0x3400 <= o <= 0x4DBF  # CJK ext A
            or 0xFF66 <= o <= 0xFF9D  # halfwidth katakana
        ):
            return True
    return False


def english_gloss_for_material(name: str) -> Optional[str]:
    base = strip_english_suffix(name)
    if base in MMD_MATERIAL_EN_GLOSS:
        return MMD_MATERIAL_EN_GLOSS[base]
    return None


def format_material_name_with_english(name: str, gloss: Optional[str] = None) -> Optional[str]:
    base = strip_english_suffix(name)
    en = gloss if gloss is not None else english_gloss_for_material(base)
    if not en:
        return None
    return f"{base} ({en})"


def _collect_material_names(
    mesh_object_names: Optional[List[str]] = None,
    armature_object_name: Optional[str] = None,
    *,
    all_materials: bool = False,
) -> List[str]:
    if all_materials:
        return [m.name for m in bpy.data.materials]

    mats: Set[str] = set()
    names = list(mesh_object_names or [])
    if armature_object_name and armature_object_name in bpy.data.objects:
        arm = bpy.data.objects[armature_object_name]
        for obj in bpy.data.objects:
            if obj.type != "MESH":
                continue
            if obj.parent == arm or (
                obj.find_armature() is not None and obj.find_armature() == arm
            ):
                if obj.name not in names:
                    names.append(obj.name)

    for mesh_name in names:
        obj = bpy.data.objects.get(mesh_name)
        if obj is None or obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is not None:
                mats.add(slot.material.name)

    if not mats and not names:
        # No mesh scope → all materials with CJK (scene-wide helper)
        return [m.name for m in bpy.data.materials if _has_cjk(m.name)]

    return sorted(mats)


def rename_mmd_materials_with_english(
    mesh_object_names: Optional[List[str]] = None,
    armature_object_name: Optional[str] = None,
    *,
    dry_run: bool = False,
    all_materials: bool = False,
) -> Dict[str, Any]:
    """
    Rename materials to ``{original} ({english})``.

    Skips ASCII-only names. Re-applies gloss if a prior `` (… )`` suffix exists
    so map corrections stick on re-run.
    """
    planned: List[Dict[str, str]] = []
    renamed: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    unmatched: List[str] = []
    errors: List[str] = []

    for mat_name in _collect_material_names(
        mesh_object_names,
        armature_object_name,
        all_materials=all_materials,
    ):
        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            continue
        base = strip_english_suffix(mat.name)
        if _ASCII_ONLY_RE.fullmatch(base) and not _has_cjk(base):
            skipped.append({"name": mat.name, "reason": "already_english"})
            continue
        gloss = english_gloss_for_material(base)
        if gloss is None:
            if _has_cjk(base):
                unmatched.append(mat.name)
            else:
                skipped.append({"name": mat.name, "reason": "no_gloss"})
            continue
        new_name = f"{base} ({gloss})"
        if new_name == mat.name:
            skipped.append({"name": mat.name, "reason": "already_correct"})
            continue
        planned.append({"from": mat.name, "to": new_name, "gloss": gloss})
        if dry_run:
            continue
        try:
            old = mat.name
            mat.name = new_name
            renamed.append({"from": old, "to": mat.name, "gloss": gloss})
        except Exception as exc:
            errors.append(f"{mat.name}:{exc}")

    return {
        "phase": "rename_materials_en",
        "dry_run": dry_run,
        "planned": planned,
        "planned_count": len(planned),
        "renamed": renamed,
        "renamed_count": len(renamed),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "unmatched": unmatched,
        "unmatched_count": len(unmatched),
        "errors": errors,
    }


if __name__ == "__main__":
    result = rename_mmd_materials_with_english(all_materials=True, dry_run=True)
