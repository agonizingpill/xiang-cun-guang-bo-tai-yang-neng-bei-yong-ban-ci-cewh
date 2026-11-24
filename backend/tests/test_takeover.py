"""端到端验证：计算中断接手、栅栏丢弃旧结果、幂等重试、幂等备案。

运行：.venv/bin/python -I backend/tests/test_takeover.py
用 fakeredis 代替 Redis、sqlite 代替 MySQL，并 mock 掉 sklearn 计算。
"""
import os
import sys
import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/hamlet_test.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')

import fakeredis
import core.queueing as queueing

# 必须在 import worker / app 之前替换掉真实 Redis 连接。
fake_server = fakeredis.FakeServer()
queueing.broker = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)

# mock 掉 sklearn：测试环境不装重型依赖，先占位再导入。
import types
sklearn = types.ModuleType('sklearn')
linear_model = types.ModuleType('sklearn.linear_model')
metrics = types.ModuleType('sklearn.metrics')
linear_model.LinearRegression = object
metrics.mean_absolute_error = lambda *a, **k: 0.0
sklearn.linear_model = linear_model
sklearn.metrics = metrics
sys.modules.update({'sklearn': sklearn, 'sklearn.linear_model': linear_model,
                    'sklearn.metrics': metrics})

import core.worker as worker_mod
worker_mod.broker = queueing.broker

import domain.learning as learning
learning.calculate = lambda data: {
    'mae': 1.5, 'plan_wh': 42.0, 'held_out': 6, 'period': 24,
    'training': max(len(data['rows']) - 6, 0),
}

from fastapi.testclient import TestClient
from sqlalchemy import select
from core.db import metadata, engine, transaction
from core.queueing import broker
from core import queueing as q
from domain.airslots_model import airslots
from domain.battery_logs_model import battery_logs
from domain.reserves_model import reserves
from core.app import app

PASS, FAIL = 0, 0
def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {extra}')

def main():
    if Path('/tmp/hamlet_test.db').exists():
        Path('/tmp/hamlet_test.db').unlink()
    metadata.create_all(engine)
    with transaction() as conn:
        conn.execute(airslots.insert(), [
            {'code': 'AIR-17', 'title': '晚间村讯', 'hour': 18, 'license': '本地插播', 'offline': True},
        ])
        conn.execute(battery_logs.insert(), [
            {'code': f'WH-{i:02d}', 'meter': 'BAT-04', 'hour': i,
             'load_wh': 90 + i % 3, 'clear': True, 'verified': True}
            for i in range(40)
        ])

    client = TestClient(app)
    login = client.post('/auth/login', json={'email': 'operator@demo.test',
                                             'password': 'DemoC0de!2026'})
    assert login.status_code == 200, login.text
    headers = {'Authorization': 'Bearer ' + login.json()['token']}

    print('\n[1] 首次提交计算，得到计算号')
    r = client.post('/reserves/forecast-wh', headers=headers,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-04', 'clear': True})
    check('提交成功', r.status_code == 200, r.text)
    first = r.json()
    token = first['token']
    check('首轮 attempt=1、无接手', first['attempt'] == 1 and first['takeovers'] == 0
          and first['reused'] is False, str(first))

    print('\n[2] 断线重试：同输入不新增评估，返回原计算号')
    r = client.post('/reserves/forecast-wh', headers=headers,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-04', 'clear': True})
    again = r.json()
    check('返回同一个 token', again['token'] == token, str(again))
    check('标记 reused=true', again['reused'] is True)
    check('队列里只有一个任务', broker.llen(q.QUEUE) == 1,
          f"queue={broker.llen(q.QUEUE)}")

    print('\n[3] worker 取走任务（模拟原机器开始计算）')
    job, item = q.pull_job()
    check('取到任务且 attempt=1', job['token'] == token and job['attempt'] == 1, str(job))
    state = client.get(f'/compute/{token}', headers=headers).json()
    check('状态变为 running', state['status'] == 'running', str(state))

    print('\n[4] 原机器算到一半断线：接手续跑，沿用原计算号与输入')
    # running 未过租期，先验证租期保护
    r = client.post(f'/compute/{token}/takeover', headers=headers)
    check('租期内拒绝接手', r.status_code == 409, r.text)
    # 模拟心跳过期
    broker.hset(f'compute:{token}', 'heartbeat', '0')
    r = client.post(f'/compute/{token}/takeover', headers=headers)
    check('过期后接手成功', r.status_code == 200, r.text)
    taken = r.json()
    check('接手沿用原 token', taken['token'] == token)
    check('attempt 递增到 2', taken['attempt'] == 2 and taken['takeovers'] == 1, str(taken))
    check('接手后状态回到 queued', taken['status'] == 'queued')
    check('输入快照未变（审签读数仍在）',
          len(taken['input']['rows']) == 40 and taken['input']['window']['code'] == 'AIR-17')

    print('\n[5] 原机器"复活"交回结果：必须被栅栏丢弃')
    stale_payload = {'mae': 999.0, 'plan_wh': 999.0, 'held_out': 6, 'period': 24, 'training': 34}
    accepted = q.finish_job(job, item, 'ready', stale_payload)
    check('旧 attempt 回写被拒', accepted is False)
    state = client.get(f'/compute/{token}', headers=headers).json()
    check('旧结果没有覆盖任务', state['status'] == 'queued'
          and state.get('result') is None, str(state))

    print('\n[6] 新 worker 用新 attempt 续跑并回写')
    job2, item2 = q.pull_job()
    check('新轮次 attempt=2', job2['attempt'] == 2, str(job2))
    accepted = q.finish_job(job2, item2, 'ready',
                            {'mae': 1.5, 'plan_wh': 42.0, 'held_out': 6,
                             'period': 24, 'training': 34})
    check('新 attempt 回写被接受', accepted is True)
    state = client.get(f'/compute/{token}', headers=headers).json()
    check('任务 ready 且显示已接手 1 次',
          state['status'] == 'ready' and state['takeovers'] == 1 and state['attempt'] == 2,
          str(state))
    check('采用的是新结果', state['result']['plan_wh'] == 42.0, str(state['result']))

    print('\n[7] 已结束任务不能再接手')
    r = client.post(f'/compute/{token}/takeover', headers=headers)
    check('ready 任务接手返回 409', r.status_code == 409, r.text)

    print('\n[8] 备案：算完即可保存')
    r = client.post('/reserves/save-reserve', headers=headers,
                    json={'code': 'RV-24', 'token': token})
    check('备案成功', r.status_code == 200, r.text)
    saved = r.json()
    check('备案写入新结果且 reused=false',
          saved['plan_wh'] == 42.0 and saved['reused'] is False, str(saved))

    print('\n[9] 已备案拿原计算号重试：返回原单，不新增评估')
    before = None
    with transaction() as conn:
        before = conn.execute(select(reserves)).mappings().all()
    r = client.post('/reserves/save-reserve', headers=headers,
                    json={'code': 'RV-24', 'token': token})
    check('重试仍 200', r.status_code == 200, r.text)
    again_saved = r.json()
    check('返回原单且 reused=true', again_saved['code'] == 'RV-24'
          and again_saved['reused'] is True, str(again_saved))
    with transaction() as conn:
        after = conn.execute(select(reserves)).mappings().all()
    check('台账仍是一条', len(after) == 1 and len(before) == 1, f'{len(before)}->{len(after)}')

    print('\n[10] worker 启动恢复：processing 里的僵死任务自动重新入队并递增 attempt')
    r = client.post('/reserves/forecast-wh', headers=headers,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-04', 'clear': False})
    token2 = r.json()['token']
    q.pull_job()  # 模拟 worker 取走后崩溃
    check('任务在 processing', broker.llen(q.PROCESSING) >= 1)
    q.requeue_processing()
    state = client.get(f'/compute/{token2}', headers=headers).json()
    check('崩溃任务回到 queued 且 attempt=2',
          state['status'] == 'queued' and state['attempt'] == 2, str(state))
    check('processing 已清空', broker.llen(q.PROCESSING) == 0)

    print('\n[11] 权限：只读访客不能接手')
    vlogin = client.post('/auth/login', json={'email': 'viewer@demo.test',
                                              'password': 'DemoC0de!2026'}).json()
    vh = {'Authorization': 'Bearer ' + vlogin['token']}
    r = client.post(f'/compute/{token2}/takeover', headers=vh)
    check('viewer 接手被拒 403', r.status_code == 403, r.text)
    r = client.get(f'/compute/{token2}', headers=vh)
    check('viewer 不能查看他人计算 403', r.status_code == 403, r.text)

    print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)

if __name__ == '__main__':
    main()
