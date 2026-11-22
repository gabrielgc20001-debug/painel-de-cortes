@echo off
chcp 65001 >nul
title Painel de Cortes - TikTok
set "PASTA=%USERPROFILE%\Documents\Painel de Cortes"
if not exist "%PASTA%" mkdir "%PASTA%"
if /I not "%~dp0"=="%PASTA%\" copy /Y "%~f0" "%PASTA%\Iniciar Painel TikTok.bat" >nul
cd /d "%PASTA%"

if not exist painel_tiktok.py (
  echo Baixando o servidor do Painel...
  powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/gabrielgc20001-debug/painel-de-cortes/main/painel_tiktok.py' -OutFile 'painel_tiktok.py'"
)

set "PY="
py -3 --version >nul 2>&1 && set "PY=py"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY (
  echo Python nao encontrado. Instalando Python 3.12 ^(pode pedir confirmacao do Windows^)...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

echo.
if "%PY%"=="py" (py -3 painel_tiktok.py) else ("%PY%" painel_tiktok.py)
pause
