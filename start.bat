@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   大学生职业网 CampusCareer V3.1
echo ============================================
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do set IP=%%b
)
start "" http://127.0.0.1:8080/login
echo 本机访问：http://127.0.0.1:8080
echo 局域网访问：http://%IP%:8080  （同一 Wi-Fi 下的其它电脑/手机可用）
echo.
echo 登录方式：打开登录页后使用「账号 + 密码」登录
echo 首次启动会在下面的日志里打印默认管理员账号与随机密码，请记录下来
echo 按 Ctrl+C 可停止服务
echo ============================================
python server.py --host 0.0.0.0 --port 8080
pause
