@echo off
setlocal
cd /d "%~dp0"
title Zeno 3.6.0
py -3 zeno.py
if errorlevel 1 (
  echo.
  echo Zeno exited with an error. Review the message above.
  pause
)
