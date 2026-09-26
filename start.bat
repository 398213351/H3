@echo off
chcp 65001 >nul
cd /d %~dp0
echo Starting SiftQ MiniMax-H3 gateway on http://127.0.0.1:8787 ...
python gateway.py
pause
