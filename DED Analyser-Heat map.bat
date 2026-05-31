@echo off
set PYTHON=C:\Users\Eyal\AppData\Local\Python\pythoncore-3.14-64\python.exe

echo Stopping any existing server on port 5050...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5050 "') do (
    taskkill /PID %%p /F >nul 2>&1
)
timeout /t 3 >nul

echo Starting DED Analyser server...
cd /d "%~dp0"
start "" "%PYTHON%" app.py

echo Waiting for server to start...
set /a attempts=0
:wait
set /a attempts+=1
if %attempts% GTR 40 (
    echo.
    echo ERROR: Server did not start after 40 seconds.
    pause
    exit /b 1
)
timeout /t 1 >nul
curl -s --max-time 2 http://localhost:5050/ded >nul 2>&1
if errorlevel 1 goto wait

start "" "http://localhost:5050/ded"
echo Server is running at http://localhost:5050/ded
