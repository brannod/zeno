@echo off
title Zeno V2.7.21 - Your private local work assistant
cd /d "%~dp0"
echo Starting Zeno V2.7.21
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -c "import discord" >nul 2>nul
  if errorlevel 1 echo WARNING: discord.py is missing. Run INSTALL_ZENO.bat once to enable the Discord bridge.
  py -3 zeno.py
) else (
  echo.
  echo Python Launcher was not found. Trying python.exe...
  python -c "import discord" >nul 2>nul
  if errorlevel 1 echo WARNING: discord.py is missing. Run INSTALL_ZENO.bat once to enable the Discord bridge.
  python zeno.py
)
echo.
echo Zeno stopped. Review any error shown above.
pause
