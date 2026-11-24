from sqlalchemy import select

from core.queueing import submit, result
from core.records import get, insert, need, scan
from domain.airslots_model import airslots
from domain.battery_logs_model import battery_logs
from domain.reserves_model import reserves
from domain.switchcards_model import switchcards


def forecast_wh(conn, user, data):
    slot = get(conn, airslots, data.airslot)
    rows = sorted([x for x in scan(conn, battery_logs) if x['meter'] == data.meter and x['verified']],
                  key=lambda x: x['hour'])
    need(len(rows) >= 36 and all(y['hour'] - x['hour'] == 1 for x, y in zip(rows, rows[1:])),
         '要有连续三十六小时审签读数')
    # submit 内部按“提交人 + 输入指纹”幂等：断线重试返回原计算号，不新增评估。
    return submit(user, {'window': slot, 'rows': rows, 'clear': data.clear})


def find_by_token(conn, token):
    row = conn.execute(select(reserves).where(reserves.c.token == token)).mappings().first()
    return dict(row) if row is not None else None


def save_reserve(conn, user, data):
    # 已备案：拿原计算号重试直接返回原单，不新增评估、不重复写台账。
    existing = find_by_token(conn, data.token)
    if existing is not None:
        return {**existing, 'reused': True}
    run = result(user, data.token)
    window = run['input']['window']
    stats = run['result']
    need(get(conn, airslots, window['code'])['hour'] == window['hour'], '节目时间已变')
    row = insert(conn, reserves, code=data.code, airslot=window['code'], plan_wh=stats['plan_wh'],
                 fallback='本地优先' if window['offline'] else '受控中继',
                 mae=stats['mae'], token=data.token)
    return {**row, 'reused': False}
