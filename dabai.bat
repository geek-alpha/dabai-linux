@echo off
rem 大白 Windows 启动脚本（与 dabai.sh 等价）
rem
rem 用法：
rem   dabai.bat              启动 server.py（首次运行自动建 venv + 装依赖；缺 Python/Node 时自动安装）
rem   dabai.bat --setup      强制重跑安装：建 venv + 装依赖 + 自检 + 启动
rem   dabai.bat --check      只做环境自检，不启动
rem   dabai.bat --diag       环境诊断：解释器 / 依赖 / 端口 / 外部工具 / Node / 管理员
rem   dabai.bat --deps       依赖审计：启动路径依赖 vs 清单，标出漏网 / 冗余
rem   dabai.bat --deps --export              一键导出本机实际依赖（带版本）
rem   dabai.bat --deps --freeze -o lock.txt  一键导出完整环境锁定（pip freeze）
rem
rem 环境变量：
rem   DABAI_PYTHON  指定解释器（默认：venv\Scripts\python.exe -> py -3 -> python）
rem   DABAI_PORT    覆盖端口（默认沿用 settings.json 配置）
rem
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "ROOT=%CD%"

set "SETUP=0"
set "CHECKONLY=0"
set "DIAG=0"
set "DEPS=0"
set "PASS="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--setup" goto opt_setup
if /i "%~1"=="--check" goto opt_check
if /i "%~1"=="--diag" goto opt_diag
if /i "%~1"=="--deps" goto opt_deps
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
set "PASS=%PASS% %~1"
shift
goto parse

:opt_setup
set "SETUP=1"
shift
goto parse

:opt_check
set "CHECKONLY=1"
shift
goto parse

:opt_diag
set "DIAG=1"
shift
goto parse

:opt_deps
set "DEPS=1"
shift
goto parse

:parsed
rem ---- 工作目录写权限：装在 Program Files 等受保护目录时，非管理员会在建 venv / 写
rem 证书时一直卡在权限重试，用户只看到黑窗停住。先探一次，把原因说清楚再往下走。
mkdir "%ROOT%\.dabai_write_test" 2>nul
if not exist "%ROOT%\.dabai_write_test" (
  echo [X] 当前目录不可写：%ROOT%
  echo     装在 Program Files 等受保护目录时，非管理员会卡在建 venv / 写证书这一步。
  echo     请把大白解压到用户目录（如 %%USERPROFILE%%\dabai）后重试。
  pause
  exit /b 1
)
rmdir "%ROOT%\.dabai_write_test" 2>nul


rem ---- 基础解释器：只用来建 venv ----
set "BASEPY="
if defined DABAI_PYTHON set "BASEPY=%DABAI_PYTHON%"
if not defined BASEPY (
  where py >nul 2>nul
  if not errorlevel 1 set "BASEPY=py -3"
)
if not defined BASEPY (
  where python >nul 2>nul
  if not errorlevel 1 set "BASEPY=python"
)
if not defined BASEPY (
  call :install_python
  if not defined BASEPY (
    pause
    exit /b 1
  )
)

set "VPY=%ROOT%\venv\Scripts\python.exe"

rem ---- 首次运行：没 venv 就自动装（双击即可，不必先手敲 --setup）----
rem 例外一：显式设了 DABAI_PYTHON —— 那是刻意不用 venv 的人，
rem 强建一个几百 MB 的环境属于越界，保持原行为（缺依赖时提示 --setup）。
rem 例外二：--check / --diag / --deps 是只读诊断，不该顺手下依赖。
if "%SETUP%"=="0" if "%CHECKONLY%"=="0" if "%DIAG%"=="0" if "%DEPS%"=="0" (
  if not exist "!VPY!" if not defined DABAI_PYTHON (
    set "SETUP=1"
    echo == 首次运行：未找到虚拟环境，自动开始安装（等价于 dabai.bat --setup）==
  )
)

rem ---- 一键引导：venv 不在、或启动必需依赖不全，都补装（幂等）----
rem 只看 venv\Scripts\python.exe 存在与否不够：上次装到一半（磁盘满 / 断网）会留下一个
rem 半成品 venv，再跑 --setup 会直接跳过，问题拖到启动时才炸。
if "%SETUP%"=="1" (
  set "NEED=0"
  if not exist "!VPY!" (
    set "NEED=1"
  ) else (
    "!VPY!" "%ROOT%\tools\check_deps.py" --gate >nul 2>nul
    if errorlevel 1 set "NEED=1"
  )
  if "!NEED!"=="1" (
    echo == 环境缺失或依赖不全：创建虚拟环境并安装依赖 ==
    if not exist "!VPY!" (
      %BASEPY% -m venv "%ROOT%\venv"
      if errorlevel 1 (
        echo [X] 创建虚拟环境失败
        pause
        exit /b 1
      )
    )
    echo 正在安装依赖（首次下载量较大，可能几分钟无输出，属正常）...
    rem 走 tools/pip_mirror.py 而不是裸 pip：pip 默认源 pypi.org 国内常几 KB/s 甚至超时，
    rem 而写死某个国内镜像同样不行——清华对云厂商 IP 段间歇 403。只能探测 + 失败换源。
    "!VPY!" "%ROOT%\tools\pip_mirror.py" --upgrade pip
    "!VPY!" "%ROOT%\tools\pip_mirror.py" -r "%ROOT%\requirements-core.txt"
    if errorlevel 1 (
      echo [X] 依赖安装失败，请检查网络或代理后重试
      pause
      exit /b 1
    )
  )
)

rem ---- 选解释器：优先项目内 venv ----
set "PYCMD="
if exist "!VPY!" set PYCMD="!VPY!"
if not defined PYCMD set "PYCMD=%BASEPY%"

rem ---- 依赖审计 / 导出：--deps 后的参数原样透传给 deps_audit.py ----
rem 无参数 = 报告；--export = 代码 import 反推的直接依赖；--freeze = pip freeze 全量。
if "%DEPS%"=="1" (
%PYCMD% "%ROOT%\tools\deps_audit.py" %PASS%
pause
exit /b !errorlevel!
)

rem ---- Node.js：前端 .ts 实时转译的硬依赖 ----
rem 缺 Node 时服务照常启动、网页也能打开，但会永远停在「连接中…」，而服务端一行错
rem 都不报 —— 最容易被当成「这程序坏了」的故障。所以在这里拦住并自动装一份到用户
rem 目录（不需要管理员，也不覆盖用户自己装的 Node）。
rem 用 goto 而不是 if(...) 块：install_node.py 的 FAIL 信息里可能带半角括号，
rem 在块里会把块提前闭合掉。
if "%CHECKONLY%"=="1" goto node_done
if "%DIAG%"=="1" goto node_done
rem 这里不设 PYTHONUTF8：install_node.py 自己按控制台代码页输出，而 PYTHONUTF8 会泄漏给
rem 后面的 check_deps.py，把中文依赖清单写成 UTF-8 塞进 GBK 控制台，用户看到一片乱码。
rem 结果走文件回传，不用 for /f 捕获命令输出：命令串以引号开头时 cmd 会按「去掉首引号
rem + 删掉末尾引号」重解析，把 python.exe" 连引号当成程序名，报「文件名、目录名或卷标
rem 语法不正确。」，一行输出都拿不到，只剩「produced no output」这种查不出所以然的结论。
if not defined TEMP set "TEMP=%ROOT%"
set "NODE_OUT=%TEMP%\dabai-node-result.txt"
del "%NODE_OUT%" 2>nul
set "NODE_LINE="
%PYCMD% "%ROOT%\tools\install_node.py" --ensure --result "%NODE_OUT%"
set "NODE_RC=!errorlevel!"
for /f "usebackq delims=" %%L in ("%NODE_OUT%") do set "NODE_LINE=%%L"
if not defined NODE_LINE set "NODE_LINE=FAIL install_node.py produced no output (rc=!NODE_RC!)"
if "!NODE_LINE:~0,5!"=="FAIL " echo     [提示] 读不到 install_node.py 的结果行（退出码 !NODE_RC!），看上面的报错，或检查 %TEMP% 是否可写
if not "!NODE_LINE:~0,3!"=="OK " goto node_fail
set "NODE_EXE=!NODE_LINE:~3!"
for %%I in ("!NODE_EXE!") do set "PATH=%%~dpI;!PATH!"
del "%NODE_OUT%" 2>nul
echo [OK] Node.js：!NODE_EXE!
goto node_done

:node_fail
echo [X] !NODE_LINE!
del "%NODE_OUT%" 2>nul
echo     网页会永远卡在「连接中…」。请手动装 Node.js 22.13+（或 23.2+）：
echo       https://nodejs.org/
echo       国内镜像 https://registry.npmmirror.com/-/binary/node/
if not defined DABAI_NO_AUTO_INSTALL (
  pause
  exit /b 1
)
:node_done

rem ---- 环境自检（--check 只自检不启动）----
if "%CHECKONLY%"=="1" (
  %PYCMD% "%ROOT%\tools\selfcheck.py"
  exit /b !errorlevel!
)

rem ---- 环境诊断：起不来 / 卡住时先跑 --diag，把输出整段贴出来即可定位 ----
if "%DIAG%"=="1" (
  echo ===== 大白 环境诊断 =====
  echo 工作目录 : !ROOT!
  echo 系统     : %OS%  %PROCESSOR_ARCHITECTURE%
  echo.
  echo [解释器]
  echo   BASEPY : !BASEPY!
  where py 2>nul
  where python 2>nul
  if exist "!VPY!" (echo   VENV   : !VPY!  [存在]) else (echo   VENV   : 不存在)
  if exist "!VPY!" "!VPY!" -c "import sys;print('  VENV:', sys.version.split()[0])"
  echo.
  echo [依赖]
  !PYCMD! "!ROOT!\tools\check_deps.py"
  echo.
  echo [端口 8001]
  netstat -ano | findstr ":8001" | findstr LISTENING
  echo   -- Windows 保留端口段（非管理员绑不上的常见原因）--
  netsh interface ipv4 show excludedportrange protocol=tcp
  echo.
  echo [yt-dlp / ffmpeg]
  if exist "!VPY!" "!VPY!" -c "import importlib.util as u;print('  yt-dlp:', 'OK' if u.find_spec('yt_dlp') else 'MISSING')"
  where ffmpeg 2>nul
  echo.
  echo [Node.js（前端 .ts 转译；缺了网页永远停在「连接中…」）]
  !PYCMD! "!ROOT!\tools\install_node.py" --check
  echo.
  echo [是否管理员]
  net session >nul 2>&1
  if errorlevel 1 (echo   否) else (echo   是)
  echo ==============================
  pause
  exit /b 0
)

rem ---- 依赖自检：缺包时给出可执行命令，而不是让 server 崩在 import ----
echo [1/2] 检查依赖...
%PYCMD% "%ROOT%\tools\check_deps.py"
if errorlevel 1 (
  echo     或一键补齐：dabai.bat --setup
  rem 双击运行时窗口会随 exit 一起关掉，用户什么都看不到——停一下让他读完。
  pause
  exit /b 1
)

rem ---- 启动 ----
rem 失败时不再只 pause 一声不响：把退出码与对应诊断命令打出来。
rem 端口绑不上（Windows 保留端口段 / 已被占用）是「非管理员起不来」的最常见
rem 原因，server.py 会自己打印 netsh 修复命令，这里再补一条兜底提示。
title dabai
echo [2/2] 启动服务（首次启动约 10-30 秒，日志停在最后一行属正常）...
rem ---- 本地定制回打：release 资产包/更新会整文件覆盖工作区，端口 8008 等定制随之丢失 ----
rem 脚本在 local\ 下，既不在 git 也不在资产包里，所以它自己永远活着，启动前跑一次即可。
if exist "%ROOT%\local\ensure_local.py" %PYCMD% "%ROOT%\local\ensure_local.py"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
%PYCMD% "%ROOT%\deploy\windows\launch.py" %PASS%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [X] 服务未正常启动，退出码 %RC%
  echo     报 WinError 10013（端口被拒）：Windows 保留了该端口段，按上面
  echo     server 输出里的 netsh 命令修复一次即可，不必长期用管理员运行。
  echo     查保留段 : netsh interface ipv4 show excludedportrange protocol=tcp
  echo     查占用者 : netstat -ano ^| findstr :8001
  echo     防火墙   : 若弹出「Windows 安全中心」允许访问，请勾选专用网络并允许
  pause
)
exit /b %RC%

:usage
echo 大白 Windows 启动脚本
echo.
echo   dabai.bat              启动 server.py（首次运行自动建 venv 装依赖；缺 Python/Node 时自动安装）
echo   dabai.bat --setup      强制重跑安装：建 venv + 装依赖 + 自检 + 启动
echo   dabai.bat --check      只做环境自检，不启动
echo   dabai.bat --diag       打印环境诊断（起不来/卡住时先跑这条）
echo   dabai.bat --deps       依赖审计：启动路径依赖 vs 清单，标出漏网 / 冗余
echo   dabai.bat --deps --export              一键导出本机实际依赖（带版本）
echo   dabai.bat --deps --freeze -o lock.txt  一键导出完整环境锁定（pip freeze）
echo.
echo 环境变量：
echo   DABAI_PYTHON            指定解释器
echo   DABAI_PORT              覆盖端口
echo   DABAI_NO_AUTO_INSTALL   设了就不自动装 Python / Node.js，只报错
exit /b 0

rem ============================================================
rem 子程序：自动安装 Python（只在探不到任何解释器时调用）
rem ============================================================
:install_python
rem 装到 %LOCALAPPDATA%\Programs\Python\Python312（用户级，不需要管理员权限）。
rem 安装器自带 PrependPath=1 会写用户 PATH，但当前 cmd 会话的 PATH 是启动时的快照、
rem 不会自动刷新，所以下面按固定路径再探一次 —— 少了这步就是「装完了还说找不到」。
if defined DABAI_NO_AUTO_INSTALL (
  echo [X] 找不到 Python，且已设 DABAI_NO_AUTO_INSTALL（禁用自动安装）
  echo     手动装：https://www.python.org/downloads/windows/
  echo     安装第一屏务必勾上 Add python.exe to PATH
  exit /b 1
)
where curl >nul 2>nul
if errorlevel 1 (
  echo [X] 找不到 Python，且系统没有 curl（Win10 1803+ 才自带），无法自动下载
  echo     手动装：https://www.python.org/downloads/windows/
  echo     安装第一屏务必勾上 Add python.exe to PATH
  exit /b 1
)
echo == 未找到 Python，自动安装 Python 3.12（用户级，不需要管理员）==
set "PYSETUP=%TEMP%\dabai-python-3.12.8-amd64.exe"
set "PYSIZE=0"
if exist "!PYSETUP!" for %%F in ("!PYSETUP!") do set "PYSIZE=%%~zF"
if !PYSIZE! LSS 10000000 (
  echo 正在下载安装包（约 27 MB）...
  curl -L --fail --connect-timeout 20 --max-time 900 -o "!PYSETUP!" "https://mirrors.huaweicloud.com/python/3.12.8/python-3.12.8-amd64.exe"
  if errorlevel 1 (
    echo     华为云镜像失败，改用官方源重试...
    curl -L --fail --connect-timeout 20 --max-time 900 -o "!PYSETUP!" "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
  )
  set "PYSIZE=0"
  if exist "!PYSETUP!" for %%F in ("!PYSETUP!") do set "PYSIZE=%%~zF"
)
if !PYSIZE! LSS 10000000 (
  echo [X] 安装包下载失败或不完整（!PYSIZE! 字节）
  del "!PYSETUP!" 2>nul
  echo     请检查网络或代理后重试，或手动装：
  echo     https://www.python.org/downloads/windows/
  echo     安装第一屏务必勾上 Add python.exe to PATH
  exit /b 1
)
echo 正在静默安装（约 1-3 分钟，期间没有输出是正常的）...
rem 不看安装器返回码：3010（成功但需重启）也算成功。一律以能否探到解释器为准。
"!PYSETUP!" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
for %%V in (314 313 312 311 310) do (
  if not defined BASEPY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "BASEPY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
if not defined BASEPY (
  where py >nul 2>nul
  if not errorlevel 1 set "BASEPY=py -3"
)
if not defined BASEPY (
  echo [X] 安装程序跑完了，但仍探不到 Python 解释器
  echo     可以手动双击这个安装包看报错：!PYSETUP!
  echo     或手动装：https://www.python.org/downloads/windows/
  echo     安装第一屏务必勾上 Add python.exe to PATH
  exit /b 1
)
del "!PYSETUP!" 2>nul
echo == Python 已就绪：!BASEPY! ==
exit /b 0
