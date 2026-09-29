@echo off
rem =====================================================================
rem  Ewakuacja - Rzeszow: start aplikacji (Windows)
rem    start.bat          serwer lokalny + aplikacja w Chrome
rem    start.bat lan      dodatkowo dostep z sieci lokalnej (np. telefon)
rem    start.bat update   ponowna instalacja zaleznosci (po zmianie requirements.txt)
rem =====================================================================
setlocal
cd /d "%~dp0"
title Ewakuacja - Rzeszow (serwer)
set "PORT=8000"
set "URL=http://localhost:%PORT%"
set "VPY=.venv\Scripts\python.exe"
set "ARGS=--open --port %PORT%"
if /i "%~1"=="lan" set "ARGS=%ARGS% --lan"
if /i "%~1"=="update" if exist ".venv\.deps_ok" del ".venv\.deps_ok"

rem --- 1. srodowisko Pythona (.venv) - tylko za pierwszym razem -----------
if exist "%VPY%" goto venv_ok
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto no_python
%PY% -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto old_python
echo [1/3] Tworzenie srodowiska .venv ...
%PY% -m venv .venv || goto fail
:venv_ok

rem --- 2. zaleznosci - tylko za pierwszym razem (albo: start.bat update) ---
if exist ".venv\.deps_ok" goto deps_ok
echo [2/3] Instalacja zaleznosci (jednorazowo, kilka minut) ...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt || goto fail
echo ok> ".venv\.deps_ok"
:deps_ok

rem --- 3. serwer (jesli juz dziala - tylko otwieramy przegladarke) --------
"%VPY%" -c "import urllib.request as u; u.urlopen('%URL%/api/map', timeout=2)" >nul 2>nul
if not errorlevel 1 (
    echo Serwer juz dziala - otwieram %URL%
    start "" "%URL%"
    goto end
)
echo [3/3] Start serwera - aplikacja otworzy sie w Chrome.
"%VPY%" server.py %ARGS%
goto end

:no_python
echo.
echo  Nie znaleziono Pythona. Zainstaluj Python 3.11 lub nowszy: https://www.python.org/downloads/
echo  (w instalatorze zaznacz "Add python.exe to PATH"), potem uruchom start.bat ponownie.
goto fail_pause
:old_python
echo.
echo  Wymagany Python 3.11 lub nowszy. Zainstaluj nowsza wersje: https://www.python.org/downloads/
goto fail_pause
:fail
echo.
echo  Blad - szczegoly powyzej.
:fail_pause
pause
exit /b 1
:end
endlocal
