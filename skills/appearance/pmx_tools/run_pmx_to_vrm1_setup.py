"""
Orchestrate PMX import + VRM1 setup (no export).

    result = run_pmx_to_vrm1_setup(filepath=r"D:\\path\\model.pmx", dry_run=True)
    result = run_pmx_to_vrm1_setup(filepath=r"D:\\path\\model.pmx", dry_run=False)
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Set

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(module_filename: str) -> dict:
    path = os.path.join(_TOOLS_DIR, module_filename)
    ns: Dict[str, Any] = {"__file__": path}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def run_pmx_to_vrm1_setup(
    filepath: Optional[str] = None,
    directory: Optional[str] = None,
    filename: Optional[str] = None,
    dry_run: bool = True,
    new_file: bool = False,
    scale: float = 0.08,
    import_kwargs: Optional[Dict[str, Any]] = None,
    types: Optional[Set[str]] = None,
    armature_object_name: Optional[str] = None,
    skip_import: bool = False,
    include_fingers: bool = True,
) -> Dict[str, Any]:
    """
    Import PMX (unless skip_import) then configure VRM1 on the armature.

    Never calls export_scene.vrm.
    """
    import_ns = _load_sibling("import_pmx.py")
    setup_ns = _load_sibling("setup_vrm1.py")
    audit_ns = _load_sibling("audit_vrm1_setup.py")

    import_pmx = import_ns["import_pmx"]
    resolve_pmx_path = import_ns["resolve_pmx_path"]
    find_mmd_hierarchy = import_ns["find_mmd_hierarchy"]
    mmd_tools_available = import_ns["mmd_tools_available"]
    setup_vrm1_on_armature = setup_ns["setup_vrm1_on_armature"]
    vrm_addon_available = setup_ns["vrm_addon_available"]
    audit_vrm1_setup = audit_ns["audit_vrm1_setup"]

    prereq = {
        "mmd_tools": mmd_tools_available(),
        "vrm_addon": vrm_addon_available(),
    }

    if dry_run and not skip_import:
        resolved = resolve_pmx_path(
            filepath=filepath, directory=directory, filename=filename
        )
        if resolved.get("error"):
            return {
                "skill": "mmd-pmx-to-vrm1",
                "dry_run": True,
                "applied": False,
                "prerequisites": prereq,
                "error": resolved.get("error"),
                "import": resolved,
            }
        import_plan = import_pmx(
            filepath=resolved["filepath"],
            new_file=new_file,
            dry_run=True,
            scale=scale,
            import_kwargs=import_kwargs,
            types=types,
        )
        return {
            "skill": "mmd-pmx-to-vrm1",
            "dry_run": True,
            "applied": False,
            "prerequisites": prereq,
            "import": import_plan,
            "setup": {
                "phase": "setup",
                "dry_run": True,
                "message": (
                    "After import, would enable VRM1, assign humanoid "
                    "(auto + fallback), MMD expressions, meta, MToon1."
                ),
                "include_fingers": include_fingers,
            },
            "export": {
                "skipped": True,
                "reason": "Skill is setup-only; no .vrm export.",
            },
            "message": "Dry-run only — approve then call with dry_run=False.",
        }

    import_result: Dict[str, Any]
    if skip_import:
        if not armature_object_name:
            hierarchy = find_mmd_hierarchy()
            armature_object_name = hierarchy.get("armature_object_name")
            import_result = {
                "phase": "import",
                "skipped": True,
                "reason": "skip_import",
                **hierarchy,
            }
        else:
            import_result = {
                "phase": "import",
                "skipped": True,
                "reason": "skip_import",
                "armature_object_name": armature_object_name,
            }
    else:
        resolved = resolve_pmx_path(
            filepath=filepath, directory=directory, filename=filename
        )
        if resolved.get("error"):
            return {
                "skill": "mmd-pmx-to-vrm1",
                "dry_run": False,
                "applied": False,
                "prerequisites": prereq,
                "error": resolved.get("error"),
                "import": resolved,
            }
        if not prereq["mmd_tools"]:
            return {
                "skill": "mmd-pmx-to-vrm1",
                "dry_run": False,
                "applied": False,
                "prerequisites": prereq,
                "error": "MMD Tools not available",
            }
        import_result = import_pmx(
            filepath=resolved["filepath"],
            new_file=new_file,
            dry_run=False,
            scale=scale,
            import_kwargs=import_kwargs,
            types=types,
        )
        armature_object_name = (
            armature_object_name
            or import_result.get("armature_object_name")
        )

    if not armature_object_name:
        return {
            "skill": "mmd-pmx-to-vrm1",
            "dry_run": False,
            "applied": False,
            "prerequisites": prereq,
            "import": import_result,
            "error": "No armature found after import",
            "export": {"skipped": True, "reason": "setup-only"},
        }

    if not prereq["vrm_addon"]:
        return {
            "skill": "mmd-pmx-to-vrm1",
            "dry_run": dry_run,
            "applied": False,
            "prerequisites": prereq,
            "import": import_result,
            "armature_object_name": armature_object_name,
            "error": "VRM Add-on not available",
            "export": {"skipped": True, "reason": "setup-only"},
        }

    model_name = import_result.get("model_name") or import_result.get("file_name")
    mesh_names = import_result.get("mesh_object_names")

    if dry_run and skip_import:
        setup_result = setup_vrm1_on_armature(
            armature_object_name,
            dry_run=True,
            model_name=model_name,
            mesh_object_names=mesh_names,
            include_fingers=include_fingers,
        )
        return {
            "skill": "mmd-pmx-to-vrm1",
            "dry_run": True,
            "applied": False,
            "prerequisites": prereq,
            "import": import_result,
            "setup": setup_result,
            "armature_object_name": armature_object_name,
            "export": {"skipped": True, "reason": "setup-only"},
        }

    setup_result = setup_vrm1_on_armature(
        armature_object_name,
        dry_run=False,
        model_name=model_name,
        mesh_object_names=mesh_names,
        include_fingers=include_fingers,
    )

    audit = audit_vrm1_setup(armature_object_name)

    return {
        "skill": "mmd-pmx-to-vrm1",
        "dry_run": False,
        "applied": bool(setup_result.get("applied")),
        "prerequisites": prereq,
        "import": import_result,
        "setup": setup_result,
        "audit": audit,
        "armature_object_name": armature_object_name,
        "root_object_name": import_result.get("root_object_name"),
        "mesh_object_names": mesh_names,
        "vrm1_ready": audit.get("vrm1_ready"),
        "shapekeys_untouched": setup_result.get("shapekeys_untouched"),
        "export": {
            "skipped": True,
            "reason": "Skill is setup-only; export .vrm manually via VRM Add-on when ready.",
        },
    }


if __name__ == "__main__":
    result = run_pmx_to_vrm1_setup(dry_run=True)
