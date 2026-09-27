@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  实验预习答题系统 · 一键答题（双击本文件即可）
REM  - 优先用下面这个 Python，它自带 pdfplumber
REM  - 找不到就退回系统的 py 或 python，缺的库脚本会自己装
REM  - 换电脑或换环境时，只改下面 PYEXE 那一行
REM ============================================================

set "PYEXE="
REM 想指定用哪个 Python？把下面那行的 REM 去掉并改成你的路径，
REM 或者先设环境变量 LABQUIZ_PY。都不设就自动从 PATH 里找。
REM set "PYEXE=C:\Python313\python.exe"
set "PYARG="
set "PYTHONIOENCODING=utf-8"

REM 想临时指定别的解释器，可以先设环境变量 LABQUIZ_PY
if defined LABQUIZ_PY set "PYEXE=%LABQUIZ_PY%"

if exist "%PYEXE%" goto :have_py

set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=py"
  set "PYARG=-3"
  goto :have_py
)
where python >nul 2>nul
if not errorlevel 1 (
  set "PYEXE=python"
  goto :have_py
)

echo.
echo [错误] 这台电脑上没找到 Python。
echo.
echo   1. 到 https://www.python.org/downloads/ 装一个 Python 3
echo      安装时记得勾选 "Add python.exe to PATH"
echo   2. 装完再双击本文件即可，缺的库脚本会自己补装
echo.
echo   如果 Python 装在别处：右键本文件 - 编辑 -
echo   把 PYEXE 那一行改成你的 python.exe 完整路径
echo.
pause
exit /b 1

:have_py
if defined PYARG (
  %PYEXE% %PYARG% -X utf8 "%~dp0lab_quiz.py" interactive %*
) else (
  "%PYEXE%" -X utf8 "%~dp0lab_quiz.py" interactive %*
)

echo.
pause
endlocal
