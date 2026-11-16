import time, json
from datetime import date, timedelta
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, ReviewPlan, ReviewLine, ReviewPeriod, ReviewBranch
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import reviews
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_review(db,m,d)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def _seed_review(db,m,dataset):
    """幂等建立演示用周期评审计划与线体，不建期次（期次由 worker 按日历产生）。"""
    if db.scalar(select(ReviewPlan).where(ReviewPlan.name=='周度能耗归因评审')):
        return
    admin=db.scalar(select(User).where(User.role=='admin'))
    plan=ReviewPlan(name='周度能耗归因评审',destination='业务复核组 · 本地收件箱',weekdays=[0,1,2,3,4],
                    run_time='08:00',start_date=date(2026,11,2),holidays=[],
                    cursor_date=date(2026,11,2)-timedelta(days=1),
                    enabled=True,disabled_at=None,created_by=admin.id,created_at=time.time(),updated_at=time.time())
    db.add(plan);db.flush()
    params={p['key']:p['default'] for p in m.get('parameters',[])}
    # 演示线体：两条已绑定样本，一条等待资料，便于展示“待资料 → 补齐 → 可批准”。
    for code,name,dataset_id in [('P01','冲压一线',dataset.id),('P02','冲压二线',dataset.id),('P03','冲压三线',None)]:
        db.add(ReviewLine(plan_id=plan.id,code=code,name=name,dataset_id=dataset_id,params=dict(params),
                          enabled=True,created_at=time.time(),updated_at=time.time()))
    db.flush()
    db.add(Audit(actor='seed',action='review.plan_create',detail={'plan':plan.id},at=time.time()))


if __name__=='__main__':seed()
