@echo off
echo Stopping all Stillbot, Python, and Chrome processes...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM chrome.exe /T >nul 2>&1
echo [OK] All servers, bots, and browser instances have been completely stopped!
pause
