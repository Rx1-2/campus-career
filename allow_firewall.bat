@echo off
chcp 65001 >nul
echo 正在为 CampusCareer 添加 Windows 防火墙入站规则（TCP 8080）...
powershell -NoProfile -Command "Start-Process -FilePath netsh -ArgumentList 'advfirewall firewall add rule name=\"CampusCareer 8080\" dir=in action=allow protocol=TCP localport=8080' -Verb RunAs -Wait"
echo.
netsh advfirewall firewall show rule name="CampusCareer 8080"
pause
