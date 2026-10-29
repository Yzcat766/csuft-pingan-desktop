@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Installed. Run start.cmd to open the desktop app.
pause
exit /b 0
:failed
echo Installation failed. Use 64-bit Python 3.12 or newer and retry.
pause
exit /b 1
