@echo off
setlocal
cd /d "%~dp0"
title Zeno 3.6.4
set PYTHONUNBUFFERED=1
py -3 -u zeno.py
if errorlevel 1 (
  echo.
  echo Zeno exited with an error. Review the message above.
  pause
)
