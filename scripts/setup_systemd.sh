#!/usr/bin/env bash
set -euo pipefail

echo "⚙️  Установка systemd сервисов..."

# Копируем unit-файлы
cp systemd/music-gateway-bot.service     /etc/systemd/system/
cp systemd/music-gateway-migrate.service /etc/systemd/system/
cp systemd/music-gateway-bot.timer       /etc/systemd/system/

# Перезагружаем systemd
systemctl daemon-reload

# Включаем автозапуск
systemctl enable music-gateway-migrate.service
systemctl enable music-gateway-bot.service
systemctl enable music-gateway-bot.timer

# Запускаем сейчас
systemctl start music-gateway-migrate.service
systemctl start music-gateway-bot.service

echo ""
echo "✅ Сервисы установлены!"
echo ""
echo "Полезные команды:"
echo "  systemctl status music-gateway-bot"
echo "  journalctl -u music-gateway-bot -f"
echo "  systemctl restart music-gateway-bot"
echo "  systemctl stop music-gateway-bot"
