@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Please run install.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PySide6.QtWidgets, requests, win32crypt; from cryptography.fernet import Fernet" >nul 2>&1
if errorlevel 1 (
    echo Dependencies are missing. Please run install.cmd again.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py" %*
