@echo off
rem Bouwt Upscaler4K.exe op deze pc (reserve als de automatische bouw via GitHub niet beschikbaar is).
title Upscaler4K.exe bouwen
cd /d "%~dp0"
py -3 -m pip install --upgrade pyinstaller numpy opencv-python-headless onnxruntime-directml || goto fout
py -3 -m PyInstaller --noconfirm --clean --windowed --name Upscaler4K --hidden-import numpy --hidden-import cv2 --collect-all onnxruntime upscaler.py || goto fout
copy /y LEESMIJ.md dist\Upscaler4K\ >nul
echo.
echo Klaar! Het programma staat in de map dist\Upscaler4K\Upscaler4K.exe
explorer dist\Upscaler4K
pause
exit /b
:fout
echo.
echo Er ging iets mis. Maak een foto of kopie van de meldingen hierboven.
pause
