from fastapi import APIRouter, Depends

from api.dependencies import get_cache, get_pool, get_queue, verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/")
async def get_stats(
    pool=Depends(get_pool),
    queue=Depends(get_queue),
    cache=Depends(get_cache),
) -> dict:
    pool_stats  = pool.get_stats()
    queue_stats = queue.get_stats()
    popular     = await cache.get_popular(limit=10)

    return {
        "userbots": pool_stats,
        "queue":    queue_stats,
        "popular_queries": [
            {"query": q, "count": int(c)} for q, c in popular
        ],
    }
