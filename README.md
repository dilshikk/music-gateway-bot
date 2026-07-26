# 🎵 Music Gateway Telegram Platform

Production-ready high-load Telegram bot platform for music search and delivery.

## Stack

- **Python 3.13+**
- **aiogram 3.x** — Telegram bot framework
- **Pyrogram** — userbot pool for source interaction
- **PostgreSQL 16** — primary database
- **Redis 7** — caching, rate limiting, history
- **SQLAlchemy 2** (async) + **Alembic** — ORM and migrations
- **APScheduler 3** — scheduled tasks
- **FastAPI** — internal monitoring API
- **Docker + Compose** — containerization
- **Nginx** — reverse proxy
- **systemd** — process management

## Features

- 🔍 Music search via Telegram bots (VK Music Bot + plugin system)
- 🤖 Userbot pool with Round-Robin + weight balancing
- 📦 Redis cache — search results, audio file_id, rate limits
- ⚡ Priority queue — Premium users get HIGH priority
- 🌐 i18n — RU / UZ / EN (Fluent .ftl)
- 📊 Monitoring + Watchdog + FastAPI internal API
- 🔁 Inline mode — search directly from any chat
- ⭐ Favorites, history, popular queries
- 🧪 83 pytest tests (unit + integration)
- 🚀 CI/CD — GitHub Actions + blue-green deploy

## Project Structure

```
music_gateway/
├── bot/                    # aiogram bot
│   ├── handlers/           # message & callback handlers
│   ├── middlewares/        # auth, rate limit, i18n, subscription
│   ├── keyboards/          # inline & reply keyboards
│   └── filters/            # admin filters
├── core/                   # business logic
│   ├── cache_manager.py    # Redis operations
│   ├── userbot_pool.py     # Pyrogram pool
│   ├── search_manager.py   # search orchestration
│   ├── queue_manager.py    # priority queue
│   └── worker.py           # queue workers
├── sources/                # music source plugins
│   ├── base.py             # abstract MusicSource
│   ├── registry.py         # source registry
│   └── vk_music_bot.py     # VK Music Bot parser
├── infrastructure/
│   ├── database/           # models, repositories, migrations
│   ├── i18n/               # Fluent translator
│   ├── monitoring/         # monitor + watchdog
│   └── scheduler/          # APScheduler tasks
├── api/                    # FastAPI internal API
├── config/                 # settings + .env.example
├── locales/                # ru.ftl, uz.ftl, en.ftl
├── docker/                 # docker-compose, nginx, postgres init
├── systemd/                # service units
├── scripts/                # deploy, setup, test runner
└── tests/                  # 83 pytest tests
```

## Quick Start

```bash
# 1. Clone
git clone https://github.com/dilshikk/music-gateway-bot.git
cd music-gateway-bot

# 2. Configure
cp config/.env.example .env
nano .env

# 3. Deploy
bash scripts/deploy.sh
```

## Development

```bash
# Install deps
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
make test

# Lint
make lint

# Start services
make up
```

## Architecture

```
User → Telegram → aiogram bot
                      ↓
               QueueManager (Priority)
                      ↓
               SearchManager
                 ↓         ↓
           Redis cache   UserbotPool
           (hit → skip)      ↓
                        Pyrogram client
                             ↓
                       @vkmusic_bot
                             ↓
                       Audio file_id
                             ↓
                     User gets audio ✓
```

## License

MIT
