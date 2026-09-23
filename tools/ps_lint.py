#!/usr/bin/env python3
"""PowerShell / 批处理脚本的静态体检 —— 在没有 Windows 的机器上也能跑。

为什么需要它：这套仓库里 Windows 侧的脚本（dabai.bat、deploy/windows/*.ps1）
在 Linux 上没法执行，也没法用 pwsh 解析（多数机器没装）。结果是「改了但没验」，
错到大括号不配对时，只有到 Windows 上第一次运行才会炸 —— 而那时人往往不在机器前。

这个工具不假装是解释器，它只查三类「一定会让脚本崩」的低级错：
  1. 括号 / 引号 / here-string 不配对
  2. 批处理里的 goto 目标不存在（goto 到不存在的标签会静默跳到文件末尾）
  3. 引用了解释器里不存在的 cmdlet（拼错名字）

它挡不住的（必须真机验证）：参数语义、注册计划任务的权限、taskkill 是否真杀掉进程。
用法：
    python3 tools/ps_lint.py                       # 查全部 Windows 脚本
    python3 tools/ps_lint.py path/to/x.ps1         # 只查指定文件
退出码：0 = 通过；1 = 发现问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# PowerShell 里我们真正用到的 cmdlet —— 用来抓拼写错误，不是完整清单
KNOWN_CMDLETS = {
    "New-ScheduledTaskAction", "New-ScheduledTaskTrigger", "New-ScheduledTaskSettingsSet",
    "Register-ScheduledTask", "Unregister-ScheduledTask", "Get-ScheduledTask",
    "Get-NetTCPConnection", "Get-Command", "Get-Date", "Copy-Item", "New-Item",
    "Set-Content", "Get-Content", "Resolve-Path", "Join-Path", "Test-Path", "Write-Host",
    "Start-Process", "New-TimeSpan", "Out-Null", "Format-Table", "Select-Object",
    "Where-Object", "ForEach-Object", "Sort-Object", "Remove-Item", "Get-ScheduledTaskInfo",
}


def _strip_ps(src: str) -> str:
    """去掉注释与字符串字面量，只留结构 —— 括号配对要在结构上判，不能把字符串里的括号算进去。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        # 块注释 <# ... #>
        if src.startswith("<#", i):
            end = src.find("#>", i + 2)
            i = n if end < 0 else end + 2
            continue
        # 行注释 #（但 # 在字符串里不算，这里已经在非字符串位置）
        if c == "#":
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        # 双引号字符串（含转义 `"）
        if c == '"':
            i += 1
            while i < n:
                if src[i] == "`":
                    i += 2
                    continue
                if src[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        # 单引号字符串（'' 表示一个引号）
        if c == "'":
            i += 1
            while i < n:
                if src[i] == "'":
                    if i + 1 < n and src[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_powershell(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8-sig", errors="replace")

    if not raw.lstrip().startswith(("<#", "#", "param", "[")):
        problems.append("开头不像 PowerShell 脚本（缺 <# ... #> 或 param 块）")

    stripped = _strip_ps(raw)
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    for lineno, line in enumerate(stripped.splitlines(), 1):
        for ch in line:
            if ch in "([{":
                stack.append((ch, lineno))
            elif ch in ")]}":
                if not stack:
                    problems.append(f"第 {lineno} 行：多出来的 {ch!r}")
                elif stack[-1][0] != pairs[ch]:
                    problems.append(
                        f"第 {lineno} 行：{ch!r} 与第 {stack[-1][1]} 行的 {stack[-1][0]!r} 不配对")
                    stack.pop()
                else:
                    stack.pop()
    for ch, lineno in stack:
        problems.append(f"第 {lineno} 行：{ch!r} 没有闭合")

    # here-string @" ... "@ 必须成对；结束符必须顶格（开头可以在赋值号后面）
    opens = len(re.findall(r'@["\']\s*$', raw, re.M))
    closes = len(re.findall(r'^["\']@', raw, re.M))
    if opens != closes:
        problems.append(f"here-string 不配对：@\" 开 {opens} 个，\"@ 闭 {closes} 个")

    # 脚本自己定义的函数不算拼写错误
    local_funcs = set(re.findall(r"(?mi)^\s*function\s+([A-Za-z][\w-]*)", raw))
    for m in re.finditer(r"\b([A-Z][a-z]+-[A-Z][A-Za-z]+)\b", raw):
        name = m.group(1)
        if name not in KNOWN_CMDLETS and name not in local_funcs:
            problems.append(f"未知 cmdlet {name!r}（拼错？还是没加进 KNOWN_CMDLETS？）")

    if not re.search(r"\[CmdletBinding\(\)\]|^param\(", raw, re.M):
        problems.append("没有 param 块（脚本不接受参数，是故意的吗）")
    return problems


def check_batch(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    if not lines or not lines[0].lower().startswith("@echo off"):
        problems.append("第一行不是 @echo off")
    if "setlocal" not in raw.lower():
        problems.append("没有 setlocal（变量会漏到调用者的环境里）")

    labels = {ln.split(":", 1)[1].split()[0].lower()
              for ln in lines if ln.startswith(":") and len(ln) > 1}
    labels |= {"eof"}
    for lineno, ln in enumerate(lines, 1):
        for m in re.finditer(r"\bgoto\s+:?([A-Za-z_][\w]*)", ln, re.I):
            tgt = m.group(1).lower()
            if tgt not in labels:
                problems.append(f"第 {lineno} 行：goto {tgt} 指向不存在的标签")
        for m in re.finditer(r"\bcall\s+:([A-Za-z_][\w]*)", ln, re.I):
            if m.group(1).lower() not in labels:
                problems.append(f"第 {lineno} 行：call :{m.group(1)} 指向不存在的标签")

    # 批处理必须 CRLF：LF 结尾的 .bat 在部分 Windows 版本上会把末行吃掉
    data = path.read_bytes()
    if b"\n" in data and b"\r\n" not in data:
        problems.append("行尾是 LF，Windows 批处理要 CRLF")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        targets = [Path(a) for a in argv[1:]]
    else:
        targets = sorted((ROOT / "deploy" / "windows").glob("*.ps1"))
        targets.append(ROOT / "dabai.bat")
    targets = [t for t in targets if t.is_file()]
    if not targets:
        print("没有找到要检查的脚本")
        return 1

    bad = 0
    for t in targets:
        problems = check_powershell(t) if t.suffix.lower() == ".ps1" else check_batch(t)
        rel = t.relative_to(ROOT) if str(t).startswith(str(ROOT)) else t
        if problems:
            bad += 1
            print(f"✘ {rel}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"✔ {rel}（{len(t.read_text(encoding='utf-8-sig', errors='replace').splitlines())} 行）")
    print()
    print(f"{len(targets) - bad}/{len(targets)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
