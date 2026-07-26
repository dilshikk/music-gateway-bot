#!/usr/bin/env bash
set -euo pipefail

echo "🧪 Запуск тестов..."

# Unit тесты (быстро, без зависимостей)
echo ""
echo "── Unit тесты ──────────────────────────"
pytest tests/unit/ -v --timeout=10

# Интеграционные тесты
echo ""
echo "── Интеграционные тесты ─────────────────"
pytest tests/integration/ -v --timeout=30

# Полный прогон с coverage
echo ""
echo "── Coverage отчёт ───────────────────────"
pytest tests/ --cov=. --cov-report=term-missing

echo ""
echo "✅ Все тесты прошли!"
