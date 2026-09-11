"""
Import a .pmx / .pmd via mmd_tools and locate the MMD hierarchy.

    result = import_pmx(r"D:\\path\\model.pmx", dry_run=True)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import bpy

DEFAULT_IMPORT_KWARGS: Dict[str, Any] = {
    "scale": 0.08,
    "clean_model": True,
    "remove_doubles": False,
    "fix_bone_order": True,
    "fix_ik_links": False,
    "apply_bone_fixed_axis": False,
    "rename_bones": True,
    "use_underscore": False,
    "use_mipmap": True,
    "log_level": "INFO",
    "save_log": False,
}

DEFAULT_TYPES = {"MESH", "ARMATURE", "PHYSICS", "DISPLAY", "MORPHS"}

DRY_RUN = True


def list_pmx_files(directory: str) -> dict:
    root = Path(directory).expanduser()
    if not root.is_dir():
        return {
            "phase": "import",
            "error": f"Not a directory: {directory}",
            "files": [],
        }
    files = sorted(
        str(p.resolve())
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in {".pmx", ".pmd"}
    )
    return {
        "phase": "import",
        "directory": str(root.resolve()),
        "count": len(files),
        "files": files,
        "file_names": [Path(f).name for f in files],
    }


def resolve_pmx_path(
    filepath: Optional[str] = None,
    directory: Optional[str] = None,
    filename: Optional[str] = None,
) -> dict:
    if filepath:
        path = Path(filepath).expanduser().resolve()
        if path.suffix.lower() not in {".pmx", ".pmd"}:
            return {"error": f"Not a .pmx/.pmd file: {filepath}", "filepath": None}
        if not path.is_file():
            return {"error": f"File not found: {filepath}", "filepath": None}
        return {"filepath": str(path), "file_name": path.name}

    if directory:
        listing = list_pmx_files(directory)
        if listing.get("error"):
            return listing
        files = listing["files"]
        if not files:
            return {"error": f"No .pmx/.pmd files in {directory}", "filepath": None}
        if filename:
            matches = [f for f in files if Path(f).name == filename]
            if not matches:
                return {
                    "error": f"{filename!r} not found in directory",
                    "filepath": None,
                    "available": listing["file_names"],
                }
            return {"filepath": matches[0], "file_name": filename}
        if len(files) == 1:
            return {"filepath": files[0], "file_name": Path(files[0]).name}
        return {
            "error": "multiple_pmx_files",
            "message": "Multiple .pmx/.pmd files; user must pick one.",
            "filepath": None,
            "files": files,
            "file_names": listing["file_names"],
        }

    return {"error": "Provide filepath or directory", "filepath": None}


def _new_empty_blend() -> None:
    bpy.ops.wm.read_homefile(use_empty=True)


def _objects_snapshot() -> Set[str]:
    return {obj.name for obj in bpy.data.objects}


def _walk_children(obj: bpy.types.Object) -> List[bpy.types.Object]:
    out = [obj]
    for child in obj.children:
        out.extend(_walk_children(child))
    return out


def find_mmd_hierarchy(
    object_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Locate mmd_tools ROOT empty + armature + meshes.
    If object_names given, prefer roots among those names (post-import).
    """
    candidates = (
        [bpy.data.objects[n] for n in object_names if n in bpy.data.objects]
        if object_names is not None
        else list(bpy.data.objects)
    )

    roots: List[bpy.types.Object] = []
    for obj in candidates:
        if getattr(obj, "mmd_type", None) == "ROOT":
            roots.append(obj)

    # Fallback: search all objects if new-object list missed root
    if not roots and object_names is not None:
        for obj in bpy.data.objects:
            if getattr(obj, "mmd_type", None) == "ROOT":
                roots.append(obj)

    if not roots:
        # Heuristic: armature among new objects
        armatures = [o for o in candidates if o.type == "ARMATURE"]
        meshes = [o.name for o in candidates if o.type == "MESH"]
        arm = armatures[0] if armatures else None
        return {
            "root_object_name": None,
            "armature_object_name": arm.name if arm else None,
            "mesh_object_names": meshes,
            "mmd_root_found": False,
            "model_name": arm.name if arm else None,
        }

    root = roots[0]
    tree = _walk_children(root)
    armature = next(
        (o for o in tree if o.type == "ARMATURE"),
        None,
    )
    meshes = [o.name for o in tree if o.type == "MESH"]

    model_name = root.name
    try:
        # mmd_root.name often holds display name
        mr = getattr(root, "mmd_root", None)
        if mr is not None and getattr(mr, "name", None):
            model_name = mr.name or root.name
    except Exception:
        pass

    return {
        "root_object_name": root.name,
        "armature_object_name": armature.name if armature else None,
        "mesh_object_names": meshes,
        "mmd_root_found": True,
        "model_name": model_name,
        "root_count": len(roots),
    }


def mmd_tools_available() -> bool:
    return hasattr(bpy.ops, "mmd_tools") and hasattr(bpy.ops.mmd_tools, "import_model")


def import_pmx(
    filepath: str,
    new_file: bool = False,
    dry_run: bool = DRY_RUN,
    scale: float = 0.08,
    import_kwargs: Optional[Dict[str, Any]] = None,
    types: Optional[Set[str]] = None,
) -> dict:
    resolved = resolve_pmx_path(filepath=filepath)
    if resolved.get("error"):
        return {"phase": "import", "skipped": True, **resolved}

    pmx_path = resolved["filepath"]
    kwargs = {**DEFAULT_IMPORT_KWARGS, **(import_kwargs or {})}
    kwargs["scale"] = scale
    type_set = set(types) if types is not None else set(DEFAULT_TYPES)

    if dry_run:
        return {
            "phase": "import",
            "dry_run": True,
            "skipped": False,
            "filepath": pmx_path,
            "file_name": resolved["file_name"],
            "new_file": new_file,
            "scale": scale,
            "types": sorted(type_set),
            "import_kwargs": {k: v for k, v in kwargs.items() if k != "filepath"},
            "mmd_tools_available": mmd_tools_available(),
            "message": f"Would import {resolved['file_name']!r} via mmd_tools.import_model.",
        }

    if not mmd_tools_available():
        return {
            "phase": "import",
            "error": "bpy.ops.mmd_tools.import_model not available — enable MMD Tools.",
            "applied": False,
        }

    before = _objects_snapshot()
    if new_file:
        _new_empty_blend()
        before = _objects_snapshot()

    op_kwargs = dict(kwargs)
    op_kwargs["filepath"] = pmx_path
    # Flag enum: pass as set
    op_kwargs["types"] = type_set

    try:
        op_result = bpy.ops.mmd_tools.import_model(**op_kwargs)
    except TypeError:
        # Older mmd_tools may not accept types as set — retry without types
        op_kwargs.pop("types", None)
        op_result = bpy.ops.mmd_tools.import_model(**op_kwargs)
    except Exception as exc:
        return {
            "phase": "import",
            "dry_run": False,
            "applied": False,
            "error": str(exc),
            "filepath": pmx_path,
        }

    applied = op_result == {"FINISHED"}
    after = _objects_snapshot()
    new_object_names = sorted(after - before)
    hierarchy = find_mmd_hierarchy(new_object_names)

    return {
        "phase": "import",
        "dry_run": False,
        "applied": applied,
        "operator_result": list(op_result) if op_result is not None else None,
        "filepath": pmx_path,
        "file_name": resolved["file_name"],
        "new_file": new_file,
        "scale": scale,
        "new_object_names": new_object_names,
        **hierarchy,
    }


def run_phase_import(
    filepath: Optional[str] = None,
    directory: Optional[str] = None,
    filename: Optional[str] = None,
    new_file: bool = False,
    dry_run: bool = DRY_RUN,
    scale: float = 0.08,
    import_kwargs: Optional[Dict[str, Any]] = None,
) -> dict:
    resolved = resolve_pmx_path(
        filepath=filepath, directory=directory, filename=filename
    )
    if resolved.get("error") == "multiple_pmx_files":
        return {
            "phase": "import",
            "skipped": True,
            "reason": "multiple_pmx_files",
            **resolved,
        }
    if resolved.get("error"):
        return {"phase": "import", "skipped": True, **resolved}

    return import_pmx(
        filepath=resolved["filepath"],
        new_file=new_file,
        dry_run=dry_run,
        scale=scale,
        import_kwargs=import_kwargs,
    )


if __name__ == "__main__":
    result = run_phase_import(dry_run=True)
