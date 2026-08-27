@echo off
:: Local release build: PyInstaller -> Inno Setup installer.
:: Output lands in dist_installer\
setlocal
cd /d "%~dp0"

for /f "usebackq tokens=*" %%v in (`python -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)\"', open('src/version.py').read()).group(1))"`) do set APPVER=%%v
if "%APPVER%"=="" (
    echo Could not read the version from src\version.py
    exit /b 1
)
echo Building DWG Viewer %APPVER%

pip install -r requirements.txt || exit /b 1
pip install pyinstaller || exit /b 1

rmdir /s /q build dist 2>nul
pyinstaller --noconfirm --clean DWGViewer.spec || exit /b 1

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
    echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
    exit /b 1
)
%ISCC% /DAppVersion=%APPVER% installer\DWGViewer.iss || exit /b 1

echo.
echo Done: dist_installer\DWGViewer-Setup-%APPVER%.exe
endlocal
