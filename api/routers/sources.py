from fastapi import APIRouter, Depends

from api.dependencies import get_registry, verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.get("/")
async def list_sources(registry=Depends(get_registry)) -> list[dict]:
    return [
        {
            "name":           s.name,
            "bot_username":   s.bot_username,
            "type":           s.source_type,
            "priority":       s.priority,
            "enabled":        s.enabled,
            "success_count":  s._success_count,
            "error_count":    s._error_count,
            "avg_response_ms": round(s.avg_response_ms, 2),
            "error_rate":     round(s.error_rate, 4),
        }
        for s in registry.all()
    ]


@router.post("/{name}/enable")
async def enable_source(name: str, registry=Depends(get_registry)) -> dict:
    source = registry.get(name)
    if not source:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Source not found")
    source.enabled = True
    return {"ok": True, "source": name, "action": "enabled"}


@router.post("/{name}/disable")
async def disable_source(name: str, registry=Depends(get_registry)) -> dict:
    source = registry.get(name)
    if not source:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Source not found")
    source.enabled = False
    return {"ok": True, "source": name, "action": "disabled"}
