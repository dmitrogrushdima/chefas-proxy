@echo off
setlocal
cd /d "%~dp0.."
echo Kontur local preview - TEST ONLY, no real pool or miners.
echo.
echo After the server starts, open Google Chrome at:
echo http://127.0.0.1:18771/
echo.
echo Keep this window open. Press Ctrl+C to stop the preview.
echo.
python launch.py --pool-host 127.0.0.1 --pool-port 18772 --listen-port 18770 --dashboard-port 18771 --env-file "" --history-file "" --compatibility-state "" --control-state state/preview-control.json --log-dir logs/preview
echo.
echo Preview stopped. If Python or a port is unavailable, check the message above.
pause
