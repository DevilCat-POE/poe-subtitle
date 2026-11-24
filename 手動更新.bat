@echo off
chcp 65001 >nul
title 流亡黯道 字幕工坊 — 手動更新
cd /d "%~dp0"
rem 介面打不開時用：直接從 GitHub 下載最新版（個人詞庫與設定不會被覆蓋）
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" updater.py
) else (
  py -3 updater.py 2>nul || python updater.py
)
echo.
pause
