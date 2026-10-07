@echo off
REM Encoded as ANSI(GBK) + CRLF - do NOT convert to UTF-8, cmd reads console cp936.
setlocal
cd /d "%~dp0"

echo ==============================================
echo   HTTP 压力测试工具 - 一键安装依赖
echo ==============================================
echo.

REM ---- 找 Python：虚拟环境 -> 本机路径 -> PATH ----
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "D:\Python\Python313\python.exe" set "PY=D:\Python\Python313\python.exe"
if not defined PY (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
    echo [失败] 没找到 Python。
    echo        请安装 https://www.python.org/downloads/ ，安装时勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

echo 使用的 Python：
%PY% --version
echo.
echo 正在安装依赖（requirements.txt，压测工具只需要 aiohttp）...
echo.

%PY% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo [失败] 安装出错。网络不畅可换清华镜像重跑：
    echo        %PY% -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo        或只装压测需要的：
    echo        %PY% -m pip install "aiohttp>=3.9" -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo.
echo ----------------------------------------------
%PY% -c "import aiohttp, sys; print('[成功] aiohttp', aiohttp.__version__, '| Python', sys.version.split()[0])"
echo [成功] 依赖就绪。
echo        图形界面：双击 打开HTTP压测工具.cmd
echo        命令行示例：%PY% http_pressure_test.py http://127.0.0.1:8080/ -c 5 -d 30
echo        使用文档：httptest.md
echo ----------------------------------------------
pause
exit /b 0
