@echo off
setlocal
cd /d "%~dp0"
if not exist config.toml copy /Y config.example.toml config.toml >nul
if exist ".venv\Scripts\python.exe" (set "HERETUNE_PY=.venv\Scripts\python.exe") else (
  where py >nul 2>nul
  if not errorlevel 1 (set "HERETUNE_PY=py -3") else (set "HERETUNE_PY=python")
)
%HERETUNE_PY% -m heretune scan --config config.toml
pause
