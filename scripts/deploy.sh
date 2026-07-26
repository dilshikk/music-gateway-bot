#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/music_gateway"
REPO_URL="https://github.com/yourname/music-gateway.git"
BRANCH="${1:-main}"

echo "🚀 Деплой ветки: $BRANCH"

# ── Подготовка ──────────────────────────────────────────────────────────────
if [ ! -d "$APP_DIR/.git" ]; then
    echo "📦 Первичное клонирование..."
    git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

echo "📥 Обновление кода..."
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# ── Проверка .env ────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "❌ Файл .env не найден!"
    echo "   Скопируй: cp config/.env.example .env"
    echo "   И заполни переменные."
    exit 1
fi

# ── Сборка и запуск ──────────────────────────────────────────────────────────
echo "🔨 Сборка Docker образов..."
docker compose build --no-cache

echo "🗄️  Запуск миграций БД..."
docker compose run --rm migrate

echo "🔄 Перезапуск сервисов..."
docker compose down --timeout 30
docker compose up -d --remove-orphans

echo "⏳ Ожидание готовности..."
sleep 5

# ── Healthcheck ──────────────────────────────────────────────────────────────
echo "🔍 Проверка здоровья..."
MAX_TRIES=12
TRIES=0

until curl -sf "http://localhost:8000/health" > /dev/null 2>&1; do
    TRIES=$((TRIES + 1))
    if [ $TRIES -ge $MAX_TRIES ]; then
        echo "❌ Сервис не ответил за 60 секунд!"
        docker compose logs --tail=50
        exit 1
    fi
    echo "   Ожидание... ($TRIES/$MAX_TRIES)"
    sleep 5
done

echo ""
echo "✅ Деплой успешно завершён!"
echo "   Статус: $(curl -s http://localhost:8000/health | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[\"status\"])')"
echo ""
docker compose ps
