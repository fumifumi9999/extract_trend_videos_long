@echo off
rem ===========================================================================
rem run_daily.cmd - wrapper for Windows Task Scheduler (see README.md).
rem
rem   run_daily.cmd --dry-run   preview only, sends no mail, records nothing
rem   run_daily.cmd             the real thing
rem
rem Task Scheduler inherits neither PATH nor the working directory nor a usable
rem console code page, so everything is pinned down here.
rem
rem NOTE: keep this file ASCII-only with CRLF line endings. cmd.exe parses batch
rem files with the console code page, so non-ASCII comments can break parsing.
rem ===========================================================================
setlocal

rem Force UTF-8 output. The log is redirected to a file, and with the default
rem cp932 a video title containing an emoji raises UnicodeEncodeError and kills
rem the whole run.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Flush every line. When stdout is a file Python buffers 8KB at a time, so a
rem run that hangs or gets killed leaves a log that stops far before the real
rem stopping point (this hid a network hang for three days in Sept 2026).
set "PYTHONUNBUFFERED=1"

rem Run in this file's folder: the CSV and trend_videos.db land there.
cd /d "%~dp0"

rem Call uv by absolute path; Task Scheduler's PATH does not have it.
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" set "UV=uv"

rem One log file per day.
if not exist "logs" mkdir "logs"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%d"
set "LOG=logs\%TODAY%.log"

echo ============================================================ >> "%LOG%"
echo [%DATE% %TIME%] start %* >> "%LOG%"

"%UV%" run main.py %* >> "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"

echo [%DATE% %TIME%] exit=%CODE% >> "%LOG%"

rem Drop logs older than 30 days.
powershell -NoProfile -Command "Get-ChildItem 'logs\*.log' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force" >nul 2>&1

rem Propagate the exit code so Task Scheduler's "Last Run Result" is meaningful.
exit /b %CODE%
