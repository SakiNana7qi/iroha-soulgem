@echo off
setlocal

set SERVICE_NAME=SoulGemMonitor

echo Stopping service...
nssm stop %SERVICE_NAME%

echo Removing service...
nssm remove %SERVICE_NAME% confirm

echo Done.
