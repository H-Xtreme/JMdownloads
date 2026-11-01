@echo off
cd /d "%~dp0"

if not exist "%~dp0libs\jmcomic" (
    echo 首次使用，正在安装依赖，请稍等...
    call "%~dp0安装依赖.bat"
)

rem 优先用自带的 Python 运行时，整个文件夹可以随便搬，目标机器不装 Python 也能跑
if exist "%~dp0runtime\pythonw.exe" (
    start "" "%~dp0runtime\pythonw.exe" "%~dp0app.py"
    exit /b 0
)

rem 没有自带运行时，就找系统里的 Python 3.10
py -3.10 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    start "" py -3.10 "%~dp0app.py"
    exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0app.py"
    exit /b 0
)

echo.
echo 没找到可用的 Python。
echo 正常情况下 runtime 目录里应该带着 Python 运行时，请检查是否被杀毒软件删掉了。
echo 也可以安装 Python 3.10 后重试，或运行「诊断启动.bat」查看详细错误。
echo.
pause
exit /b 1
