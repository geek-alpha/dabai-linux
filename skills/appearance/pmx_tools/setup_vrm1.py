"""
Enable VRM1 on an mmd_tools armature: humanoid, expressions, meta, MToon1.

    result = setup_vrm1_on_armature("Armature", dry_run=False, model_name="MyModel")
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import bpy

# Sibling tools (same directory)
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(module_filename: str) -> dict:
    path = os.path.join(_TOOLS_DIR, module_filename)
    ns: Dict[str, Any] = {"__file__": path}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def _get_armature(armature_object_name: str):
    obj = bpy.data.objects.get(armature_object_name)
    if obj is None:
        return None
    if obj.type != "ARMATURE":
        return None
    return obj


def vrm_addon_available() -> bool:
    return hasattr(bpy.types.Armature, "bl_rna") and any(
        p.identifier == "vrm_addon_extension"
        for p in bpy.types.Armature.bl_rna.properties
    )


def enable_vrm1_spec(armature) -> Dict[str, Any]:
    if not hasattr(armature.data, "vrm_addon_extension"):
        return {"ok": False, "error": "vrm_addon_extension missing — enable VRM Add-on."}
    armature.data.vrm_addon_extension.spec_version = "1.0"
    return {"ok": True, "spec_version": "1.0"}


def stub_vrm1_meta(armature, model_name: str = "MMD Model") -> Dict[str, Any]:
    meta = armature.data.vrm_addon_extension.vrm1.meta
    meta.vrm_name = model_name or "MMD Model"
    meta.version = meta.version or "1.0.0"
    if len(meta.authors) == 0:
        meta.authors.add().value = model_name or "Unknown"
    # Safe conservative defaults (documented in reference.md)
    try:
        meta.avatar_permission = "onlyAuthor"
        meta.commercial_usage = "personalNonProfit"
        meta.credit_notation = "required"
        meta.allow_redistribution = False
        meta.modification = "prohibited"
    except Exception:
        pass
    return {
        "vrm_name": meta.vrm_name,
        "version": meta.version,
        "author_count": len(meta.authors),
    }


def assign_humanoid_automatic(armature_object_name: str) -> Dict[str, Any]:
    if not hasattr(bpy.ops.vrm, "assign_vrm1_humanoid_human_bones_automatically"):
        return {
            "ok": False,
            "error": "assign_vrm1_humanoid_human_bones_automatically missing",
        }
    result = bpy.ops.vrm.assign_vrm1_humanoid_human_bones_automatically(
        armature_object_name=armature_object_name
    )
    return {"ok": result == {"FINISHED"}, "operator_result": list(result)}


def apply_estimated_humanoid_t_pose(armature_object_name: str) -> Dict[str, Any]:
    """
    Pose armature to VRM estimated humanoid T-pose (Pose Mode; rest unchanged).

    MMD rest is often A-ish; VRM export requires T-pose. Prefer the VRM Add-on
    estimator after humanoid slots are assigned.
    """
    if not hasattr(bpy.ops.vrm, "make_estimated_humanoid_t_pose"):
        return {
            "ok": False,
            "error": "make_estimated_humanoid_t_pose missing",
        }
    result = bpy.ops.vrm.make_estimated_humanoid_t_pose(
        armature_object_name=armature_object_name
    )
    out: Dict[str, Any] = {
        "ok": result == {"FINISHED"},
        "operator_result": list(result),
    }
    arm = _get_armature(armature_object_name)
    if arm is not None and hasattr(arm.data, "vrm_addon_extension"):
        # Export uses this pose (not rest) so estimated T sticks.
        try:
            hum = arm.data.vrm_addon_extension.vrm1.humanoid
            hum.pose = "currentPose"
            out["humanoid_pose"] = hum.pose
        except Exception as exc:
            out["humanoid_pose_error"] = str(exc)
    return out


def assign_expressions_from_mmd(armature_object_name: str) -> Dict[str, Any]:
    if not hasattr(bpy.ops.vrm, "assign_vrm1_expressions_from_mmd"):
        return {
            "ok": False,
            "error": "assign_vrm1_expressions_from_mmd missing",
        }
    result = bpy.ops.vrm.assign_vrm1_expressions_from_mmd(
        armature_object_name=armature_object_name
    )
    return {"ok": result == {"FINISHED"}, "operator_result": list(result)}


def snapshot_shapekey_names(
    mesh_object_names: Optional[List[str]] = None,
    armature_object_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect shape-key names per mesh. Used to prove MMD morphs stay intact."""
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
            elif arm.parent is not None and obj.parent == arm.parent:
                if obj.name not in names:
                    names.append(obj.name)

    per_mesh: Dict[str, List[str]] = {}
    all_keys: List[str] = []
    for mesh_name in names:
        obj = bpy.data.objects.get(mesh_name)
        if obj is None or obj.type != "MESH" or obj.data is None:
            continue
        sk = obj.data.shape_keys
        if sk is None:
            continue
        keys = [kb.name for kb in sk.key_blocks]
        if keys:
            per_mesh[mesh_name] = keys
            all_keys.extend(keys)

    return {
        "mesh_count_with_keys": len(per_mesh),
        "total_key_slots": len(all_keys),
        "unique_key_names": sorted(set(all_keys)),
        "unique_count": len(set(all_keys)),
        "per_mesh": per_mesh,
    }


def compare_shapekey_snapshots(
    before: Dict[str, Any], after: Dict[str, Any]
) -> Dict[str, Any]:
    before_set = set(before.get("unique_key_names") or [])
    after_set = set(after.get("unique_key_names") or [])
    missing = sorted(before_set - after_set)
    added = sorted(after_set - before_set)
    per_mesh_before = before.get("per_mesh") or {}
    per_mesh_after = after.get("per_mesh") or {}
    renamed_meshes: List[str] = []
    for mesh_name, keys in per_mesh_before.items():
        after_keys = per_mesh_after.get(mesh_name)
        if after_keys is None:
            renamed_meshes.append(mesh_name)
            continue
        if list(keys) != list(after_keys):
            renamed_meshes.append(mesh_name)

    untouched = not missing and not added and not renamed_meshes
    return {
        "shapekeys_untouched": untouched,
        "missing_names": missing,
        "added_names": added,
        "meshes_with_key_order_or_name_drift": renamed_meshes,
        "before_unique_count": before.get("unique_count"),
        "after_unique_count": after.get("unique_count"),
    }


_MTOON_LIT_WHITE = (1.0, 1.0, 1.0, 1.0)
_MTOON_ALPHA_CUTOUT = "MASK"  # glTF/VRM cutout


def _set_mtoon1_lit_color_white(mtoon) -> bool:
    """
    Force MToon1 Lit Color (PBR baseColorFactor) to white.

    Enabling MToon1 on MMD materials often leaves base_color_factor black
    (0,0,0,*) even when mmd_material.diffuse_color is white — textures then
    render black. Stamp white so albedo textures show correctly.
    """
    pbr = mtoon.pbr_metallic_roughness
    before = tuple(pbr.base_color_factor)
    if before == _MTOON_LIT_WHITE:
        return False
    pbr.base_color_factor = _MTOON_LIT_WHITE
    return True


def _set_mtoon1_alpha_cutout(mtoon) -> bool:
    """Force MToon1 alpha mode to cutout (MASK)."""
    before = mtoon.alpha_mode
    if before == _MTOON_ALPHA_CUTOUT:
        return False
    mtoon.alpha_mode = _MTOON_ALPHA_CUTOUT
    return True


def enable_mtoon1_on_meshes(
    mesh_object_names: Optional[List[str]] = None,
    armature_object_name: Optional[str] = None,
) -> Dict[str, Any]:
    mats = set()
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

    enabled: List[str] = []
    skipped: List[str] = []
    lit_set_white: List[str] = []
    alpha_set_cutout: List[str] = []
    errors: List[str] = []

    for mesh_name in names:
        obj = bpy.data.objects.get(mesh_name)
        if obj is None or obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in mats:
                continue
            mats.add(mat.name)
            if not hasattr(mat, "vrm_addon_extension"):
                errors.append(f"{mat.name}:no_vrm_extension")
                continue
            try:
                mtoon = mat.vrm_addon_extension.mtoon1
                if mtoon.enabled:
                    skipped.append(mat.name)
                else:
                    mtoon.enabled = True
                    enabled.append(mat.name)
                if _set_mtoon1_lit_color_white(mtoon):
                    lit_set_white.append(mat.name)
                if _set_mtoon1_alpha_cutout(mtoon):
                    alpha_set_cutout.append(mat.name)
            except Exception as exc:
                errors.append(f"{mat.name}:{exc}")

    return {
        "enabled": enabled,
        "enabled_count": len(enabled),
        "already_enabled": skipped,
        "lit_color_set_white": lit_set_white,
        "lit_color_set_white_count": len(lit_set_white),
        "alpha_mode_set_cutout": alpha_set_cutout,
        "alpha_mode_set_cutout_count": len(alpha_set_cutout),
        "errors": errors,
        "mesh_object_names": names,
    }


def setup_vrm1_on_armature(
    armature_object_name: str,
    dry_run: bool = True,
    model_name: Optional[str] = None,
    mesh_object_names: Optional[List[str]] = None,
    include_fingers: bool = True,
) -> Dict[str, Any]:
    arm = _get_armature(armature_object_name)
    if arm is None:
        return {
            "phase": "setup",
            "error": f"Armature not found: {armature_object_name!r}",
            "applied": False,
        }

    if not vrm_addon_available() and not hasattr(arm.data, "vrm_addon_extension"):
        return {
            "phase": "setup",
            "error": "VRM Add-on not available on armature data.",
            "applied": False,
        }

    bone_map_ns = _load_sibling("mmd_vrm1_bone_map.py")
    plan_fallback = bone_map_ns["plan_fallback_humanoid"]
    apply_fallback = bone_map_ns["apply_fallback_humanoid"]

    display_name = model_name or arm.name
    planned_fallback = plan_fallback(arm, include_fingers=include_fingers, only_empty=True)

    if dry_run:
        shapekey_snap = snapshot_shapekey_names(
            mesh_object_names=mesh_object_names,
            armature_object_name=armature_object_name,
        )
        return {
            "phase": "setup",
            "dry_run": True,
            "applied": False,
            "armature_object_name": armature_object_name,
            "model_name": display_name,
            "will_enable_vrm1": True,
            "will_assign_humanoid_auto": hasattr(
                bpy.ops.vrm, "assign_vrm1_humanoid_human_bones_automatically"
            ),
            "will_assign_expressions_from_mmd": hasattr(
                bpy.ops.vrm, "assign_vrm1_expressions_from_mmd"
            ),
            "will_apply_estimated_t_pose": hasattr(
                bpy.ops.vrm, "make_estimated_humanoid_t_pose"
            ),
            "will_keep_mmd_shapekeys": True,
            "shapekey_snapshot": {
                "mesh_count_with_keys": shapekey_snap["mesh_count_with_keys"],
                "unique_count": shapekey_snap["unique_count"],
                "sample_names": shapekey_snap["unique_key_names"][:30],
            },
            "planned_humanoid": planned_fallback,
            "will_enable_mtoon1": True,
            "will_rename_materials_en": True,
            "will_rename_bones_en": True,
            "mesh_object_names": mesh_object_names or [],
            "message": (
                f"Would enable VRM1 on {armature_object_name!r}, "
                f"auto+fallback humanoid, estimated T-pose, "
                f"MMD expression binds (keys kept), meta, MToon1, "
                f"JP material + bone English glosses."
            ),
        }

    steps: Dict[str, Any] = {}

    steps["enable_vrm1"] = enable_vrm1_spec(arm)
    if not steps["enable_vrm1"].get("ok"):
        return {
            "phase": "setup",
            "dry_run": False,
            "applied": False,
            "armature_object_name": armature_object_name,
            "steps": steps,
            "error": steps["enable_vrm1"].get("error"),
        }

    steps["meta"] = stub_vrm1_meta(arm, display_name)
    steps["humanoid_auto"] = assign_humanoid_automatic(armature_object_name)
    steps["humanoid_fallback"] = apply_fallback(
        arm,
        include_fingers=include_fingers,
        only_empty=True,
        dry_run=False,
    )
    steps["estimated_t_pose"] = apply_estimated_humanoid_t_pose(armature_object_name)

    before_keys = snapshot_shapekey_names(
        mesh_object_names=mesh_object_names,
        armature_object_name=armature_object_name,
    )
    steps["expressions_mmd"] = assign_expressions_from_mmd(armature_object_name)
    after_keys = snapshot_shapekey_names(
        mesh_object_names=mesh_object_names,
        armature_object_name=armature_object_name,
    )
    steps["shapekey_guard"] = compare_shapekey_snapshots(before_keys, after_keys)
    steps["shapekey_guard"]["before_unique_count"] = before_keys["unique_count"]
    steps["shapekey_guard"]["after_unique_count"] = after_keys["unique_count"]

    steps["mtoon1"] = enable_mtoon1_on_meshes(
        mesh_object_names=mesh_object_names,
        armature_object_name=armature_object_name,
    )
    rename_ns = _load_sibling("rename_mmd_materials_en.py")
    steps["rename_materials_en"] = rename_ns["rename_mmd_materials_with_english"](
        mesh_object_names=mesh_object_names,
        armature_object_name=armature_object_name,
        dry_run=False,
    )
    bone_rename_ns = _load_sibling("rename_mmd_bones_en.py")
    steps["rename_bones_en"] = bone_rename_ns["rename_mmd_bones_with_english"](
        armature_object_name,
        dry_run=False,
    )

    return {
        "phase": "setup",
        "dry_run": False,
        "applied": True,
        "armature_object_name": armature_object_name,
        "model_name": display_name,
        "shapekeys_untouched": steps["shapekey_guard"]["shapekeys_untouched"],
        "steps": steps,
    }


if __name__ == "__main__":
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    result = (
        setup_vrm1_on_armature(arms[0].name, dry_run=True)
        if arms
        else {"error": "no_armature"}
    )
