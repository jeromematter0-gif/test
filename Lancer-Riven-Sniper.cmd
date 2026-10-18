@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Installer Python 3.11 ou plus recent, puis relancer ce fichier.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Installation impossible. Conserver le message d'erreur affiche ci-dessus.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m riven_sniper --open %*
set RIVEN_EXIT=%ERRORLEVEL%
pause
exit /b %RIVEN_EXIT%
