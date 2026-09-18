#!/bin/sh
# 在 Windows(ssh win) 上执行一个 PowerShell 脚本文件。
# 为什么要编码：ssh 到 Windows 走 cmd.exe，cmd 会吃掉双引号并把 | 当管道，
# 内联 PS 命令只要带管道/引号就必然被打散。UTF-16LE base64 对 cmd 是纯文本，安全。
# 用法: tools/winps.sh /tmp/xxx.ps1
f="$1"
[ -f "$f" ] || { echo "usage: $0 <script.ps1>" >&2; exit 2; }
b64=$(python3 -c "import base64,sys;print(base64.b64encode(open(sys.argv[1],'rb').read().decode('utf-8').encode('utf-16-le')).decode())" "$f") || exit 3
ssh win "powershell -NoProfile -EncodedCommand $b64"
