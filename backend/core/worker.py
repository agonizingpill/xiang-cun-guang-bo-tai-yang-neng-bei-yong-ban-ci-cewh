from pathlib import Path
from traceback import print_exc

from core.queueing import broker, finish_job, pull_job, requeue_processing
from domain.learning import calculate

pulse = Path('/tmp/compute-heartbeat')


def serve():
    while True:
        broker.ping()
        pulse.touch()
        job, item = pull_job()
        if job is None:
            continue
        try:
            result = calculate(job['input'])
            accepted = finish_job(job, item, 'ready', result)
        except ValueError as exc:
            accepted = finish_job(job, item, 'rejected', {'detail': str(exc)})
        except Exception:
            print_exc()
            accepted = finish_job(job, item, 'rejected',
                                  {'detail': '计算失败，请检查输入和 worker 日志'})
        if not accepted:
            # 任务已被接手：原机器的结果按规则丢弃，仅留 worker 侧痕迹。
            print(f"stale result discarded for taken-over compute {job['token']} "
                  f"attempt {job['attempt']}")
        pulse.touch()


if __name__ == '__main__':
    requeue_processing()
    serve()
