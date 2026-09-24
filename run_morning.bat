@echo off
:: Morning full briefing (called at 07:00)
cd /d "%~dp0"
python hotelrunner_daily_summary.py --max-pages 15 --page-delay 1
python telegram_send.py --mode full --team-only
python manager_report.py --send
