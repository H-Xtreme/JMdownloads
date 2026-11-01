@echo off
cd /d "%~dp0"
echo ============================================
echo   安装 / 修复依赖到 libs 目录
echo   只写程序目录，不动系统环境
echo ============================================
echo.

set "PY=%~dp0runtime\python.exe"
if exist "%PY%" goto found

py -3.10 -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython
set "PY=py -3.10"

:found
"%PY%" -c "import sys;print('使用解释器:', sys.executable);print('版本:', sys.version.split()[0])"
echo.
echo 正在下载依赖，请保持网络畅通...
echo.
"%PY%" -m pip install --upgrade --target "%~dp0libs" -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto failed

echo.
echo 依赖安装完成，现在可以双击「启动.bat」了。
pause
exit /b 0

:failed
echo.
echo 安装失败。可以换官方源再试一次：
echo   "%PY%" -m pip install --upgrade --target "%~dp0libs" -r "%~dp0requirements.txt"
pause
exit /b 1

:nopython
echo 没有找到自带的 runtime，也没有系统 Python 3.10。
echo 请先安装 Python 3.10，或从备份里恢复 runtime 目录。
pause
exit /b 1
