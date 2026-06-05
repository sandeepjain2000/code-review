@echo off
setlocal EnableExtensions

REM CodeSentinel — start backend, frontend dev server, and open the UI
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

title CodeSentinel Launcher
color 0A
echo.
echo  ========================================
echo   CodeSentinel - Starting...
echo  ========================================
echo.
echo  Project: %ROOT%
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python 3 and add it to PATH.
    echo.
    pause
    exit /b 1
)

if not exist "%ROOT%\backend\main.py" (
    echo  ERROR: backend\main.py not found.
    pause
    exit /b 1
)

REM 1) Backend API (FastAPI + serves UI at /ui)
echo  [1/3] Starting backend on http://localhost:8000 ...
start "CodeSentinel - Backend" cmd /k "cd /d "%ROOT%\backend" && echo CodeSentinel Backend - http://localhost:8000 && echo API docs: http://localhost:8000/docs && echo UI: http://localhost:8000/ui && echo. && python main.py"

REM 2) Frontend static server (optional dev; API defaults to :8000)
echo  [2/3] Starting frontend on http://127.0.0.1:5500 ...
start "CodeSentinel - Frontend" cmd /k "cd /d "%ROOT%\frontend" && echo CodeSentinel Frontend - http://127.0.0.1:5500 && echo API target: http://localhost:8000 && echo. && python -m http.server 5500"

REM 3) Wait for backend, then open browser
echo  [3/3] Waiting for backend (5s), then opening browser...
timeout /t 5 /nobreak >nul

start "" "http://localhost:8000/ui?v=categories"

echo   Tip: If the UI looks old, press Ctrl+F5 on the browser page.

echo.
echo  ========================================
echo   Ready
echo  ========================================
echo.
echo   UI (recommended):  http://localhost:8000/ui
echo   API:               http://localhost:8000
echo   API docs:          http://localhost:8000/docs
echo   Frontend (dev):    http://127.0.0.1:5500
echo.
echo   Two terminal windows are running (Backend + Frontend).
echo   Close those windows to stop the servers.
echo.
pause

endlocal
