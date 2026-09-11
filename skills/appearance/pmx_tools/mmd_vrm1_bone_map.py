"""
MMD JP/EN bone name → VRM1 humanoid slot fallback map.

Keep bone names; only fill empty VRM1 slots.

Covers:
- Classic JP: 左腕 / 右腕
- mmd_tools rename_bones (Blender L/R): 腕.L / 腕.R
- Underscore L/R: 腕_L / Arm_L
- English display names
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# VRM1 slot attribute on human_bones → candidate bone names (exact match)
# Prefer classic JP, then mmd_tools .L/.R JP base, then EN.
HUMAN_BONE_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "hips": (
        "下半身",
        "Lower Body",
        "LowerBody",
        "Lower_Body",
        "Hip",
        "Hips",
        "腰",
        "センター",
        "Center",
    ),
    "spine": (
        "上半身",
        "Upper Body",
        "UpperBody",
        "Upper_Body",
        "Spine",
    ),
    "chest": (
        "上半身2",
        "上半身２",
        "Upper Body 2",
        "UpperBody2",
        "Upper_Body_2",
        "Chest",
    ),
    "upper_chest": (
        "上半身3",
        "上半身３",
        "Upper Body 3",
        "UpperBody3",
        "Upper_Body_3",
        "UpperChest",
        "Upper Chest",
    ),
    "neck": ("首", "Neck"),
    "head": ("頭", "Head"),
    "jaw": ("あご", "顎", "Jaw"),
    "left_eye": (
        "左目",
        "目.L",
        "目_L",
        "Eye_L",
        "Eye.L",
        "EyeL",
        "Left Eye",
        "LeftEye",
    ),
    "right_eye": (
        "右目",
        "目.R",
        "目_R",
        "Eye_R",
        "Eye.R",
        "EyeR",
        "Right Eye",
        "RightEye",
    ),
    "left_shoulder": (
        "左肩",
        "肩.L",
        "肩_L",
        "Shoulder_L",
        "Shoulder.L",
        "ShoulderL",
        "Left Shoulder",
        "LeftShoulder",
    ),
    "left_upper_arm": (
        "左腕",
        "腕.L",
        "腕_L",
        "Arm_L",
        "Arm.L",
        "ArmL",
        "Left Arm",
        "LeftArm",
        "LeftUpperArm",
    ),
    "left_lower_arm": (
        "左ひじ",
        "左肘",
        "ひじ.L",
        "肘.L",
        "ひじ_L",
        "肘_L",
        "Elbow_L",
        "Elbow.L",
        "ElbowL",
        "Left Elbow",
        "LeftElbow",
        "LeftLowerArm",
    ),
    "left_hand": (
        "左手首",
        "手首.L",
        "手首_L",
        "Wrist_L",
        "Wrist.L",
        "WristL",
        "Hand_L",
        "Hand.L",
        "Left Wrist",
        "LeftWrist",
        "LeftHand",
    ),
    "right_shoulder": (
        "右肩",
        "肩.R",
        "肩_R",
        "Shoulder_R",
        "Shoulder.R",
        "ShoulderR",
        "Right Shoulder",
        "RightShoulder",
    ),
    "right_upper_arm": (
        "右腕",
        "腕.R",
        "腕_R",
        "Arm_R",
        "Arm.R",
        "ArmR",
        "Right Arm",
        "RightArm",
        "RightUpperArm",
    ),
    "right_lower_arm": (
        "右ひじ",
        "右肘",
        "ひじ.R",
        "肘.R",
        "ひじ_R",
        "肘_R",
        "Elbow_R",
        "Elbow.R",
        "ElbowR",
        "Right Elbow",
        "RightElbow",
        "RightLowerArm",
    ),
    "right_hand": (
        "右手首",
        "手首.R",
        "手首_R",
        "Wrist_R",
        "Wrist.R",
        "WristR",
        "Hand_R",
        "Hand.R",
        "Right Wrist",
        "RightWrist",
        "RightHand",
    ),
    "left_upper_leg": (
        "左足",
        "足.L",
        "足_L",
        "Leg_L",
        "Leg.L",
        "LegL",
        "Left Leg",
        "LeftLeg",
        "LeftUpperLeg",
    ),
    "left_lower_leg": (
        "左ひざ",
        "左膝",
        "ひざ.L",
        "膝.L",
        "ひざ_L",
        "膝_L",
        "Knee_L",
        "Knee.L",
        "KneeL",
        "Left Knee",
        "LeftKnee",
        "LeftLowerLeg",
    ),
    "left_foot": (
        "左足首",
        "足首.L",
        "足首_L",
        "Ankle_L",
        "Ankle.L",
        "AnkleL",
        "Foot_L",
        "Foot.L",
        "Left Ankle",
        "LeftAnkle",
        "LeftFoot",
    ),
    "left_toes": (
        "左つま先",
        "左つま先先",
        "つま先.L",
        "つま先先.L",
        "つま先_L",
        "Toe_L",
        "Toe.L",
        "Toes_L",
        "Toes.L",
        "Left Toe",
        "LeftToes",
    ),
    "right_upper_leg": (
        "右足",
        "足.R",
        "足_R",
        "Leg_R",
        "Leg.R",
        "LegR",
        "Right Leg",
        "RightLeg",
        "RightUpperLeg",
    ),
    "right_lower_leg": (
        "右ひざ",
        "右膝",
        "ひざ.R",
        "膝.R",
        "ひざ_R",
        "膝_R",
        "Knee_R",
        "Knee.R",
        "KneeR",
        "Right Knee",
        "RightKnee",
        "RightLowerLeg",
    ),
    "right_foot": (
        "右足首",
        "足首.R",
        "足首_R",
        "Ankle_R",
        "Ankle.R",
        "AnkleR",
        "Foot_R",
        "Foot.R",
        "Right Ankle",
        "RightAnkle",
        "RightFoot",
    ),
    "right_toes": (
        "右つま先",
        "右つま先先",
        "つま先.R",
        "つま先先.R",
        "つま先_R",
        "Toe_R",
        "Toe.R",
        "Toes_R",
        "Toes.R",
        "Right Toe",
        "RightToes",
    ),
}

# Optional finger map: classic 左/右 + mmd_tools base+suffix (親指０.L)
FINGER_MAP: Dict[str, Tuple[str, ...]] = {
    "left_thumb_metacarpal": (
        "左親指０",
        "左親指0",
        "親指０.L",
        "親指0.L",
        "親指０_L",
        "Thumb0_L",
        "Thumb0.L",
    ),
    "left_thumb_proximal": (
        "左親指１",
        "左親指1",
        "親指１.L",
        "親指1.L",
        "親指１_L",
        "Thumb1_L",
        "Thumb1.L",
    ),
    "left_thumb_distal": (
        "左親指２",
        "左親指2",
        "親指２.L",
        "親指2.L",
        "親指２_L",
        "Thumb2_L",
        "Thumb2.L",
    ),
    "left_index_proximal": (
        "左人指１",
        "左人差指１",
        "人指１.L",
        "人差指１.L",
        "人指1.L",
        "Index1_L",
        "Index1.L",
    ),
    "left_index_intermediate": (
        "左人指２",
        "左人差指２",
        "人指２.L",
        "人差指２.L",
        "人指2.L",
        "Index2_L",
        "Index2.L",
    ),
    "left_index_distal": (
        "左人指３",
        "左人差指３",
        "人指３.L",
        "人差指３.L",
        "人指3.L",
        "Index3_L",
        "Index3.L",
    ),
    "left_middle_proximal": (
        "左中指１",
        "中指１.L",
        "中指1.L",
        "Middle1_L",
        "Middle1.L",
    ),
    "left_middle_intermediate": (
        "左中指２",
        "中指２.L",
        "中指2.L",
        "Middle2_L",
        "Middle2.L",
    ),
    "left_middle_distal": (
        "左中指３",
        "中指３.L",
        "中指3.L",
        "Middle3_L",
        "Middle3.L",
    ),
    "left_ring_proximal": (
        "左薬指１",
        "薬指１.L",
        "薬指1.L",
        "Ring1_L",
        "Ring1.L",
    ),
    "left_ring_intermediate": (
        "左薬指２",
        "薬指２.L",
        "薬指2.L",
        "Ring2_L",
        "Ring2.L",
    ),
    "left_ring_distal": (
        "左薬指３",
        "薬指３.L",
        "薬指3.L",
        "Ring3_L",
        "Ring3.L",
    ),
    "left_little_proximal": (
        "左小指１",
        "小指１.L",
        "小指1.L",
        "Little1_L",
        "Little1.L",
        "Pinky1_L",
    ),
    "left_little_intermediate": (
        "左小指２",
        "小指２.L",
        "小指2.L",
        "Little2_L",
        "Little2.L",
        "Pinky2_L",
    ),
    "left_little_distal": (
        "左小指３",
        "小指３.L",
        "小指3.L",
        "Little3_L",
        "Little3.L",
        "Pinky3_L",
    ),
    "right_thumb_metacarpal": (
        "右親指０",
        "右親指0",
        "親指０.R",
        "親指0.R",
        "親指０_R",
        "Thumb0_R",
        "Thumb0.R",
    ),
    "right_thumb_proximal": (
        "右親指１",
        "右親指1",
        "親指１.R",
        "親指1.R",
        "親指１_R",
        "Thumb1_R",
        "Thumb1.R",
    ),
    "right_thumb_distal": (
        "右親指２",
        "右親指2",
        "親指２.R",
        "親指2.R",
        "親指２_R",
        "Thumb2_R",
        "Thumb2.R",
    ),
    "right_index_proximal": (
        "右人指１",
        "右人差指１",
        "人指１.R",
        "人差指１.R",
        "人指1.R",
        "Index1_R",
        "Index1.R",
    ),
    "right_index_intermediate": (
        "右人指２",
        "右人差指２",
        "人指２.R",
        "人差指２.R",
        "人指2.R",
        "Index2_R",
        "Index2.R",
    ),
    "right_index_distal": (
        "右人指３",
        "右人差指３",
        "人指３.R",
        "人差指３.R",
        "人指3.R",
        "Index3_R",
        "Index3.R",
    ),
    "right_middle_proximal": (
        "右中指１",
        "中指１.R",
        "中指1.R",
        "Middle1_R",
        "Middle1.R",
    ),
    "right_middle_intermediate": (
        "右中指２",
        "中指２.R",
        "中指2.R",
        "Middle2_R",
        "Middle2.R",
    ),
    "right_middle_distal": (
        "右中指３",
        "中指３.R",
        "中指3.R",
        "Middle3_R",
        "Middle3.R",
    ),
    "right_ring_proximal": (
        "右薬指１",
        "薬指１.R",
        "薬指1.R",
        "Ring1_R",
        "Ring1.R",
    ),
    "right_ring_intermediate": (
        "右薬指２",
        "薬指２.R",
        "薬指2.R",
        "Ring2_R",
        "Ring2.R",
    ),
    "right_ring_distal": (
        "右薬指３",
        "薬指３.R",
        "薬指3.R",
        "Ring3_R",
        "Ring3.R",
    ),
    "right_little_proximal": (
        "右小指１",
        "小指１.R",
        "小指1.R",
        "Little1_R",
        "Little1.R",
        "Pinky1_R",
    ),
    "right_little_intermediate": (
        "右小指２",
        "小指２.R",
        "小指2.R",
        "Little2_R",
        "Little2.R",
        "Pinky2_R",
    ),
    "right_little_distal": (
        "右小指３",
        "小指３.R",
        "小指3.R",
        "Little3_R",
        "Little3.R",
        "Pinky3_R",
    ),
}

REQUIRED_HUMAN_BONES: Tuple[str, ...] = (
    "hips",
    "spine",
    "head",
    "left_upper_arm",
    "left_lower_arm",
    "left_hand",
    "right_upper_arm",
    "right_lower_arm",
    "right_hand",
    "left_upper_leg",
    "left_lower_leg",
    "left_foot",
    "right_upper_leg",
    "right_lower_leg",
    "right_foot",
)


def _bone_name_set(armature) -> Set[str]:
    return {b.name for b in armature.data.bones}


_EN_SUFFIX_RE = re.compile(r"\s+\([^)]*\)\s*$")


def _strip_english_suffix(name: str) -> str:
    return _EN_SUFFIX_RE.sub("", name).rstrip()


def _bone_lookup(armature) -> Dict[str, str]:
    """Map bare / glossed name → actual bone name (prefer exact)."""
    lookup: Dict[str, str] = {}
    for b in armature.data.bones:
        lookup[b.name] = b.name
        bare = _strip_english_suffix(b.name)
        # First bare wins only if not already set to exact
        lookup.setdefault(bare, b.name)
    return lookup


def _match_candidate(bone_lookup: Dict[str, str], candidates: Sequence[str]) -> Optional[str]:
    for name in candidates:
        hit = bone_lookup.get(name)
        if hit:
            return hit
    # Case-insensitive EN fallback on bare names
    lower_map = {k.lower(): v for k, v in bone_lookup.items()}
    for name in candidates:
        hit = lower_map.get(name.lower())
        if hit:
            return hit
    return None


def plan_fallback_humanoid(
    armature,
    *,
    include_fingers: bool = True,
    only_empty: bool = True,
) -> Dict[str, Any]:
    """
    Plan slot → bone_name assignments from the static map.
    Does not write. If only_empty, skip slots that already have a bone_name.
    """
    bone_lookup = _bone_lookup(armature)
    bone_names = set(bone_lookup.values())
    ext = getattr(armature.data, "vrm_addon_extension", None)
    human_bones = None
    if ext is not None:
        try:
            human_bones = ext.vrm1.humanoid.human_bones
        except Exception:
            human_bones = None

    maps = dict(HUMAN_BONE_CANDIDATES)
    if include_fingers:
        maps.update(FINGER_MAP)

    planned: Dict[str, str] = {}
    unmatched: List[str] = []
    skipped_filled: List[str] = []

    for slot, candidates in maps.items():
        current = ""
        if human_bones is not None and hasattr(human_bones, slot):
            try:
                current = getattr(human_bones, slot).node.bone_name or ""
            except Exception:
                current = ""
        if only_empty and current:
            skipped_filled.append(slot)
            continue
        match = _match_candidate(bone_lookup, candidates)
        if match:
            planned[slot] = match
        else:
            unmatched.append(slot)

    required_missing: List[str] = []
    for s in REQUIRED_HUMAN_BONES:
        if s in planned or s in skipped_filled:
            continue
        current = ""
        if human_bones is not None and hasattr(human_bones, s):
            try:
                current = getattr(human_bones, s).node.bone_name or ""
            except Exception:
                current = ""
        if not current:
            required_missing.append(s)

    return {
        "planned": planned,
        "unmatched_slots": unmatched,
        "skipped_already_filled": skipped_filled,
        "required_missing_after_plan": required_missing,
        "bone_count": len(bone_names),
    }


def apply_fallback_humanoid(
    armature,
    *,
    include_fingers: bool = True,
    only_empty: bool = True,
    dry_run: bool = True,
) -> Dict[str, Any]:
    plan = plan_fallback_humanoid(
        armature, include_fingers=include_fingers, only_empty=only_empty
    )
    if dry_run:
        return {
            "dry_run": True,
            "applied": False,
            **plan,
            "message": f"Would assign {len(plan['planned'])} humanoid slots via fallback map.",
        }

    ext = armature.data.vrm_addon_extension
    ext.spec_version = "1.0"
    human_bones = ext.vrm1.humanoid.human_bones
    assigned: Dict[str, str] = {}
    errors: List[str] = []

    for slot, bone_name in plan["planned"].items():
        if not hasattr(human_bones, slot):
            errors.append(f"unknown_slot:{slot}")
            continue
        try:
            getattr(human_bones, slot).node.bone_name = bone_name
            assigned[slot] = bone_name
        except Exception as exc:
            errors.append(f"{slot}:{exc}")

    return {
        "dry_run": False,
        "applied": len(assigned) > 0 or not plan["planned"],
        "assigned": assigned,
        "assigned_count": len(assigned),
        "errors": errors,
        **plan,
    }
