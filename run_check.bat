@echo off
:: Hourly change check (called every hour 08:00 - 00:00)
cd /d "%~dp0"
python hotelrunner_daily_summary.py --max-pages 15 --page-delay 1 --no-dashboard
python telegram_send.py --mode check
