import hmac

from fastapi import Header, HTTPException, Request

from config.settings import settings


async def verify_api_key(x_api_key: str = Header(...)) -> None:
    """Простая API-key авторизация для внутреннего API."""
    # compare_digest — сравнение за постоянное время (защита от timing-атак)
    if not hmac.compare_digest(x_api_key.encode(), settings.INTERNAL_API_KEY.encode()):
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
