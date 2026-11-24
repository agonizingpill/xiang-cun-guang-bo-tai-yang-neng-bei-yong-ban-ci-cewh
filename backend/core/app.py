from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI
from sqlalchemy import text

from core import auth
from core.audit import record
from core.db import metadata, transaction
from core.config import SEED_DEMO
from core.middleware import install
from core.queueing import broker, claim, view
from domain.routes import routers
from scripts.seed_demo import seed

@asynccontextmanager
async def lifespan(app):
    from core.db import engine
    metadata.create_all(engine)
    if SEED_DEMO:
        try:
            seed()
        except Exception:
            logging.getLogger('uvicorn.error').exception('demo seed failed')
    yield

app = FastAPI(lifespan=lifespan)
install(app)
app.include_router(auth.router)
for router in routers:
    app.include_router(router)

@app.get('/health')
def health():
    with transaction() as conn:
        conn.execute(text('SELECT 1'))
    return {'ok': bool(broker.ping())}

@app.get('/compute/{token}')
def compute(token: str, user=Depends(auth.actor)):
    return view(user, token)


@app.post('/compute/{token}/takeover')
def takeover(token: str, user=Depends(auth.actor)):
    """中断续跑：沿用原计算号与冻结输入，递增 attempt 使原机器结果失效。"""
    output = claim(user, token)
    with transaction() as conn:
        record(conn, user, 'compute.takeover')
    return output
