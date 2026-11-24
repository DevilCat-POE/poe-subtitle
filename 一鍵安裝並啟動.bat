@echo off
chcp 65001 >nul
title 流亡黯道 字幕工坊 — 安裝與啟動
cd /d "%~dp0"

set "PYEXE="
py -3 --version >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python --version >nul 2>nul && set "PYEXE=python"
if defined PYEXE goto havepy

echo [1/4] 這台電腦還沒有 Python，嘗試自動安裝...
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
echo.
echo 安裝完成後，請關閉這個視窗，再雙擊一次本檔案。
echo 如果上面出現錯誤，請到 https://www.python.org/downloads/ 手動安裝，
echo 安裝畫面第一頁務必勾選 Add python.exe to PATH。
pause
exit /b

:havepy
if exist ".venv\Scripts\pythonw.exe" goto havevenv
echo [1/4] 建立程式環境...
%PYEXE% -m venv .venv
if errorlevel 1 goto fail

:havevenv
fc /b "requirements.txt" ".venv\requirements.installed" >nul 2>nul
if not errorlevel 1 goto havereq
echo [2/4] 安裝／更新辨識套件，約需數分鐘，請稍候，不要關閉視窗...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
copy /y "requirements.txt" ".venv\requirements.installed" >nul

:havereq
if exist ".venv\cuda.flag" goto havecuda
echo [3/4] 安裝顯示卡加速元件（約 1GB）。沒有 NVIDIA 顯示卡也沒關係，失敗會自動略過...
".venv\Scripts\python.exe" -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
echo ok> ".venv\cuda.flag"

:havecuda
where ffmpeg >nul 2>nul && goto haveff
if exist "ffmpeg.exe" goto haveff
echo [4/4] 安裝 ffmpeg（轉換影片聲音用）...
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
echo.
echo ffmpeg 安裝完成後，請關閉這個視窗，再雙擊一次本檔案。
pause
exit /b

:haveff
if exist ".venv\shortcut.flag" goto launch
set "SC_DIR=%~dp0"
set "SC_NAME=流亡黯道字幕工坊"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:SC_DIR; $s=(New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), $env:SC_NAME + '.lnk')); $s.TargetPath=Join-Path $d '.venv\Scripts\pythonw.exe'; $s.Arguments='poe_subtitle_gui.py'; $s.WorkingDirectory=$d; $s.IconLocation=Join-Path $d 'icon.ico'; $s.Save()" >nul 2>nul
echo ok> ".venv\shortcut.flag"
echo 已在桌面建立捷徑「流亡黯道字幕工坊」，之後可以直接點它開啟。

:launch
start "" ".venv\Scripts\pythonw.exe" poe_subtitle_gui.py
exit /b

:fail
echo.
echo 安裝失敗。請把這個視窗截圖，到 GitHub 的 Issues 回報：
echo https://github.com/DevilCat-POE/poe-subtitle/issues
pause
exit /b
