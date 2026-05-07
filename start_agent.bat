@echo off
echo Starting Cinerama Screenshot Agent...
echo Keep this window open or minimized for the screenshot buttons to work!
echo.
call venv\Scripts\activate.bat
python screenshot_agent.py
pause
