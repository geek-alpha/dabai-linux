"""
Append English glosses to MMD bone names: ``腕.L`` → ``腕.L (arm.L)``.

Keeps the original JP (or mixed) Blender bone name; appends `` (english)``.
Blender bone names are capped at **63 UTF-8 bytes** — gloss is truncated to fit.
Idempotent: strips trailing `` (…)`` before re-applying.

Also rewrites VRM1 humanoid ``node.bone_name`` slots to the new names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import bpy

# Longest-first token replace for gloss text (lowercase English).
# Numbers, .L/.R, ASCII fragments (Twin, Sub, Elbow, …) stay as-is.
_MMD_BONE_EN_TOKENS: Tuple[Tuple[str, str], ...] = tuple(
    sorted(
        {
            "全ての親": "master",
            "センター": "center",
            "グルーブ": "groove",
            "上半身": "upper body",
            "下半身": "lower body",
            "両目先": "both eyes tip",
            "両目": "both eyes",
            "足ＩＫ": "leg ik",
            "足IK親": "leg ik parent",
            "つま先ＩＫ": "toe ik",
            "つま先": "toe",
            "足首": "ankle",
            "腕捩": "arm twist",
            "手捩": "hand twist",
            "手首先": "wrist tip",
            "手首": "wrist",
            "親指": "thumb",
            "人指": "index",
            "中指": "middle",
            "薬指": "ring",
            "小指": "little",
            "前髪": "bangs",
            "髪リボン": "hair ribbon",
            "首リボン": "neck ribbon",
            "帽子毛": "hat hair",
            "前帽子": "front hat",
            "後帽子": "back hat",
            "前スカート": "front skirt",
            "後スカート": "back skirt",
            "スカート": "skirt",
            "コート": "coat",
            "袖": "sleeve",
            "髪": " hair",
            "帽子": "hat",
            "首": "neck",
            "頭": "head",
            "目先": "eye tip",
            "目": "eye",
            "肩": "shoulder",
            "腕": "arm",
            "ひじ": "elbow",
            "ひざ": "knee",
            "足": "leg",
            "胸上": "bust upper",
            "胸下": "bust lower",
            "胸中": "bust mid",
            "胸": "bust",
            "先": " tip",
            "錘": " wt",
            "捩": "twist",
            "親": "parent",
            "ＩＫ": "ik",
        }.items(),
        key=lambda kv: -len(kv[0]),
    )
)

_EN_SUFFIX_RE = re.compile(r"\s+\([^)]*\)\s*$")
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
MAX_BONE_NAME_BYTES = 63


def strip_english_suffix(name: str) -> str:
    return _EN_SUFFIX_RE.sub("", name).rstrip()


def _has_cjk(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if (
            0x3040 <= o <= 0x30FF
            or 0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or 0xFF66 <= o <= 0xFF9D
            or 0xFF10 <= o <= 0xFF19  # fullwidth digits
            or ch in "ＩＫ"
        ):
            return True
    return False


def _gloss_from_tokens(base: str) -> str:
    text = base.translate(_FW_DIGITS)
    for tok, en in _MMD_BONE_EN_TOKENS:
        text = text.replace(tok, en)
    # Space between latin letter and digit: upper body2 → upper body 2
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    # Space between ASCII word and following ASCII word glued by JP replace
    # e.g. Twinhair → Twin hair (if still CamelCase left)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+\.", ".", text)
    # Prefer lowercase gloss (keep .L/.R as .l/.r)
    return text.lower()


def _fit_appended_name(base: str, gloss: str) -> Optional[str]:
    """Return ``base (gloss)`` truncated to 63 UTF-8 bytes, or None if no room."""
    base_b = base.encode("utf-8")
    # " (" + gloss + ")"
    overhead = 3  # space + open paren + close paren in ASCII
    budget = MAX_BONE_NAME_BYTES - len(base_b) - overhead
    if budget < 1:
        return None
    gloss_b = gloss.encode("utf-8")
    if len(gloss_b) > budget:
        gloss_b = gloss_b[:budget]
        gloss = gloss_b.decode("utf-8", errors="ignore").rstrip(" ._-")
        if not gloss:
            return None
    return f"{base} ({gloss})"


def english_gloss_for_bone(name: str) -> Optional[str]:
    base = strip_english_suffix(name)
    if not _has_cjk(base):
        return None
    gloss = _gloss_from_tokens(base)
    if not gloss:
        return None
    # Need at least one translated latin letter (not only digits/punct left)
    if not any(c.isascii() and c.isalpha() for c in gloss):
        return None
    return gloss


def format_bone_name_with_english(name: str) -> Optional[str]:
    base = strip_english_suffix(name)
    gloss = english_gloss_for_bone(base)
    if not gloss:
        return None
    return _fit_appended_name(base, gloss)


def _update_vrm1_humanoid_bone_names(armature, rename_map: Dict[str, str]) -> Dict[str, Any]:
    updated: Dict[str, str] = {}
    if not rename_map:
        return {"updated": updated, "updated_count": 0}
    try:
        human_bones = armature.data.vrm_addon_extension.vrm1.humanoid.human_bones
    except Exception as exc:
        return {"updated": updated, "updated_count": 0, "error": str(exc)}

    for prop in human_bones.bl_rna.properties:
        slot_name = prop.identifier
        if slot_name in ("rna_type", "last_bone_names_version"):
            continue
        slot = getattr(human_bones, slot_name, None)
        if slot is None or not hasattr(slot, "node"):
            continue
        try:
            old = slot.node.bone_name or ""
        except Exception:
            continue
        if old in rename_map:
            new = rename_map[old]
            slot.node.bone_name = new
            updated[slot_name] = new
    return {"updated": updated, "updated_count": len(updated)}


def rename_mmd_bones_with_english(
    armature_object_name: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Rename armature bones to ``{original} ({english})``.

    Keeps JP stem. Updates VRM1 humanoid bone_name references.
    """
    arm = bpy.data.objects.get(armature_object_name)
    if arm is None or arm.type != "ARMATURE":
        return {
            "phase": "rename_bones_en",
            "error": f"Armature not found: {armature_object_name!r}",
            "dry_run": dry_run,
        }

    # Object mode — bone.data rename updates matching vertex groups
    prev_mode = bpy.context.mode
    try:
        bpy.context.view_layer.objects.active = arm
        if arm.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    planned: List[Dict[str, str]] = []
    renamed: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    truncated: List[str] = []
    errors: List[str] = []
    rename_map: Dict[str, str] = {}

    # Snapshot names first (collection mutates during rename)
    bone_names = [b.name for b in arm.data.bones]

    for old_name in bone_names:
        bone = arm.data.bones.get(old_name)
        if bone is None:
            continue
        base = strip_english_suffix(bone.name)
        if not _has_cjk(base):
            skipped.append({"name": bone.name, "reason": "no_cjk"})
            continue
        gloss = english_gloss_for_bone(base)
        if not gloss:
            skipped.append({"name": bone.name, "reason": "no_gloss"})
            continue
        new_name = _fit_appended_name(base, gloss)
        if new_name is None:
            skipped.append({"name": bone.name, "reason": "no_byte_budget"})
            continue
        ideal = f"{base} ({gloss})"
        if new_name != ideal:
            truncated.append(bone.name)
        if new_name == bone.name:
            skipped.append({"name": bone.name, "reason": "already_correct"})
            continue
        planned.append({"from": bone.name, "to": new_name, "gloss": gloss})
        if dry_run:
            continue
        try:
            prev = bone.name
            bone.name = new_name
            # Blender may uniquify on collision
            actual = bone.name
            rename_map[prev] = actual
            renamed.append({"from": prev, "to": actual, "gloss": gloss})
        except Exception as exc:
            errors.append(f"{old_name}:{exc}")

    vrm_update: Dict[str, Any] = {"updated_count": 0}
    if not dry_run and rename_map:
        vrm_update = _update_vrm1_humanoid_bone_names(arm, rename_map)

    try:
        if prev_mode == "POSE" and arm.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
        elif prev_mode == "EDIT_ARMATURE" and arm.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
    except Exception:
        pass

    return {
        "phase": "rename_bones_en",
        "dry_run": dry_run,
        "armature_object_name": armature_object_name,
        "planned": planned[:40],
        "planned_count": len(planned),
        "renamed": renamed[:40],
        "renamed_count": len(renamed),
        "skipped_count": len(skipped),
        "skipped_sample": skipped[:20],
        "truncated_count": len(truncated),
        "vrm_humanoid_update": vrm_update,
        "errors": errors,
    }


if __name__ == "__main__":
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    result = (
        rename_mmd_bones_with_english(arms[0].name, dry_run=True)
        if arms
        else {"error": "no_armature"}
    )
