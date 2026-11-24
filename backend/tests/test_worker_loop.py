"""真实 worker 线程循环与跨用户接手验证。

运行：.venv/bin/python -I backend/tests/test_worker_loop.py
"""
import os
import sys
import time
import types
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/hamlet_test2.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')

import fakeredis
import core.queueing as queueing

fake_server = fakeredis.FakeServer()
queueing.broker = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)

sklearn = types.ModuleType('sklearn')
linear_model = types.ModuleType('sklearn.linear_model')
metrics = types.ModuleType('sklearn.metrics')
linear_model.LinearRegression = object
metrics.mean_absolute_error = lambda *a, **k: 0.0
sklearn.linear_model = linear_model
sklearn.metrics = metrics
sys.modules.update({'sklearn': sklearn, 'sklearn.linear_model': linear_model,
                    'sklearn.metrics': metrics})

import threading
import core.worker as worker_mod
worker_mod.broker = queueing.broker

CALLS = []
# 门闩：只挡指定 token 的 calculate，精确模拟该任务“算到一半”，不影响其他任务。
# armed=True 表示下一个任务一旦进入 calculate 就阻塞，绑定 token 后只挡该 token。
gate = {'stall_token': None, 'armed': False}
release = threading.Event()
current_token = threading.local()
def fake_calculate(data):
    if gate['armed'] and gate['stall_token'] is None:
        gate['stall_token'] = getattr(current_token, 'token', None)
    if gate['stall_token'] is not None and getattr(current_token, 'token', None) == gate['stall_token']:
        release.wait(timeout=10)
    CALLS.append(len(data['rows']))
    return {'mae': 2.25, 'plan_wh': 77.0, 'held_out': 6, 'period': 24,
            'training': max(len(data['rows']) - 6, 0)}
# worker 在导入时已把 calculate 绑定到自身模块，必须 patch 它拿到的引用。
worker_mod.calculate = fake_calculate
# 包装 pull_job，让本轮处理的 token 在线程本地变量里可见，供门闩判断。
_orig_pull = worker_mod.pull_job
def tracked_pull():
    job, item = _orig_pull()
    if job is not None:
        current_token.token = job['token']
    return job, item
worker_mod.pull_job = tracked_pull

from fastapi.testclient import TestClient
from core.db import metadata, engine, transaction
from core.queueing import broker
from core import queueing as q
from domain.airslots_model import airslots
from domain.battery_logs_model import battery_logs
from core.app import app

PASS = FAIL = 0
def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {extra}')

def wait_status(client, headers, token, target, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f'/compute/{token}', headers=headers).json()
        if last['status'] == target:
            return last
        time.sleep(0.1)
    return last

def login(client, email):
    r = client.post('/auth/login', json={'email': email, 'password': 'DemoC0de!2026'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}

def main():
    if Path('/tmp/hamlet_test2.db').exists():
        Path('/tmp/hamlet_test2.db').unlink()
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
    op = login(client, 'operator@demo.test')
    rv = login(client, 'reviewer@demo.test')

    print('\n[A] 启动真实 worker 线程，任务端到端跑完')
    thread = threading.Thread(target=worker_mod.serve, daemon=True)
    thread.start()
    r = client.post('/reserves/forecast-wh', headers=op,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-04', 'clear': True})
    token = r.json()['token']
    final = wait_status(client, op, token, 'ready')
    check('worker 线程把任务跑到 ready', final['status'] == 'ready', str(final))
    check('结果来自 worker 计算', final['result']['plan_wh'] == 77.0, str(final.get('result')))
    check('processing 列表已清空', broker.llen(q.PROCESSING) == 0)

    print('\n[B] 原机器算到一半断线：复核员跨用户接手，worker 自动续跑且旧结果丢弃')
    calls_before = len(CALLS)
    # 新电表制造全新输入，避免与 [A] 的幂等键冲突，产生全新计算号。
    with transaction() as conn:
        conn.execute(battery_logs.insert(), [
            {'code': f'WB-{i:02d}', 'meter': 'BAT-08', 'hour': i,
             'load_wh': 95 + i % 4, 'clear': True, 'verified': True}
            for i in range(40)
        ])
    # 先武装门闩：下一个进入 calculate 的任务会被精确挡住，再提交。
    release.clear()
    gate['stall_token'] = None
    gate['armed'] = True
    # 作业员提交任务，随后由复核员跨用户接手。
    r = client.post('/reserves/forecast-wh', headers=op,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-08', 'clear': False})
    token2 = r.json()['token']
    # 等待 worker 取走并在 calculate 入口被门闩挡住。
    deadline = time.time() + 5
    while time.time() < deadline and gate['stall_token'] != token2:
        time.sleep(0.05)
    gate['armed'] = False
    check('门闩精确锁定目标任务', gate['stall_token'] == token2, str(gate))
    running = wait_status(client, op, token2, 'running')
    check('任务进入 running（原机器开始算）', running['status'] == 'running', str(running))
    # 任务已稳定停在计算中途，把心跳冻结成僵死状态。
    broker.hset(f'compute:{token2}', 'heartbeat', '0')
    # 复核员查看他人任务（规则允许）
    peek = client.get(f'/compute/' + token2, headers=rv)
    check('复核员可查看他人计算', peek.status_code == 200, peek.text)
    r = client.post(f'/compute/{token2}/takeover', headers=rv)
    check('复核员跨用户接手成功', r.status_code == 200, r.text)
    check('接手后 attempt=2、takeovers=1',
          r.json()['attempt'] == 2 and r.json()['takeovers'] == 1, str(r.json()))
    # 原机器“复活”完成计算：其回写必须被栅栏拒绝，任务仍由新一轮完成。
    gate['armed'] = False
    gate['stall_token'] = None
    release.set()
    final2 = wait_status(client, rv, token2, 'ready')
    check('worker 自动续跑至 ready', final2['status'] == 'ready', str(final2))
    check('最终轮次为第 2 轮', final2['attempt'] == 2, str(final2))
    check('两轮都实际执行了计算（首轮结果被丢弃，续跑轮被采用）',
          len(CALLS) - calls_before == 2, str(CALLS))

    print('\n[C] 排队期间连续接手：轮次递增，队列不堆积（停掉 worker 做确定性验证）')
    gate['armed'] = False
    gate['stall_token'] = None
    release.set()
    def stop_pulling(*a, **k):
        raise RuntimeError('stop worker thread for deterministic takeover test')
    worker_mod.pull_job = stop_pulling
    thread.join(timeout=5)
    check('worker 线程已停止', not thread.is_alive())
    with transaction() as conn:
        conn.execute(battery_logs.insert(), [
            {'code': f'W2-{i:02d}', 'meter': 'BAT-09', 'hour': i,
             'load_wh': 80 + i % 2, 'clear': False, 'verified': True}
            for i in range(40)
        ])
    r = client.post('/reserves/forecast-wh', headers=op,
                    json={'airslot': 'AIR-17', 'meter': 'BAT-09', 'clear': False})
    token3 = r.json()['token']
    check('新任务排队', r.json()['status'] == 'queued')
    r1 = client.post(f'/compute/{token3}/takeover', headers=op)
    queued_len = broker.llen(q.QUEUE)
    r2 = client.post(f'/compute/{token3}/takeover', headers=op)
    check('排队中可连续接手', r1.status_code == 200 and r2.status_code == 200,
          r1.text + r2.text)
    check('轮次递增到 3', r2.json()['attempt'] == 3 and r2.json()['takeovers'] == 2,
          str(r2.json()))
    check('多次接手队列只留一个条目', broker.llen(q.QUEUE) == queued_len == 1,
          f'queue={broker.llen(q.QUEUE)}')
    state = client.get(f'/compute/{token3}', headers=op).json()
    check('按计算号能看到已接手 2 次', state['takeovers'] == 2 and state['attempt'] == 3,
          str(state))

    print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)

if __name__ == '__main__':
    main()
