from fastapi import Header, HTTPException, Request

from config.settings import settings


async def verify_api_key(x_api_key: str = Header(...)) -> None:
    """Простая API-key авторизация для внутреннего API."""
    if x_api_key != settings.INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_pool(request: Request):
    return request.app.state.pool


def get_registry(request: Request):
    return request.app.state.registry


def get_cache(request: Request):
    return request.app.state.cache


def get_queue(request: Request):
    return request.app.state.queue


def get_monitor(request: Request):
    return request.app.state.monitor
