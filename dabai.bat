@echo off
REM 大白 Windows 启动脚本（与 dabai.sh 等价）
REM
REM 用法：
REM   dabai.bat             启动 server.py（经 deploy\windows\launch.py 注入密钥）
REM   dabai.bat --setup     一键：建 venv + 装依赖 + 启动（首次用这条）
REM   dabai.bat --check     只做环境自检，不启动
REM
REM 环境变量：
REM   DABAI_PYTHON  指定解释器（默认：venv\Scripts\python.exe -> py -3 -> python）
REM   DABAI_PORT    覆盖端口（默认沿用 settings.json 配置）
REM
REM 为什么不用 requirements.txt：那里面有 Blender 内嵌模块和本机私有包，PyPI 装不到，
REM 会成片失败。跨平台的那份是 requirements-core.txt（见它的文件头）。
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "ROOT=%CD%"
set "SETUP=0"
set "CHECKONLY=0"
set "ARGS="

REM ---- 参数分流：--setup / --check / --help 自己处理，其余原样透传给 server.py ----
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--setup" ( set "SETUP=1" & shift & goto parse )
if /i "%~1"=="--check" ( set "CHECKONLY=1" & shift & goto parse )
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
set "ARGS=!ARGS! %~1"
shift
goto parse

:parsed
set "VENV_PY=%ROOT%\venv\Scripts\python.exe"

if "%SETUP%"=="1" (
  if not exist "%VENV_PY%" call :bootstrap
)

set "PY="
if exist "%VENV_PY%" set "PY=%VENV_PY%"
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY call :find_python
if not defined PY (
  echo [X] 找不到 Python。请装 Python 3.10+ 并勾选 "Add python.exe to PATH"，
  echo     或用环境变量 DABAI_PYTHON 指定解释器。
  exit /b 1
)

REM ---- 环境自检（缺依赖时给出可执行命令，而不是让 server 崩在 import）----
if "%CHECKONLY%"=="1" (
  %PY% "%ROOT%\tools\selfcheck.py"
  exit /b !errorlevel!
)

set "MISSFILE=%TEMP%\dabai_missing_%RANDOM%.txt"
%PY% -c "import importlib.util as u;need=['fastapi','uvicorn','aiohttp','requests','starlette'];print(' '.join(m for m in need if u.find_spec(m) is None))" > "%MISSFILE%" 2>nul
set "MISSING="
set /p MISSING=<"%MISSFILE%"
del "%MISSFILE%" >nul 2>nul
if defined MISSING (
  echo [X] 缺少依赖：!MISSING!
  echo     安装：%PY% -m pip install -r "%ROOT%\requirements-core.txt"
  echo     或先自检：dabai.bat --check
  exit /b 1
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM 走 launch.py 而不是直接跑 server.py：它先把 secrets.env 读进环境变量再启动。
REM Windows 的计划任务没有 EnvironmentFile= 等价物，靠这层保证「手动跑」和
REM 「服务跑」拿到同一套密钥 —— 否则会出现只在一侧复现的诡异差异。
%PY% "%ROOT%\deploy\windows\launch.py"!ARGS!
exit /b !errorlevel!

REM ---- 子过程 ----
:bootstrap
echo == 首次运行：创建虚拟环境并安装依赖 ==
set "SYS_PY="
where py >nul 2>nul
if %errorlevel% equ 0 set "SYS_PY=py -3"
if not defined SYS_PY (
  where python >nul 2>nul
  if %errorlevel% equ 0 set "SYS_PY=python"
)
if not defined SYS_PY (
  echo [X] 找不到 Python，装不了环境。请先装 Python 3.10+ 再跑一次。
  exit /b 1
)
%SYS_PY% -m venv "%ROOT%\venv"
if not exist "%VENV_PY%" (
  echo [X] 创建 venv 失败，看上面的报错。
  exit /b 1
)
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%ROOT%\requirements-core.txt"
if errorlevel 1 echo [!] 有依赖没装上（详见上面输出）。核心能力通常仍可用，缺哪个补哪个。
echo == 环境就绪 ==
exit /b 0

:find_python
where py >nul 2>nul
if %errorlevel% equ 0 ( set "PY=py -3" & exit /b 0 )
where python >nul 2>nul
if %errorlevel% equ 0 ( set "PY=python" & exit /b 0 )
exit /b 1

:usage
echo 大白 Windows 启动脚本
echo.
echo   dabai.bat             启动 server.py
echo   dabai.bat --setup     一键：建 venv + 装依赖 + 启动（首次用这条）
echo   dabai.bat --check     只做环境自检，不启动
echo.
echo 环境变量：
echo   DABAI_PYTHON  指定解释器（默认：venv\Scripts\python.exe -^> py -3 -^> python）
echo   DABAI_PORT    覆盖端口（默认沿用 settings.json 配置）
exit /b 0
