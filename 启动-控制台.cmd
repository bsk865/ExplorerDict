@echo off
rem ============================================================
rem  ExplorerDict console/debug launcher (keeps a console window)
rem
rem  Use this when the GUI does not appear: the console stays open and
rem  the same start-up details are also written to data\logs\startup.log.
rem
rem  Double-click THIS file in File Explorer. A chat/file link is NOT a
rem  reliable way to execute it.
rem
rem  ASCII-only on purpose: .cmd files are read with the OEM code page.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "BOOT=%~dp0bootstrap.py"
set "LAUNCHINFO=%~dp0data\logs\launcher.env"
chcp 65001 >nul

if not exist "%BOOT%" (
  echo [ERROR] bootstrap.py not found next to this .cmd file.
  echo         Expected: "%BOOT%"
  pause
  exit /b 1
)

rem ---- Resolve python.exe to an ABSOLUTE path ----------------------
set "PY="
for /f "delims=" %%I in ('where python.exe 2^>nul') do (
  if not defined PY set "PY=%%~fI"
)
if not defined PY (
  echo [ERROR] python.exe not found on PATH. Install Python 3.11+ ^(64-bit^).
  pause
  exit /b 1
)

if not exist "%~dp0data\logs" mkdir "%~dp0data\logs" >nul 2>nul
> "%LAUNCHINFO%" echo cmd=%~f0
>>"%LAUNCHINFO%" echo python=%PY%
>>"%LAUNCHINFO%" echo bootstrap=%BOOT%
>>"%LAUNCHINFO%" echo cwd=%CD%

echo Starting ExplorerDict in console/debug mode ...
echo   interpreter : "%PY%"
echo   bootstrap   : "%BOOT%"
echo   startup log : "%~dp0data\logs\startup.log"
echo.
"%PY%" -X utf8 "%BOOT%" --console-log %*
set "CODE=%ERRORLEVEL%"
echo.
echo [ExplorerDict] exit code = %CODE%
if not "%CODE%"=="0" echo [HINT] see data\logs\startup.log and data\logs\app.log for details.
pause
exit /b %CODE%
