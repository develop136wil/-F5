@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo      SearchMaster Build Automation
echo ==========================================

echo [1/3] Building Python Backend (PyInstaller)...
cd welfare_scraper
..\.venv\Scripts\python.exe -B scripts\build_backend_isolated.py --distpath dist --workpath build
if %errorlevel% neq 0 (
    echo "Python Build Failed!"
    exit /b %errorlevel%
)

echo Checking the frozen backend against current source...
..\.venv\Scripts\python.exe -B scripts\verify_backend_archive.py --executable dist\api-server.exe
if errorlevel 1 (
    echo "Backend source verification failed. Sidecar was not replaced."
    exit /b 1
)

echo [2/3] Setting up Sidecar Binary...
copy /Y "dist\api-server.exe" "..\welfare-tauri\src-tauri\api-server-x86_64-pc-windows-msvc.exe"
if %errorlevel% neq 0 (
    echo "Copy Failed!"
    exit /b %errorlevel%
)
cd ..

echo [3/3] Building Tauri Application...
cd welfare-tauri
call npm run tauri build
if %errorlevel% neq 0 (
    echo "Tauri Build Failed!"
    exit /b %errorlevel%
)
cd ..

echo Verifying and packaging an immutable release candidate...
.venv\Scripts\python.exe -B welfare_scraper\scripts\verify_release_bundle.py publish --project-root welfare-tauri --output-root artifacts\release-candidates
if errorlevel 1 (
    echo "Release verification failed. Do not distribute this build."
    exit /b 1
)

echo ==========================================
echo         Build Success!
echo Verified EXE, ZIP and manifest: artifacts\release-candidates
echo ==========================================
