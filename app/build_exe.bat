@echo off
rem ==== NVR_Export 一键打包(需 Python 3.9+ 已安装并勾选 Add to PATH)====
cd /d %~dp0
python --version || goto :nopython
python -m pip install --upgrade pyinstaller requests || goto :err
python -m PyInstaller --noconfirm --clean --onefile --windowed --name NVR_Export --icon icon.ico --add-data "icon.ico;." --paths ..\scripts nvr_gui.py || goto :err
echo.
echo 构建完成: %~dp0dist\NVR_Export.exe
exit /b 0
:nopython
echo [错误] 未找到 python,请先安装 Python 3.9+ 并勾选 Add to PATH
exit /b 1
:err
echo [错误] 构建失败,请查看上方日志
exit /b 1
