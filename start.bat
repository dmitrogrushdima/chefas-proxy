@echo off
setlocal
if exist "%~dp0dist\chefas-proxy-windows-x64\chefas-proxy.exe" (
  cd /d "%~dp0"
  "%~dp0dist\chefas-proxy-windows-x64\chefas-proxy.exe" %*
  exit /b
)
set "runtime=%~dp0"
if exist "%~dp0src\" set "runtime=%~dp0bin\chefas-proxy\"
if not exist "%runtime%launch.py" (
  echo Download the ready-to-run package: https://github.com/dmitrogrushdima/chefas-proxy/releases
  echo For source development, build first: python scripts/build_ext.py
  exit /b 1
)
if not defined CHEFAS_PROXY_PYTHON set "CHEFAS_PROXY_PYTHON=python"
cd /d "%~dp0"
"%CHEFAS_PROXY_PYTHON%" "%runtime%launch.py" %*
exit /b %errorlevel%
