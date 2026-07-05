@echo off
cd /d "%~dp0"
echo Starting Canadian Deals Bot... (close this window to stop it)
where python >nul 2>nul
if %errorlevel%==0 (
    python deals_bot.py --loop
) else (
    py deals_bot.py --loop
)
pause
