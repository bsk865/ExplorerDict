@echo off
rem ============================================================
rem  ExplorerDict launcher (GUI, no console window)
rem
rem  IMPORTANT: double-click THIS file in File Explorer.
rem  A chat/file link is NOT a reliable way to execute it (the link may
rem  open a viewer or a sandboxed copy instead of running the file).
rem
rem  ASCII-only on purpose: .cmd files are read with the OEM code page,
rem  so this file must not contain literal non-ASCII text. Paths with
rem  Chinese characters are still handled - they are passed via variables.
rem
rem  Startup goes through bootstrap.py, which records the resolved
rem  interpreter path and any early failure to data\logs\startup.log.
rem  If no window appears, read that file.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "BOOT=%~dp0bootstrap.py"
set "LAUNCHINFO=%~dp0data\logs\launcher.env"

if not exist "%BOOT%" (
  echo [ERROR] bootstrap.py not found next to this .cmd file.
  echo         Expected: "%BOOT%"
  pause
  exit /b 1
)

rem ---- Resolve pythonw.exe to an ABSOLUTE path ----------------------
set "PYW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do (
  if not defined PYW set "PYW=%%~fI"
)
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW (
  echo [ERROR] pythonw.exe not found. Install Python 3.11+ ^(64-bit^) and add it to PATH.
  echo         Details: see data\logs\startup.log
  pause
  exit /b 1
)

rem ---- Tell bootstrap.py which interpreter this launcher picked -----
rem  Written as a sidecar because .cmd cannot emit UTF-8; bootstrap.py
rem  folds it into startup.log (single UTF-8 log file, no mojibake).
if not exist "%~dp0data\logs" mkdir "%~dp0data\logs" >nul 2>nul
> "%LAUNCHINFO%" echo cmd=%~f0
>>"%LAUNCHINFO%" echo pythonw=%PYW%
>>"%LAUNCHINFO%" echo bootstrap=%BOOT%
>>"%LAUNCHINFO%" echo cwd=%CD%

if not exist "%~dp0app\main.py" (
  echo [ERROR] app\main.py not found. Keep this .cmd inside the project folder.
  pause
  exit /b 1
)

rem ---- Launch detached (GUI). Early errors land in startup.log ------
start "" "%PYW%" -X utf8 "%BOOT%" %*
exit /b 0
