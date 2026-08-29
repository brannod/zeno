@echo off
setlocal
cd /d "%~dp0"
title Install Zeno 3.4.3
echo Installing optional Zeno integrations...
py -3 -m pip install --upgrade pip
if errorlevel 1 goto :fail
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo.
echo Installing Chromium for Live Browser...
py -3 -m playwright install chromium
if errorlevel 1 goto :fail
echo.
echo Zeno 3.4.3 dependencies are installed.
pause
exit /b 0
:fail
echo.
echo Installation stopped because a command failed.
pause
exit /b 1
