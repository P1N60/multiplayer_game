@echo off
setlocal

REM Build a standalone Windows executable using PyInstaller.
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

REM Pin setuptools to a version that avoids newer pkg_resources jaraco runtime hook issues.
python -m pip install setuptools==69.5.1
if errorlevel 1 exit /b 1

python -m pip install pyinstaller==6.16.0
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean --onefile --windowed --name MultiplayerTopDown --copy-metadata setuptools main.py
if errorlevel 1 exit /b 1

if exist release rmdir /s /q release
mkdir release

copy dist\MultiplayerTopDown.exe release\MultiplayerTopDown.exe >nul
copy README.md release\README.md >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'release\MultiplayerTopDown.exe','release\README.md' -DestinationPath 'release\MultiplayerTopDown-Windows.zip' -Force"
if errorlevel 1 exit /b 1

echo.
echo Build complete.
echo EXE: release\MultiplayerTopDown.exe
echo ZIP: release\MultiplayerTopDown-Windows.zip
