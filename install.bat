@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ============================================================
echo   HereTune installer
echo   Your NAS has been summoned for a performance review.
echo ============================================================
echo.

set "BASEPY="
where py >nul 2>nul
if not errorlevel 1 set "BASEPY=py -3"
if not defined BASEPY (
  where python >nul 2>nul
  if not errorlevel 1 set "BASEPY=python"
)
if not defined BASEPY (
  echo [ERROR] Python was not found.
  echo Install Python 3.11 or newer from python.org, then run this again.
  pause
  exit /b 1
)

for /f %%V in ('%BASEPY% -c "import sys; print(1 if sys.version_info >= (3,11) else 0)"') do set "PYOK=%%V"
if not "%PYOK%"=="1" (
  echo [ERROR] HereTune requires Python 3.11 or newer.
  %BASEPY% --version
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating a project-local Python environment in .venv ...
  %BASEPY% -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [1/4] Reusing the existing project-local Python environment.
)

echo [2/4] Updating packaging tools inside HereTune's project-local environment ...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :fail

echo [3/4] Installing HereTune and its Python dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade .
if errorlevel 1 goto :fail

if not exist config.toml copy /Y config.example.toml config.toml >nul

echo [4/4] Checking external tools ...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [WARN] FFmpeg is not on PATH. HereTune will start, but analysis will not.
  echo        Install FFmpeg or set paths.ffmpeg / paths.ffprobe in Settings.
) else (
  echo [ OK ] FFmpeg found.
)
where ffprobe >nul 2>nul
if errorlevel 1 (
  echo [WARN] FFprobe is not on PATH. Normal FFmpeg distributions include it.
) else (
  echo [ OK ] FFprobe found.
)

echo.
echo HereTune installed successfully.
echo Run start_heretune.bat, then open http://localhost:8777/ .
echo First launch will walk you through the media folder and analysis preset.
echo No global Python packages were harmed during the making of this installer.
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Installation failed. The pixels have unionized.
echo Review the output above, then try again.
pause
exit /b 1
