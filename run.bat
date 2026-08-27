@echo off
:: DWG Viewer launcher
:: Double-click this file to start the viewer.

cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: Install / update dependencies on first run
pip show ezdxf >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies (first run)...
    pip install -r requirements.txt
)

pip show PyQt6 >nul 2>&1
if errorlevel 1 (
    echo Installing PyQt6...
    pip install -r requirements.txt
)

:: Launch the viewer — pythonw runs without a console window,
:: so this batch file can exit while the GUI stays open.
start "" pythonw main.py %*
