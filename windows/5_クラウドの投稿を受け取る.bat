@echo off
chcp 65001 > nul
cd /d "%~dp0blog"
python tools\sync.py
set SYNC_RESULT=%ERRORLEVEL%
if "%SYNC_RESULT%"=="0" python tools\wait_key.py publish
if not "%SYNC_RESULT%"=="0" python tools\wait_key.py error
