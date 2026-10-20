@echo off
rem setup.cmd: run setup.ps1 without changing the machine's PowerShell execution policy.
rem -ExecutionPolicy Bypass applies to this one process only. Double-click it, or run it from
rem cmd or PowerShell. Arguments pass through, for example: setup.cmd --notes ..\Agent-notes
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
