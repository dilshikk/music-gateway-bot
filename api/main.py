"""
FastAPI внутренний API.
Используется для:
  - мониторинга (дашборд, health check)
  - управления компонентами из внешних систем
  - интеграций (вебхуки, CI/CD хуки)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import health, stats, userbots, sources


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield  # компоненты монтируются снаружи через app.state


app = FastAPI(
    title="Music Gateway API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router,    prefix="/health",    tags=["Health"])
app.include_router(stats.router,     prefix="/stats",     tags=["Stats"])
app.include_router(userbots.router,  prefix="/userbots",  tags=["Userbots"])
app.include_router(sources.router,   prefix="/sources",   tags=["Sources"])
