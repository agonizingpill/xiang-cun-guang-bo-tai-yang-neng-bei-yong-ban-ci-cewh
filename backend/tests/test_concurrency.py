"""并发栅栏压力测试：同时接手与回写下，只有当前 attempt 能落库。

运行：.venv/bin/python -I backend/tests/test_concurrency.py
"""
import os
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DATABASE_URL', 'sqlite:////tmp/hamlet_conc.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')

import fakeredis
import core.queueing as queueing

queueing.broker = fakeredis.FakeStrictRedis(decode_responses=True)
from core.queueing import broker
from core import queueing as q

PASS = FAIL = 0
def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {extra}')

# 直接构造一个 running 任务，绕过 HTTP 层聚焦队列原语。
def seed_running(attempt=1):
    token = 'tok-conc'
    broker.delete(f'compute:{token}', q.QUEUE, q.PROCESSING)
    broker.hset(f'compute:{token}', mapping={
        'token': token, 'owner': 'op@x', 'submitter': 'op@x', 'status': 'running',
        'attempt': attempt, 'input': '{"rows": []}', 'result': '', 'detail': '',
        'heartbeat': '0',
    })
    item = f'{{"token": "{token}", "owner": "op@x", "attempt": {attempt}}}'
    broker.rpush(q.PROCESSING, item)
    return token, item

def main():
    print('\n[1] 多个旧 attempt 并发回写，全部被拒，任务状态不变')
    token, item = seed_running(3)
    results = []
    barrier = threading.Barrier(8)
    def stale_finish(attempt):
        barrier.wait()
        job = {'token': token, 'owner': 'op@x', 'attempt': attempt}
        results.append(q.finish_job(job, item, 'ready', {'plan_wh': attempt}))
    threads = [threading.Thread(target=stale_finish, args=(a,)) for a in (1, 1, 2, 2, 1, 2, 1, 2)]
    for t in threads: t.start()
    for t in threads: t.join()
    check('全部旧 attempt 回写被拒', results == [False] * 8, str(results))
    raw = broker.hgetall(f'compute:{token}')
    check('任务仍是 running、attempt=3', raw['status'] == 'running' and raw['attempt'] == '3', str(raw))
    check('没有写入任何结果', raw['result'] == '')

    print('\n[2] 当前 attempt 回写与并发接手竞争：恰好一个生效，绝不脏写')
    outcomes = []
    def writer():
        # 当前 attempt=3 的合法回写
        outcomes.append(('write', q.finish_job({'token': token, 'owner': 'op@x', 'attempt': 3},
                                               item, 'ready', {'plan_wh': 3.0})))
    user = {'sub': 'rv@x', 'role': 'reviewer'}
    def claimer():
        try:
            outcomes.append(('claim', q.claim(user, token)))
        except Exception as exc:
            outcomes.append(('claim-err', str(exc)))
    barrier = threading.Barrier(2)
    def run(fn):
        barrier.wait()
        fn()
    tw = threading.Thread(target=run, args=(writer,))
    tc = threading.Thread(target=run, args=(claimer,))
    tw.start(); tc.start(); tw.join(); tc.join()
    raw = broker.hgetall(f'compute:{token}')
    kinds = {k for k, _ in outcomes}
    check('两个动作都执行了', kinds == {'write', 'claim'}, str(outcomes))
    write_won = any(k == 'write' and v is True for k, v in outcomes)
    claim_won = any(k == 'claim' and isinstance(v, dict) for k, v in outcomes)
    check('回写与接手互斥（只有一方生效）', write_won ^ claim_won, str(outcomes))
    if write_won:
        check('回写获胜：任务 ready 且结果正确',
              raw['status'] == 'ready' and '"plan_wh": 3.0' in raw['result'], str(raw))
    else:
        check('接手获胜：任务回到 queued 且 attempt 递增',
              raw['status'] == 'queued' and int(raw['attempt']) == 4, str(raw))
        check('接手获胜时未残留旧结果', raw['result'] == '')

    print('\n[3] 高频连续接手 20 次：attempt 单调递增，队列始终只有一个条目')
    token2, _ = seed_running(1)
    for _ in range(20):
        q.claim(user, token2)
    raw = broker.hgetall(f'compute:{token2}')
    check('attempt 递增到 21', int(raw['attempt']) == 21, str(raw['attempt']))
    check('takeovers 视角为 20', q._public(raw)['takeovers'] == 20)
    entries = broker.lrange(q.QUEUE, 0, -1)
    check('队列仅一个条目且为最新 attempt',
          len(entries) == 1 and __import__('json').loads(entries[0])['attempt'] == 21,
          str(entries))
    check('processing 已清空', broker.llen(q.PROCESSING) == 0)

    print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
    sys.exit(1 if FAIL else 0)

if __name__ == '__main__':
    main()
