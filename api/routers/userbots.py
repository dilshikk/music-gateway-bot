from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import get_pool, verify_api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])


class UserbotResponse(BaseModel):
    id:             int
    phone:          str
    status:         str
    weight:         int
    requests_today: int
    requests_total: int
    daily_limit:    int
    error_count:    int


@router.get("/", response_model=list[UserbotResponse])
async def list_userbots(pool=Depends(get_pool)) -> list[UserbotResponse]:
    return [
        UserbotResponse(
            id             = e.id,
            phone          = e.model.phone,
            status         = e.model.status.value,
            weight         = e.model.weight,
            requests_today = e.model.requests_today,
            requests_total = e.model.requests_total,
            daily_limit    = e.model.daily_limit,
            error_count    = e.model.error_count,
        )
        for e in pool.list_userbots()
    ]


@router.post("/{userbot_id}/enable")
async def enable_userbot(userbot_id: int, pool=Depends(get_pool)) -> dict:
    await pool.enable_userbot(userbot_id)
    return {"ok": True, "userbot_id": userbot_id, "action": "enabled"}


@router.post("/{userbot_id}/disable")
async def disable_userbot(userbot_id: int, pool=Depends(get_pool)) -> dict:
    await pool.disable_userbot(userbot_id)
    return {"ok": True, "userbot_id": userbot_id, "action": "disabled"}


@router.post("/{userbot_id}/restart")
async def restart_userbot(userbot_id: int, pool=Depends(get_pool)) -> dict:
    ok = await pool.restart_userbot(userbot_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Restart failed")
    return {"ok": True, "userbot_id": userbot_id, "action": "restarted"}
