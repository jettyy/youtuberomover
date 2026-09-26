@echo off
rem Silence Cut GUI launcher - double-click to open the window.
cd /d "%~dp0"

where pyw >/dev/null 2>nul
if %errorlevel%==0 (
    pyw -c "import tkinter" >/dev/null 2>/dev/null && (start "" pyw "%~dp0silence_cut_gui.pyw" & exit /b 0)
)
where pythonw >/dev/null 2>nul
if %errorlevel%==0 (
    pythonw -c "import tkinter" >/dev/null 2>/dev/null && (start "" pythonw "%~dp0silence_cut_gui.pyw" & exit /b 0)
)

echo.
echo [ERROR] Python (with tkinter) was not found.
echo   1) Install Python from https://www.python.org/downloads/
echo   2) During install, check "Add python.exe to PATH" and keep "tcl/tk and IDLE" checked.
echo   3) Then double-click this file again.
echo.
pause
