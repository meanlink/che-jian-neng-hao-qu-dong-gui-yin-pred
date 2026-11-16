import time, json, logging
from pathlib import Path
from sqlalchemy import select, update
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, ReviewPlan, ReviewBranch, ReviewPeriod
from src.services.business import analyze
from src.services.reports import deliver
from src.services import reviews
from src.services.audit import record
logging.basicConfig(level=logging.INFO)


STALE_RUNNING_SECONDS=900


def claim(job_id):
    """原子占用排队任务：多个执行器同时拿到同一 ID 时只有一个能执行。"""
    with Session() as db:
        claimed=db.execute(update(Job).where(Job.id==job_id,Job.status=='queued').values(status='running',started_at=time.time())).rowcount
        db.commit()
        return claimed==1


def execute(job_id):
    if not claim(job_id):return
    with Session() as db:
        j=db.get(Job,job_id)
        if not j:return
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
        _sync_review_branch(job_id)


def _sync_review_branch(job_id):
    """分支任务完成后回写期次；整期齐全后由 worker 按审批状态分发。"""
    with Session() as db:
        branch=db.scalar(select(ReviewBranch).where(ReviewBranch.job_id==job_id))
        if not branch:return
        period=db.get(ReviewPeriod,branch.period_id)
        reviews.sync_branches(db,period)
        reviews.evaluate_period(db,period)
        db.commit()


def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
    review_tick()


def review_tick():
    """周期评审生产者：建期、补齐资料分支入队、已批准期次分发。

    多个执行器可同时运行：建期与分发都靠唯一约束/原子占用兜底，重复执行不会重复建期或分发。
    """
    with Session() as db:
        plan_ids=list(db.scalars(select(ReviewPlan.id).where(ReviewPlan.enabled==True)))
    queued=[]
    for plan_id in plan_ids:
        with Session() as db:
            plan=db.get(ReviewPlan,plan_id)
            if not plan or not plan.enabled:continue
            try:
                reviews.generate_periods(db,plan,'worker')
                jobs=reviews.dispatch_ready_branches(db,plan,'worker')
                queued+=[job.id for job in jobs]
                db.commit()
            except Exception:
                logging.exception('Review generation retry');db.rollback()
    with Session() as db:
        # Redis 推送可能失败或执行器中途退出；SQL 排队状态兜底扫描（含已停用计划的已建期收尾）。
        queued+=list(db.scalars(select(Job.id).where(Job.status=='queued',Job.origin=='review').limit(50)))
    for job_id in dict.fromkeys(queued):
        execute(job_id)  # 分支与人工试算共用同一执行路径；同步计算后回写期次
    with Session() as db:
        try:
            reviews.distribute_approved_periods(db,'worker')
        except Exception:
            logging.exception('Review distribution retry');db.rollback()


def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # 多个执行器可能并存：只回收长时间未更新的 running 任务，避免抢走其他执行器的活。
    with Session() as db:
        cutoff=time.time()-STALE_RUNNING_SECONDS
        for j in db.scalars(select(Job).where(Job.status=='running')):
            if j.started_at is not None and j.started_at>=cutoff:
                continue
            j.status='queued';j.started_at=None
            record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
