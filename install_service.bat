@echo off
chcp 65001 >nul
rem Установка службы CoolPrint. Запускать от имени администратора.
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Запустите этот файл от имени администратора.
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
    python -m venv .venv || exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
rem pywin32: регистрация DLL для служб
.venv\Scripts\python.exe .venv\Scripts\pywin32_postinstall.py -install >nul 2>&1

.venv\Scripts\python.exe windows_service.py --startup auto install || exit /b 1
rem Перезапуск при сбое: через 10 с, 10 с, 60 с
sc failure CoolPrint reset= 86400 actions= restart/10000/restart/10000/restart/60000 >nul
netsh advfirewall firewall add rule name="CoolPrint web" dir=in action=allow protocol=TCP localport=8765 profile=private >nul
.venv\Scripts\python.exe windows_service.py start

echo.
echo Готово. Веб-интерфейс: http://localhost:8765/
echo Токен доступа: data\web_token.txt (или [web] token в config.ini)
echo Лог: data\coolprint.log
endlocal
