from fastapi import APIRouter, Depends, Request

from api.dependencies import get_cache, get_monitor, get_pool, verify_api_key
from infrastructure.monitoring.monitor import ServiceStatus, SystemSnapshot

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/")
async def health_check(
    pool=Depends(get_pool),
    cache=Depends(get_cache),
) -> dict:
    redis_ok = await cache.ping()
    stats    = pool.get_stats()

    status = "ok"
    if not redis_ok or stats["total"] == 0:
        status = "error"
    elif stats["idle"] == 0:
        status = "degraded"

    return {
        "status":       status,
        "redis":        redis_ok,
        "userbots":     stats,
    }


@router.get("/full")
async def full_health(monitor=Depends(get_monitor)) -> dict:
    snap: SystemSnapshot | None = monitor.get_snapshot()
    if not snap:
        return {"status": "no_data"}

    return {
        "status":       "error" if snap.has_errors else ("warn" if snap.has_warnings else "ok"),
        "cpu_percent":  snap.cpu_percent,
        "ram_percent":  snap.ram_percent,
        "ram_used_mb":  snap.ram_used_mb,
        "disk_percent": snap.disk_percent,
        "snapshot_at":  snap.snapshot_at,
        "checks": [
            {
                "name":       c.name,
                "status":     c.status.value,
                "message":    c.message,
                "latency_ms": round(c.latency_ms, 2),
            }
            for c in snap.checks
        ],
    }
