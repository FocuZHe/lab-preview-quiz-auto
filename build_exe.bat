@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] 安装打包依赖...
python -m pip install -q --disable-pip-version-check pyinstaller pdfplumber
if errorlevel 1 goto :fail

echo [2/3] 开始打包（第一次会比较慢）...
python -m PyInstaller --onefile --console --noconfirm --name labquiz ^
  --distpath exe_dist --workpath exe_build --specpath exe_build ^
  --add-data "bank.json;." ^
  --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all PIL ^
  lab_quiz.py
if errorlevel 1 goto :fail

echo [3/3] 完成
echo.
echo 产物: exe_dist\labquiz.exe
echo 把 exe 和 bank.json 放在同一个目录即可使用。
echo.
pause
exit /b 0

:fail
echo.
echo 打包失败，请看上面的报错信息。
echo.
pause
exit /b 1
