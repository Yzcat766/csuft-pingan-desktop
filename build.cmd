@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
    echo Missing project Python. Run install.cmd and install requirements-build.txt first.
    if "%~1"=="" pause
    exit /b 1
)
rem Both CMD and Python use the same isolated, verified EXE build pipeline.
".venv\Scripts\python.exe" "%~dp0build_exe.py" %*
set "buildExit=%errorlevel%"
if not "%buildExit%"=="0" echo Build failed. Review the log path printed above.
if "%~1"=="" pause
exit /b %buildExit%
