-- Создаём расширения если нет
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- для быстрого full-text поиска
CREATE EXTENSION IF NOT EXISTS btree_gin; -- для GIN индексов

-- Индекс на searches.query для аналитики
-- (создаётся после alembic migrate, но прописываем явно)
