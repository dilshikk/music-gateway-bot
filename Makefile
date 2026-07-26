.PHONY: help install lint lint-fix test test-unit test-int build up down logs migrate clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	pip install -r requirements.txt -r requirements-dev.txt

lint: ## Lint + format check
	ruff check . && ruff format --check .

lint-fix: ## Auto-fix linting
	ruff check --fix . && ruff format .

typecheck: ## Type checking
	mypy . --ignore-missing-imports

test: ## All tests with coverage
	pytest tests/ --cov=. --cov-report=term-missing --cov-report=html

test-unit: ## Unit tests only
	pytest tests/unit/ -v --timeout=10

test-int: ## Integration tests only
	pytest tests/integration/ -v --timeout=60

test-fast: ## Tests without coverage
	pytest tests/ -q --no-cov

build: ## Build Docker image
	docker compose build

up: ## Start services
	docker compose up -d

down: ## Stop services
	docker compose down

logs: ## Tail logs
	docker compose logs -f bot worker

migrate: ## Apply DB migrations
	alembic upgrade head

migrate-new: ## New migration (make migrate-new MSG="add table")
	alembic revision --autogenerate -m "$(MSG)"

rollback: ## Rollback one migration
	alembic downgrade -1

clean: ## Clean cache files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

shell-bot: ## Enter bot container
	docker compose exec bot bash

shell-db: ## PostgreSQL shell
	docker compose exec postgres psql -U postgres music_gateway
	
.PHONY: help lint test test-unit test-int build deploy clean

help:          ## Список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Разработка ────────────────────────────────────────────────────────────────
install:       ## Установить зависимости
	pip install -r requirements.txt -r requirements-dev.txt

lint:          ## Линтер + форматирование
	ruff check . && ruff format --check .

lint-fix:      ## Автоисправление линтера
	ruff check --fix . && ruff format .

typecheck:     ## Проверка типов
	mypy . --ignore-missing-imports

# ── Тесты ─────────────────────────────────────────────────────────────────────
test:          ## Все тесты с coverage
	pytest tests/ --cov=. --cov-report=term-missing --cov-report=html

test-unit:     ## Только юнит-тесты
	pytest tests/unit/ -v --timeout=10

test-int:      ## Только интеграционные тесты
	pytest tests/integration/ -v --timeout=60

test-fast:     ## Тесты без coverage (быстро)
	pytest tests/ -q --no-cov

# ── Docker ────────────────────────────────────────────────────────────────────
build:         ## Собрать Docker образ
	docker compose build

up:            ## Запустить сервисы
	docker compose up -d

down:          ## Остановить сервисы
	docker compose down

logs:          ## Логи в реальном времени
	docker compose logs -f bot worker

# ── БД ───────────────────────────────────────────────────────────────────────
migrate:       ## Применить миграции
	alembic upgrade head

migrate-new:   ## Создать новую миграцию (make migrate-new MSG="add table")
	alembic revision --autogenerate -m "$(MSG)"

rollback:      ## Откат на одну миграцию назад
	alembic downgrade -1

# ── Прочее ────────────────────────────────────────────────────────────────────
clean:         ## Очистка кэша и временных файлов
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

shell-bot:     ## Войти в контейнер бота
	docker compose exec bot bash

shell-db:      ## Psql в PostgreSQL
	docker compose exec postgres psql -U postgres music_gateway

