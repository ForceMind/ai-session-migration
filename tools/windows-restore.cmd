@echo off
setlocal
chcp 65001 >nul
set "ASM_PROJECT=%~dp0.."
set "PYTHONPATH=%ASM_PROJECT%\src"
set "ASM_CMD=-B -X utf8 -m ai_session_migration.windows_restore.guide"
if exist "%ASM_PROJECT%\.venv\Scripts\python.exe" (
    "%ASM_PROJECT%\.venv\Scripts\python.exe" %ASM_CMD% %*
    exit /b
)
where py.exe >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 and the project dependencies are required.
    echo See docs\WINDOWS-RESTORE.md.
    exit /b 1
)
py -3.12 %ASM_CMD% %*
exit /b
