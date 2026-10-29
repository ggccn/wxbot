@echo off
setlocal
set PYTHONIOENCODING=utf-8
set HERE=%~dp0
if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" "%HERE%wxctl.py" %*
) else (
  python "%HERE%wxctl.py" %*
)
