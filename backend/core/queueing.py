import hashlib
import json
from time import time
from uuid import uuid4

from fastapi import HTTPException
from redis import Redis

from core.config import REDIS_URL

broker = Redis.from_url(REDIS_URL, decode_responses=True)
QUEUE = 'compute_requests'
PROCESSING = f'{QUEUE}:processing'
RECORD_TTL = 86400
# 任务进入 running 后允许的独占秒数，超过即可被接手（原机器疑似断线/僵死）。
LEASE_SECONDS = 120


def _key(token):
    return f'compute:{token}'


def _idem_key(owner, digest):
    return f'compute_idem:{owner}:{digest}'


def _read(token):
    raw = broker.hgetall(_key(token))
    if not raw:
        raise HTTPException(404, detail='计算任务不存在或已过期')
    return raw


def _public(raw):
    data = {
        'token': raw['token'],
        'status': raw['status'],
        'attempt': int(raw['attempt']),
        'takeovers': max(int(raw['attempt']) - 1, 0),
        'submitter': raw.get('submitter', raw['owner']),
        'owner': raw['owner'],
        'input': json.loads(raw['input']),
    }
    if raw.get('result'):
        data['result'] = json.loads(raw['result'])
    if raw.get('detail'):
        data['detail'] = raw['detail']
    return data


def submit(user, payload):
    owner = user['sub']
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    idem = _idem_key(owner, digest)
    # 断线重试：同一提交人、同一份输入直接复用原计算号，不新增评估。
    existing = broker.get(idem)
    if existing and broker.exists(_key(existing)):
        return {**_public(_read(existing)), 'reused': True}

    token = uuid4().hex
    now = str(int(time()))
    item = json.dumps({'token': token, 'owner': owner, 'attempt': 1}, ensure_ascii=False)
    broker.hset(_key(token), mapping={
        'token': token, 'owner': owner, 'submitter': owner,
        'status': 'queued', 'attempt': 1,
        'input': body, 'result': '', 'detail': '', 'heartbeat': '0',
        'created_at': now, 'updated_at': now,
    })
    broker.expire(_key(token), RECORD_TTL)
    broker.rpush(QUEUE, item)
    # 幂等键与任务同生命周期；任务过期后同输入才会开新计算。
    broker.set(idem, token, ex=RECORD_TTL)
    return {'token': token, 'status': 'queued', 'attempt': 1, 'takeovers': 0, 'reused': False}


def view(user, token):
    raw = _read(token)
    # 提交人始终可查看自己的任务；接手只换执行者，不改归属。
    if raw['submitter'] != user['sub'] and user['role'] != 'reviewer':
        raise HTTPException(403, detail='仅提交人和复核员可查看计算')
    return _public(raw)


def result(user, token):
    data = view(user, token)
    if data['status'] != 'ready':
        raise HTTPException(409, detail='等待计算完成后再提交')
    return data


def claim(user, token):
    key = _key(token)
    raw = _read(token)
    if raw['submitter'] != user['sub'] and user['role'] != 'reviewer':
        raise HTTPException(403, detail='仅提交人和复核员可接手计算')
    if raw['status'] == 'running' and int(time()) - int(raw['heartbeat'] or '0') < LEASE_SECONDS:
        raise HTTPException(409, detail='原机器仍在租期内，暂不能接手')

    # WATCH 做时代栅栏：接手期间若任务被别的执行者改动则放弃本次提交。
    for _ in range(5):
        with broker.pipeline(transaction=True) as pipe:
            pipe.watch(key)
            state = pipe.hget(key, 'status')
            if state is None:
                pipe.unwatch()
                raise HTTPException(404, detail='计算任务不存在或已过期')
            if state not in ('queued', 'running'):
                pipe.unwatch()
                raise HTTPException(409, detail='任务已结束，无需接手')
            attempt = int(pipe.hget(key, 'attempt')) + 1
            # owner 记录本一轮执行者；submitter 始终是原提交人，权限归属不变。
            item = json.dumps({'token': token, 'owner': user['sub'], 'attempt': attempt},
                              ensure_ascii=False)
            # 清掉该任务此前残留在两个列表中的旧 attempt 条目，保证只占一个队列位。
            stale = [raw for raw in broker.lrange(QUEUE, 0, -1) + broker.lrange(PROCESSING, 0, -1)
                     if json.loads(raw).get('token') == token]
            pipe.multi()
            pipe.hset(key, mapping={'status': 'queued', 'attempt': attempt,
                                    'owner': user['sub'], 'heartbeat': '0',
                                    'updated_at': str(int(time()))})
            pipe.expire(key, RECORD_TTL)
            for old in stale:
                pipe.lrem(QUEUE, 1, old)
                pipe.lrem(PROCESSING, 1, old)
            pipe.rpush(QUEUE, item)
            if pipe.execute() is not None:
                return {**_public(_read(token)), 'reused': True}
    raise HTTPException(409, detail='任务状态竞争激烈，请稍后重试')


def pull_job():
    """阻塞取下一个任务并原子置为 running，返回 (job, 队列原始条目)。

    接手会在队列中留下旧 attempt 条目；取到过期条目时直接剔除并继续等，
    不做无效计算，也不会让旧 attempt 触碰任务状态。
    """
    while True:
        item = broker.blmove(QUEUE, PROCESSING, timeout=2, src='LEFT', dest='RIGHT')
        if item is None:
            return None, None
        job = json.loads(item)
        current = broker.hget(_key(job['token']), 'attempt')
        if current is None or int(current) != job['attempt']:
            broker.lrem(PROCESSING, 1, item)
            continue
        # 输入快照冻结在任务哈希里，队列条目只保留栅栏所需的 token/attempt。
        job['input'] = json.loads(broker.hget(_key(job['token']), 'input'))
        broker.hset(_key(job['token']), mapping={
            'status': 'running', 'heartbeat': str(int(time())), 'updated_at': str(int(time())),
        })
        broker.expire(_key(job['token']), RECORD_TTL)
        return job, item


def finish_job(job, item, status, payload):
    """按 attempt 栅栏回写；被接手的旧机器回写会失败并静默丢弃。

    WATCH 保证：若任务已被接手（attempt 已递增、状态已变动），
    则本次事务不会执行，旧机器结果无法覆盖新任务。
    """
    key = _key(job['token'])
    for _ in range(5):
        with broker.pipeline(transaction=True) as pipe:
            pipe.watch(key)
            current_state = pipe.hget(key, 'status')
            current_attempt = pipe.hget(key, 'attempt')
            if current_state != 'running' or int(current_attempt) != job['attempt']:
                pipe.unwatch()
                return False
            pipe.multi()
            if status == 'ready':
                pipe.hset(key, mapping={'status': 'ready',
                                        'result': json.dumps(payload, ensure_ascii=False),
                                        'detail': '', 'updated_at': str(int(time()))})
            else:
                pipe.hset(key, mapping={'status': 'rejected', 'result': '',
                                        'detail': payload.get('detail', ''),
                                        'updated_at': str(int(time()))})
            pipe.expire(key, RECORD_TTL)
            pipe.lrem(PROCESSING, 1, item)
            if pipe.execute() is not None:
                return True
    return False


def requeue_processing():
    """worker 启动时把遗留 processing 条目按新 attempt 重新入队（崩溃续跑）。

    只有与任务当前 attempt 一致的条目才是崩溃执行者留下的，需要递增续跑；
    其余都是被接手后残留的过期条目，直接剔除，避免凭空多开一轮。
    """
    for item in broker.lrange(PROCESSING, 0, -1):
        job = json.loads(item)
        key = _key(job['token'])
        raw = broker.hgetall(key)
        if not raw or raw['status'] not in ('running', 'queued') \
                or int(raw['attempt']) != job['attempt']:
            broker.lrem(PROCESSING, 1, item)
            continue
        attempt = int(raw['attempt']) + 1
        fresh = json.dumps({'token': job['token'], 'owner': raw['owner'], 'attempt': attempt},
                           ensure_ascii=False)
        broker.hset(key, mapping={'status': 'queued', 'attempt': attempt, 'heartbeat': '0',
                                  'updated_at': str(int(time()))})
        broker.expire(key, RECORD_TTL)
        broker.rpush(QUEUE, fresh)
        broker.lrem(PROCESSING, 1, item)
