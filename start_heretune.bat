@echo off
setlocal
cd /d "%~dp0"
if not exist config.toml copy /Y config.example.toml config.toml >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist ".venv\Scripts\python.exe" (
  set "HERETUNE_PY=.venv\Scripts\python.exe"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "HERETUNE_PY=py -3"
  ) else (
    set "HERETUNE_PY=python"
  )
  echo [HereTune] .venv not found; using system Python. Run install.bat for the supported setup.
)
%HERETUNE_PY% -m heretune serve --config config.toml
set "HERETUNE_EXIT=%ERRORLEVEL%"
if not "%HERETUNE_EXIT%"=="0" (
  echo.
  echo [HereTune] The server exited unexpectedly with code %HERETUNE_EXIT%.
  echo [HereTune] Crash details, when Python could capture them, are in data\logs\crash.log
  echo.
  pause
)
exit /b %HERETUNE_EXIT%
