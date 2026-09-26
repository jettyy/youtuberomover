@echo off
rem Silence Cut GUI launcher - double-click to open the window.
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0silence_cut_gui.pyw"
set "SILENCE_CUT_HIDE_CONSOLE=1"
set "FOUND="

if not exist "%SCRIPT%" goto notextracted
if not exist "%~dp0silence_cut.py" goto notextracted

echo Starting Silence Cut...
call :try py -3
if not defined FOUND call :try_dirs
if not defined FOUND call :try python
if not defined FOUND call :try python3
if not defined FOUND goto notfound
exit /b 0

rem ---- common install folders (python.org, install manager, Anaconda) ----
:try_dirs
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*" "%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%ProgramData%\anaconda3" "%ProgramData%\miniconda3") do (
    if not defined FOUND if exist "%%~D\python.exe" call :try "%%~D\python.exe"
)
exit /b 0

rem ---- try one python command: must run and have tkinter ----
:try
%* -c "import tkinter" >nul 2>&1
if errorlevel 1 exit /b 1
set "FOUND=1"
start "Silence Cut" /min %* "%SCRIPT%"
exit /b 0

:notextracted
echo.
echo [ERROR] Program files are missing next to this .bat file.
echo   Please EXTRACT the whole zip first (right-click the zip - "Extract All"),
echo   then double-click run_gui.bat inside the extracted folder.
echo.
pause
exit /b 1

:notfound
echo.
echo [ERROR] Could not start Python with tkinter.
echo.
echo ---- diagnostics: please send a screenshot of this window ----
echo [where]
where py python pythonw 2>&1
echo [py -0p]
py -0p 2>&1
echo [python]
python -c "import sys; print(sys.executable); import tkinter; print('tkinter OK')" 2>&1
echo --------------------------------------------------------------
echo.
echo If Python is not installed: https://www.python.org/downloads/
echo   - check "Add python.exe to PATH" during install
echo   - keep "tcl/tk and IDLE" checked
echo.
pause
exit /b 1
