@echo off
cd /d "%~dp0"
echo ============================================
echo   诊断模式：错误信息会显示在本窗口
echo ============================================
echo.

echo [1] 检查自带运行时
if exist "%~dp0runtime\python.exe" (
    "%~dp0runtime\python.exe" -c "import sys, tkinter; print('  runtime ->', sys.executable, sys.version.split()[0], 'tkinter OK')"
) else (
    echo   runtime 目录缺失
)
echo.

echo [2] 检查依赖目录
if exist "%~dp0libs\jmcomic" (echo   libs 已存在) else (echo   libs 不存在，请先运行 安装依赖.bat)
echo.

echo [3] 启动程序，关闭窗口即退出
echo.
if exist "%~dp0runtime\python.exe" (
    "%~dp0runtime\python.exe" "%~dp0app.py"
) else (
    py -3.10 "%~dp0app.py"
)
echo.
echo 程序已退出，退出码: %errorlevel%
echo 若上方没有报错但仍无界面，请查看 启动日志.txt
echo.
pause
