@echo off
setlocal
set ROOT=%~dp0

echo [1/2] Checking CDP port 9222...
netstat -ano | findstr /R /C:":9222 .*LISTENING" >nul
if %errorlevel% neq 0 (
  echo CDP 9222 is not listening. Starting Chrome with remote debugging...
  start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%ROOT%profiles\chrome_cdp"
  timeout /t 3 /nobreak >nul
) else (
  echo CDP 9222 already active.
)

echo [2/2] Starting UI on 7861...
powershell -ExecutionPolicy Bypass -File "%ROOT%scripts\start_ui.ps1" -ForceRestart

echo Done.
endlocal
