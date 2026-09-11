"""
Audit VRM1 setup on an armature after PMX import / setup.

    report = audit_vrm1_setup("Armature")
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import bpy

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(module_filename: str) -> dict:
    path = os.path.join(_TOOLS_DIR, module_filename)
    ns: Dict[str, Any] = {"__file__": path}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def _count_expression_binds(armature) -> Dict[str, Any]:
    try:
        expressions = armature.data.vrm_addon_extension.vrm1.expressions
    except Exception as exc:
        return {"error": str(exc), "morph_bind_count": 0, "preset_with_binds": []}

    morph_bind_count = 0
    preset_with_binds: List[str] = []

    # Preset expressions are attributes on .preset
    preset = getattr(expressions, "preset", None)
    if preset is not None:
        for attr in dir(preset):
            if attr.startswith("_"):
                continue
            expr = getattr(preset, attr, None)
            if expr is None or not hasattr(expr, "morph_target_binds"):
                continue
            try:
                n = len(expr.morph_target_binds)
            except Exception:
                n = 0
            if n:
                morph_bind_count += n
                preset_with_binds.append(attr)

    custom = getattr(expressions, "custom", None)
    custom_count = 0
    if custom is not None:
        try:
            for expr in custom:
                try:
                    n = len(expr.morph_target_binds)
                except Exception:
                    n = 0
                morph_bind_count += n
                custom_count += 1 if n else 0
        except Exception:
            pass

    return {
        "morph_bind_count": morph_bind_count,
        "preset_with_binds": sorted(preset_with_binds),
        "custom_with_binds": custom_count,
    }


def _mtoon_stats(armature_object_name: Optional[str] = None) -> Dict[str, Any]:
    enabled = 0
    total = 0
    names_enabled: List[str] = []
    arm = (
        bpy.data.objects.get(armature_object_name)
        if armature_object_name
        else None
    )
    seen = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if arm is not None:
            if obj.parent != arm and obj.find_armature() != arm:
                # also accept same mmd root sibling meshes via name filter skip
                if obj.parent is None or getattr(obj.parent, "mmd_type", None) != "ROOT":
                    if arm.parent is None or obj.parent != arm.parent:
                        continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen:
                continue
            seen.add(mat.name)
            total += 1
            try:
                if mat.vrm_addon_extension.mtoon1.enabled:
                    enabled += 1
                    names_enabled.append(mat.name)
            except Exception:
                pass
    return {
        "material_count": total,
        "mtoon1_enabled_count": enabled,
        "mtoon1_enabled_names": names_enabled[:40],
    }


def audit_vrm1_setup(
    armature_object_name: Optional[str] = None,
) -> Dict[str, Any]:
    bone_map_ns = _load_sibling("mmd_vrm1_bone_map.py")
    required = bone_map_ns["REQUIRED_HUMAN_BONES"]

    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if armature_object_name:
        arm = bpy.data.objects.get(armature_object_name)
        if arm is None or arm.type != "ARMATURE":
            return {
                "phase": "audit",
                "error": f"Armature not found: {armature_object_name!r}",
            }
    elif len(arms) == 1:
        arm = arms[0]
    elif not arms:
        return {"phase": "audit", "error": "No armature in scene"}
    else:
        return {
            "phase": "audit",
            "error": "multiple_armatures",
            "armatures": [a.name for a in arms],
            "message": "Pass armature_object_name.",
        }

    spec_version = None
    humanoid_map: Dict[str, str] = {}
    required_missing: List[str] = []
    required_filled: List[str] = []

    if hasattr(arm.data, "vrm_addon_extension"):
        ext = arm.data.vrm_addon_extension
        try:
            spec_version = ext.spec_version
        except Exception:
            spec_version = None
        try:
            hb = ext.vrm1.humanoid.human_bones
            for slot in dir(hb):
                if slot.startswith("_"):
                    continue
                node = getattr(hb, slot, None)
                if node is None or not hasattr(node, "node"):
                    continue
                try:
                    name = node.node.bone_name or ""
                except Exception:
                    name = ""
                if name:
                    humanoid_map[slot] = name
            for slot in required:
                if humanoid_map.get(slot):
                    required_filled.append(slot)
                else:
                    required_missing.append(slot)
        except Exception as exc:
            return {
                "phase": "audit",
                "armature_object_name": arm.name,
                "error": f"humanoid_read_failed:{exc}",
            }
    else:
        return {
            "phase": "audit",
            "armature_object_name": arm.name,
            "error": "vrm_addon_extension missing",
            "vrm1_ready": False,
        }

    expr = _count_expression_binds(arm)
    mtoon = _mtoon_stats(arm.name)

    vrm1_ready = (
        spec_version == "1.0"
        and len(required_missing) == 0
    )

    return {
        "phase": "audit",
        "armature_object_name": arm.name,
        "spec_version": spec_version,
        "bone_count": len(arm.data.bones),
        "humanoid_assigned": humanoid_map,
        "humanoid_assigned_count": len(humanoid_map),
        "required_filled": required_filled,
        "required_missing": required_missing,
        "required_complete": len(required_missing) == 0,
        "expressions": expr,
        "mtoon": mtoon,
        "vrm1_ready": vrm1_ready,
        "export_note": "Export is out of skill scope — use VRM Add-on manually when ready.",
    }


if __name__ == "__main__":
    result = audit_vrm1_setup()
