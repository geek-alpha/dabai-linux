#!/usr/bin/env python3
"""
Mixamo 动作库批量下载 & 重命名工具

功能：
  1. 从 animation-library.json 生成 Mixamo 下载清单（Markdown 格式，含搜索名）
  2. 扫描下载目录中的 FBX 文件，自动匹配并重命名到对应分类目录
  3. 支持模糊匹配（文件名中的空格/大小写/连字符差异）
  4. 生成下载进度报告

用法：
  # 生成下载清单
  python mixamo_download_tool.py --generate-list

  # 扫描下载目录并重命名
  python mixamo_download_tool.py --scan "C:/Users/xxx/Downloads"

  # 扫描并复制到动作库目录（不移动原文件）
  python mixamo_download_tool.py --scan "C:/Users/xxx/Downloads" --copy

  # 查看当前动作库状态
  python mixamo_download_tool.py --status

  # 生成 Mixamo 批量下载 URL 列表（需登录后手动访问）
  python mixamo_download_tool.py --urls
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from difflib import SequenceMatcher

# ==================== 配置 ====================
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / ".." / "web" / "anim" / "animation-library.json"
ANIM_DIR = SCRIPT_DIR / ".." / "web" / "anim"

# Mixamo 常见的文件名变体映射
# key = 配置中的文件名（不含路径和.fbx），value = 可能的 Mixamo 下载文件名模式
FILENAME_ALIASES = {
    # idle
    "Idle": ["Idle", "Idle Breathing", "Breathing Idle", "Idle Animation"],
    "Idle_Happy": ["Idle Happy", "Happy Idle", "Cheerful Idle"],
    "Idle_Sad": ["Idle Sad", "Sad Idle", "Depressed Idle"],
    "Idle_Angry": ["Idle Angry", "Angry Idle", "Furious Idle"],
    "Excited_Idle": ["Excited Idle", "Idle Excited", "Energetic Idle"],
    "Shy_Idle": ["Shy Idle", "Idle Shy", "Nervous Idle"],
    "Tired_Idle": ["Tired Idle", "Idle Tired", "Exhausted Idle", "Sleepy Idle"],
    "Thinking_Idle": ["Thinking Idle", "Idle Thinking", "Thoughtful Idle"],
    # gesture
    "Waving": ["Waving", "Wave", "Hand Wave", "Hello Wave"],
    "Clapping": ["Clapping", "Clap", "Applause", "Clap Hands"],
    "Pointing": ["Pointing", "Point", "Point Forward", "Pointing Finger"],
    "Thumbs_Up": ["Thumbs Up", "ThumbsUp", "Thumb Up", "Like", "Thumbs Up Gesture"],
    "No_Gesture": ["No Gesture", "No", "Reject", "Rejecting", "Wave No"],
    "Yes_Gesture": ["Yes Gesture", "Yes", "Nodding", "Nod Yes", "Agree"],
    "Come_Here": ["Come Here", "Come", "Calling", "Beckoning", "Come Here Gesture"],
    "Stop_Gesture": ["Stop Gesture", "Stop", "Halt", "Stop Hand", "Stop Sign"],
    "Hand_Heart": ["Hand Heart", "Heart Hands", "Love Gesture", "Finger Heart", "Heart Gesture"],
    "Surrender": ["Surrender", "Surrendering", "Give Up", "Hands Up", "Surrender Gesture"],
    "Scratch_Head": ["Scratch Head", "Scratching Head", "Head Scratch", "Confused Scratch"],
    "Arms_Crossed": ["Arms Crossed", "Crossed Arms", "Arm Cross", "Cross Arms"],
    # emotion
    "Happy_Jump": ["Happy Jump", "Jumping Happy", "Joyful Jump", "Excited Jump", "Jump for Joy"],
    "Sad_Slump": ["Sad Slump", "Slump", "Defeated", "Disappointed", "Sad Reaction"],
    "Angry_Stomp": ["Angry Stomp", "Stomp", "Stomping", "Furious Stomp", "Angry Reaction"],
    "Surprise": ["Surprise", "Surprised", "Shocked", "Surprise Reaction", "Startled"],
    "Victory": ["Victory", "Victory Dance", "Win", "Celebrate", "Winner"],
    "Bow": ["Bow", "Bowing", "Greeting Bow", "Respect Bow"],
    "Shy_Back": ["Shy Back", "Shy Reaction", "Embarrassed", "Shy Walk Back"],
    "Proud": ["Proud", "Proud Pose", "Confident Pose", "Standing Proud"],
    "Laughing": ["Laughing", "Laugh", "Laughter", "Happy Laugh"],
    "Crying": ["Crying", "Cry", "Sobbing", "Sad Cry"],
    "Sigh": ["Sigh", "Sighing", "Heavy Sigh", "Disappointed Sigh"],
    "Yawning": ["Yawning", "Yawn", "Sleepy Yawn", "Tired Yawn"],
    "Dancing_Happy": ["Dancing Happy", "Happy Dance", "Cheerful Dance", "Dance Happy"],
    "Taunt": ["Taunt", "Taunting", "Teasing", "Mocking", "Provoke"],
    "Blow_Kiss": ["Blow Kiss", "Blowing Kiss", "Kiss", "Flying Kiss", "Air Kiss"],
    # walk
    "Walking": ["Walking", "Walk", "Walk Forward", "Normal Walk"],
    "Walking_Happy": ["Walking Happy", "Happy Walk", "Cheerful Walk", "Walk Happy"],
    "Walking_Sad": ["Walking Sad", "Sad Walk", "Depressed Walk", "Walk Sad"],
    "Walking_Angry": ["Walking Angry", "Angry Walk", "Furious Walk", "Walk Angry", "Stomp Walk"],
    "Running": ["Running", "Run", "Run Forward", "Fast Run"],
    "Walking_Backwards": ["Walking Backwards", "Walk Back", "Backward Walk", "Walk Backwards"],
    "Turn_Left": ["Turn Left", "Left Turn", "Turning Left"],
    "Turn_Right": ["Turn Right", "Right Turn", "Turning Right"],
    # dance
    "Silly_Dancing": ["Silly Dancing", "Silly Dance", "Funny Dance", "Goofy Dance"],
    "Hip_Hop_Dancing": ["Hip Hop Dancing", "Hip Hop Dance", "HipHop", "Hip Hop"],
    "Twist_Dance": ["Twist Dance", "Twist", "Twisting", "Twist Dancing"],
    "Macarena": ["Macarena", "Macarena Dance", "Macarena Dancing"],
    "Chicken_Dance": ["Chicken Dance", "Dancing Chicken", "Chicken Song Dance"],
    "Celebration_Dance": ["Celebration Dance", "Celebration", "Celebrating", "Party Dance"],
    # pose
    "Confident_Pose": ["Confident Pose", "Confident", "Confident Standing", "Power Pose"],
    "Cool_Pose": ["Cool Pose", "Cool", "Cool Standing", "Stylish Pose"],
    "Sexy_Pose": ["Sexy Pose", "Sexy", "Attractive Pose", "Model Pose"],
    "Shy_Pose": ["Shy Pose", "Shy Standing", "Embarrassed Pose"],
    "Sitting_On_Chair": ["Sitting On Chair", "Sitting Chair", "Chair Sit", "Sit Chair"],
    "Sitting_On_Floor": ["Sitting On Floor", "Sitting Floor", "Floor Sit", "Sit Floor", "Cross Legged Sit"],
    "Kneeling": ["Kneeling", "Kneel", "Kneel Down", "On Knees"],
    "Lying_Down": ["Lying Down", "Lie Down", "Lying", "Sleeping", "Lay Down"],
}


def normalize_name(name: str) -> str:
    """标准化文件名用于匹配：去空格、下划线、连字符，统一小写"""
    return re.sub(r'[\s_\-\.]', '', name.lower())


def similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度"""
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def load_config() -> dict:
    """加载动作库配置"""
    if not CONFIG_PATH.exists():
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_all_animations(config: dict) -> list:
    """获取所有动作的扁平列表，附带分类信息"""
    result = []
    for cat_key, cat in config.get('categories', {}).items():
        for anim in cat.get('animations', []):
            result.append({
                **anim,
                'category': cat_key,
                'category_label': cat.get('label', cat_key),
            })
    return result


def find_matching_config(fbx_filename: str, all_anims: list) -> dict | None:
    """
    根据下载的 FBX 文件名匹配配置中的动作
    返回匹配的配置项或 None
    """
    base_name = Path(fbx_filename).stem  # 去掉 .fbx

    best_match = None
    best_score = 0

    for anim in all_anims:
        # 配置中的文件名（如 idle/Idle.fbx → Idle）
        config_file = Path(anim['file']).stem

        # 直接匹配
        if normalize_name(base_name) == normalize_name(config_file):
            return anim

        # 别名匹配
        aliases = FILENAME_ALIASES.get(config_file, [config_file])
        for alias in aliases:
            score = similarity(base_name, alias)
            if score > best_score:
                best_score = score
                best_match = anim

    # 相似度阈值 0.6 以上认为匹配
    if best_score >= 0.6:
        return best_match
    return None


def cmd_generate_list(config: dict):
    """生成下载清单（Markdown 格式）"""
    all_anims = get_all_animations(config)
    categories = config.get('categories', {})

    print(f"\n{'='*60}")
    print(f"  Mixamo 动作下载清单（共 {len(all_anims)} 个动作）")
    print(f"{'='*60}\n")

    for cat_key, cat in categories.items():
        anims = cat.get('animations', [])
        print(f"\n## {cat.get('label', cat_key)}（{len(anims)} 个）")
        if cat.get('description'):
            print(f"   _{cat['description']}_\n")

        for i, anim in enumerate(anims, 1):
            file_name = Path(anim['file']).stem
            search_hint = FILENAME_ALIASES.get(file_name, [file_name])[0]
            emotion = anim.get('emotion', '-')
            loop = '循环' if anim.get('loop') else '单次'
            desc = anim.get('description', '')

            print(f"  {i:2d}. **{anim['name']}**")
            print(f"      Mixamo 搜索: `{search_hint}`")
            print(f"      保存路径: `{anim['file']}`")
            print(f"      情绪: {emotion} | 类型: {loop}")
            if desc:
                print(f"      说明: {desc}")
            print()

    # 统计
    total = len(all_anims)
    loop_count = sum(1 for a in all_anims if a.get('loop'))
    print(f"\n{'─'*60}")
    print(f"  总计: {total} 个动作（{loop_count} 个循环, {total - loop_count} 个单次）")
    print(f"  分类: {len(categories)} 个")
    print(f"{'─'*60}\n")

    # 保存为文件
    output_path = SCRIPT_DIR / ".." / "web" / "anim" / "DOWNLOAD_CHECKLIST.md"
    # （此处省略生成文件的详细 Markdown，上面的输出已足够）
    print(f"[提示] 访问 https://www.mixamo.com/ 搜索并下载 FBX 格式动作")
    print(f"[提示] 下载后放到任意目录，运行 --scan 自动归类重命名\n")


def cmd_scan(config: dict, scan_dir: str, move: bool = True):
    """扫描下载目录并重命名到动作库"""
    scan_path = Path(scan_dir).expanduser().resolve()
    if not scan_path.exists():
        print(f"[错误] 扫描目录不存在: {scan_path}")
        sys.exit(1)

    all_anims = get_all_animations(config)

    # 找到所有 FBX 文件
    fbx_files = list(scan_path.rglob("*.fbx"))
    if not fbx_files:
        print(f"[提示] 在 {scan_path} 中未找到 .fbx 文件")
        return

    print(f"\n{'='*60}")
    print(f"  扫描目录: {scan_path}")
    print(f"  找到 FBX 文件: {len(fbx_files)} 个")
    print(f"  模式: {'移动' if move else '复制'}到动作库目录")
    print(f"{'='*60}\n")

    matched = 0
    unmatched = []
    already = 0

    for fbx_file in fbx_files:
        match = find_matching_config(fbx_file.name, all_anims)
        if not match:
            unmatched.append(fbx_file.name)
            print(f"  ✗ {fbx_file.name}  →  无法匹配")
            continue

        # 目标路径
        target_rel = match['file']
        target_path = ANIM_DIR / target_rel

        # 检查是否已存在
        if target_path.exists():
            already += 1
            print(f"  ≈ {fbx_file.name}  →  {target_rel}（已存在，跳过）")
            continue

        # 确保目标目录存在
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # 移动或复制
        if move:
            shutil.move(str(fbx_file), str(target_path))
        else:
            shutil.copy2(str(fbx_file), str(target_path))

        matched += 1
        action = '移动' if move else '复制'
        print(f"  ✓ {fbx_file.name}  →  {target_rel} [{action}]")

    print(f"\n{'─'*60}")
    print(f"  匹配成功: {matched} 个")
    print(f"  已存在跳过: {already} 个")
    print(f"  无法匹配: {len(unmatched)} 个")
    if unmatched:
        print(f"\n  无法匹配的文件:")
        for name in unmatched:
            print(f"    - {name}")
    print(f"{'─'*60}\n")


def cmd_status(config: dict):
    """查看当前动作库状态"""
    all_anims = get_all_animations(config)
    categories = config.get('categories', {})

    print(f"\n{'='*60}")
    print(f"  动作库状态")
    print(f"{'='*60}\n")

    total = len(all_anims)
    have_files = 0
    missing = []

    for cat_key, cat in categories.items():
        cat_have = 0
        cat_total = len(cat.get('animations', []))
        for anim in cat.get('animations', []):
            file_path = ANIM_DIR / anim['file']
            if file_path.exists():
                cat_have += 1
                have_files += 1
            else:
                missing.append(anim['name'])

        pct = (cat_have / cat_total * 100) if cat_total > 0 else 0
        bar = '█' * int(pct / 10) + '░' * (10 - int(pct / 10))
        print(f"  {cat.get('label', cat_key):<10s} {bar}  {cat_have:2d}/{cat_total:2d}  ({pct:5.1f}%)")

    pct = (have_files / total * 100) if total > 0 else 0
    print(f"\n  总计: {have_files}/{total} 个文件已就位 ({pct:.1f}%)")

    if missing:
        print(f"\n  缺少的动作（{len(missing)} 个）:")
        for name in sorted(missing):
            print(f"    - {name}")
    print(f"\n{'─'*60}\n")


def cmd_urls(config: dict):
    """生成 Mixamo 搜索 URL 列表"""
    all_anims = get_all_animations(config)

    print(f"\n{'='*60}")
    print(f"  Mixamo 搜索链接（共 {len(all_anims)} 个）")
    print(f"  注意：需要先登录 mixamo.com，然后点击链接搜索")
    print(f"{'='*60}\n")

    categories = config.get('categories', {})
    for cat_key, cat in categories.items():
        print(f"\n## {cat.get('label', cat_key)}")
        for anim in cat.get('animations', []):
            file_name = Path(anim['file']).stem
            search_term = FILENAME_ALIASES.get(file_name, [file_name])[0]
            url = f"https://www.mixamo.com/#/?query={search_term.replace(' ', '+')}"
            print(f"  [{anim['name']}] {url}")

    print(f"\n[提示] 下载时选择 FBX 格式，Skin: Without Skin\n")


def main():
    parser = argparse.ArgumentParser(
        description='Mixamo 动作库批量下载 & 重命名工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成下载清单
  python mixamo_download_tool.py --generate-list

  # 扫描下载目录并移动文件到动作库
  python mixamo_download_tool.py --scan "C:/Users/xxx/Downloads"

  # 扫描并复制（保留原文件）
  python mixamo_download_tool.py --scan "C:/Users/xxx/Downloads" --copy

  # 查看当前动作库文件就位情况
  python mixamo_download_tool.py --status

  # 生成 Mixamo 搜索链接列表
  python mixamo_download_tool.py --urls
        """
    )
    parser.add_argument('--generate-list', action='store_true', help='生成下载清单')
    parser.add_argument('--scan', type=str, metavar='DIR', help='扫描指定目录中的 FBX 文件并重命名')
    parser.add_argument('--copy', action='store_true', help='扫描时复制而非移动文件')
    parser.add_argument('--status', action='store_true', help='查看动作库文件就位状态')
    parser.add_argument('--urls', action='store_true', help='生成 Mixamo 搜索链接列表')

    args = parser.parse_args()

    config = load_config()

    if args.generate_list:
        cmd_generate_list(config)
    elif args.scan:
        cmd_scan(config, args.scan, move=not args.copy)
    elif args.status:
        cmd_status(config)
    elif args.urls:
        cmd_urls(config)
    else:
        parser.print_help()
        print("\n[提示] 未指定操作，默认显示当前状态\n")
        cmd_status(config)


if __name__ == '__main__':
    main()
