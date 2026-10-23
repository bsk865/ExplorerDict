@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.build-env\Scripts\pythonw.exe" (
  echo Python runtime not found. Please restore the application environment.
  pause
  exit /b 1
)
start "" "%~dp0.build-env\Scripts\pythonw.exe" "%~dp0browser_entry.pyw"
