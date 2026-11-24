@echo off
setlocal EnableExtensions
title FOXAI Time Machine
cd /d "%~dp0"

echo.
echo  FOXAI Time Machine
echo  ------------------
echo.

REM Prefer the Windows Python launcher, then python, then python3.
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py"
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  where python3 >nul 2>&1 && set "PYEXE=python3"
)

if not defined PYEXE (
  echo  Python was not found on this computer.
  echo.
  echo  Install Python 3 from https://www.python.org/downloads/
  echo  During setup, enable "Add python.exe to PATH".
  echo.
  echo  Troubleshooting fallback after install:
  echo    py serve.py
  echo  then open http://127.0.0.1:8420
  echo.
  pause
  exit /b 1
)

echo  Using: %PYEXE%
echo.
"%PYEXE%" "%~dp0launch.py"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo  Launcher exited with error code %ERR%.
  pause
)
exit /b %ERR%
