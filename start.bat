@echo off
title Video Upscaler 4K
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python is nog niet geinstalleerd. Dat wordt nu geprobeerd via winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo Sluit dit venster en dubbelklik daarna opnieuw op start.bat.
    echo Lukt het niet? Installeer Python zelf via https://www.python.org/downloads/
    pause
    exit /b
)

py -3 upscaler.py
if errorlevel 1 pause
