@echo off
setlocal
set "runtime=%~dp0"
if exist "%~dp0src\" set "runtime=%~dp0bin\kontur\"
if not exist "%runtime%launch.py" (
  echo Compiled runtime missing. Run: python build_ext.py
  exit /b 1
)
if not defined KONTUR_PYTHON set "KONTUR_PYTHON=python"
cd /d "%~dp0"
"%KONTUR_PYTHON%" "%runtime%launch.py" %*
exit /b %errorlevel%
