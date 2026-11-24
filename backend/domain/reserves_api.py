from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError

from core.auth import actor, allow
from core.audit import record
from core.db import transaction
from core.records import scan
from domain import reserves_service as service
from domain.reserves_model import reserves
from domain.reserves_schemas import ForecastWh, SaveReserve

router = APIRouter()

@router.get('/reserves')
def list_reserves(user=Depends(actor)):
    with transaction() as conn:
        return scan(conn, reserves)


@router.post('/reserves/forecast-wh')
def forecast_wh(data: ForecastWh, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    with transaction() as conn:
        output = service.forecast_wh(conn, user, data)
        # 复用原计算号属于断线重试，不是新评估，不重复留痕。
        if not output.get('reused'):
            record(conn, user, 'reserves.forecast_wh')
        return output


@router.post('/reserves/save-reserve')
def save_reserve(data: SaveReserve, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    with transaction() as conn:
        try:
            output = service.save_reserve(conn, user, data)
        except IntegrityError:
            # 并发下两个请求同时备案同一计算号：回滚后返回已落库的原单。
            conn.rollback()
            existing = service.find_by_token(conn, data.token)
            if existing is None:
                raise
            return {**existing, 'reused': True}
        if not output.get('reused'):
            record(conn, user, 'reserves.save_reserve')
        return output
