@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::Out.Write($env:RM_SSH_PASSWORD)"
