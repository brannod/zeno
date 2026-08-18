@echo off
title Install Zeno V2.7.21
cd /d "%~dp0"
echo ========================================================
echo   ZENO V2.7.21 DEPENDENCY INSTALLER
echo   Files + PDF + DeepSearch + Chromium + Discord
echo ========================================================
echo.
where py >nul 2>nul
if %errorlevel% neq 0 (
  echo ERROR: Python Launcher was not found.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo Check "Add python.exe to PATH" during installation, then run this again.
  pause
  exit /b 1
)
echo Updating pip...
py -3 -m pip install --upgrade pip
if %errorlevel% neq 0 goto :failed
echo.
echo Installing Zeno Python packages...
py -3 -m pip install -r requirements.txt
if %errorlevel% neq 0 goto :failed
echo.
echo Verifying Discord bridge package...
py -3 -c "import discord; print('discord.py ready:', discord.__version__)"
if %errorlevel% neq 0 goto :failed
echo.
echo Installing the private Chromium browser...
py -3 -m playwright install chromium
if %errorlevel% neq 0 goto :failed
echo.
echo SUCCESS: Zeno dependencies are installed.
echo You can now double-click START_ZENO.bat.
pause
exit /b 0

:failed
echo.
echo INSTALLATION FAILED. Check your internet connection and the error above.
echo See FIRST_TIME_USER_GUIDE.txt for troubleshooting.
pause
exit /b 1
