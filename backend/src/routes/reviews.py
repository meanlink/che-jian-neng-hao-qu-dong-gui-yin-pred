import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.middlewares.auth import admin_only, can_write, current_user
from src.models import ReviewBranch, ReviewDelivery, ReviewLine, ReviewPeriod, ReviewPlan
from src.schemas.review import BranchDataInput, LineInput, PlanInput, PlanToggle, PlanUpdate
from src.services import jobs as job_service, reviews
from src.services.audit import record
from src.utils.serialization import review_delivery_dict

router = APIRouter(prefix='/review')


def _plan(db, plan_id):
    plan = db.get(ReviewPlan, plan_id)
    if not plan:
        raise HTTPException(404, '评审计划不存在')
    return plan


def _period(db, period_id):
    period = db.get(ReviewPeriod, period_id)
    if not period:
        raise HTTPException(404, '评审期次不存在')
    return period


def _branch(db, branch_id):
    branch = db.get(ReviewBranch, branch_id)
    if not branch:
        raise HTTPException(404, '线体分支不存在')
    return branch


def _lines(db, plan_id):
    return list(db.scalars(select(ReviewLine).where(ReviewLine.plan_id == plan_id).order_by(ReviewLine.id)))


@router.get('/plans')
def plan_listing(db=Depends(get_db), u=Depends(current_user)):
    return [reviews.plan_dict(plan, _lines(db, plan.id))
            for plan in db.scalars(select(ReviewPlan).order_by(ReviewPlan.id.desc()))]


@router.post('/plans', status_code=201)
def plan_create(data: PlanInput, db=Depends(get_db), u=Depends(admin_only)):
    plan = reviews.create_plan(db, data, u)
    record(db, u.email, 'review.plan_create', {'plan': plan.id})
    db.commit()
    return reviews.plan_dict(plan, [])


@router.patch('/plans/{plan_id}')
def plan_update(plan_id: int, data: PlanUpdate, db=Depends(get_db), u=Depends(admin_only)):
    plan = _plan(db, plan_id)
    reviews.update_plan(db, plan, data)
    record(db, u.email, 'review.plan_update', {'plan': plan.id})
    db.commit()
    return reviews.plan_dict(plan, _lines(db, plan.id))


@router.post('/plans/{plan_id}/toggle')
def plan_toggle(plan_id: int, data: PlanToggle, db=Depends(get_db), u=Depends(admin_only)):
    plan = _plan(db, plan_id)
    reviews.set_plan_enabled(db, plan, data.enabled, u.email)
    db.commit()
    return reviews.plan_dict(plan, _lines(db, plan.id))


@router.post('/plans/{plan_id}/lines', status_code=201)
def line_save(plan_id: int, data: LineInput, db=Depends(get_db), u=Depends(admin_only)):
    plan = _plan(db, plan_id)
    line = reviews.upsert_line(db, plan, data, u.email)
    db.commit()
    return reviews.line_dict(line)


@router.delete('/plans/{plan_id}/lines/{line_id}')
def line_disable(plan_id: int, line_id: int, db=Depends(get_db), u=Depends(admin_only)):
    line = db.get(ReviewLine, line_id)
    if not line or line.plan_id != plan_id:
        raise HTTPException(404, '线体不存在')
    line.enabled = False
    record(db, u.email, 'review.line_disabled', {'plan': plan_id, 'line': line.code})
    db.commit()
    return {'ok': True, 'line': reviews.line_dict(line)}


@router.post('/plans/{plan_id}/generate')
def plan_generate(plan_id: int, db=Depends(get_db), u=Depends(admin_only)):
    """按业务日历立即补建到期期次，并让补齐资料的分支入队。"""
    plan = _plan(db, plan_id)
    created = reviews.generate_periods(db, plan, u.email)
    jobs = reviews.dispatch_ready_branches(db, plan, u.email)
    record(db, u.email, 'review.generate', {'plan': plan.id, 'periods': len(created), 'jobs': len(jobs)})
    db.commit()
    # 提交后再推送 Redis，避免执行器抢在事务提交前取到任务；SQL 排队状态仍是兜底。
    for job in jobs:
        job_service.enqueue(job.id)
    return {'created': [period.id for period in created], 'queued': [job.id for job in jobs]}


@router.get('/periods')
def period_listing(status: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    result = []
    for period in db.scalars(select(ReviewPeriod).order_by(ReviewPeriod.due_date.desc(), ReviewPeriod.id.desc()).limit(200)):
        data = reviews.period_dict(db, period)
        if status and data['status'] != status:
            continue
        result.append(data)
    db.commit()
    return result


@router.get('/periods/summary')
def period_summary(db=Depends(get_db), u=Depends(current_user)):
    counts = {key: 0 for key in reviews.PERIOD_LABELS}
    for period in db.scalars(select(ReviewPeriod)):
        state, _ = reviews.evaluate_period(db, period)
        counts[state] += 1
    db.commit()
    return {'counts': counts, 'labels': reviews.PERIOD_LABELS, 'order': reviews.STATUS_ORDER}


@router.get('/periods/{period_id}')
def period_detail(period_id: int, db=Depends(get_db), u=Depends(current_user)):
    data = reviews.period_dict(db, _period(db, period_id))
    db.commit()
    return data


@router.post('/periods/{period_id}/approve')
def period_approve(period_id: int, db=Depends(get_db), u=Depends(admin_only)):
    period = _period(db, period_id)
    reviews.approve_period(db, period, u.email)
    db.commit()
    return reviews.period_dict(db, period)


@router.post('/periods/{period_id}/distribute')
def period_distribute(period_id: int, db=Depends(get_db), u=Depends(admin_only)):
    period = _period(db, period_id)
    delivery = reviews.distribute_period(db, period, u.email)
    db.commit()
    return {'period': reviews.period_dict(db, period), 'delivery': review_delivery_dict(delivery)}


@router.post('/branches/{branch_id}/retry')
def branch_retry(branch_id: int, db=Depends(get_db), u=Depends(can_write)):
    branch = _branch(db, branch_id)
    job = reviews.retry_branch(db, branch, u.email)
    db.commit()
    job_service.enqueue(job.id)
    return {'branch': reviews.branch_dict(branch), 'job_id': job.id}


@router.post('/branches/{branch_id}/data')
def branch_fill_data(branch_id: int, data: BranchDataInput, db=Depends(get_db), u=Depends(can_write)):
    branch = _branch(db, branch_id)
    job = reviews.fill_branch_data(db, branch, data.dataset_id, u.email)
    db.commit()
    job_service.enqueue(job.id)
    return {'branch': reviews.branch_dict(branch), 'job_id': job.id}


@router.get('/deliveries')
def delivery_listing(db=Depends(get_db), u=Depends(current_user)):
    return [review_delivery_dict(d) for d in reviews.period_deliveries(db)]


@router.get('/deliveries/{delivery_id}/download')
def delivery_download(delivery_id: int, format: str = 'csv', db=Depends(get_db), u=Depends(current_user)):
    delivery = db.get(ReviewDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, '归档不存在')
    if format not in ('csv', 'json'):
        raise HTTPException(422, '支持 CSV 或 JSON')
    filename = delivery.filename + ('.json' if format == 'json' else '')
    path = os.path.join(ARTIFACT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, '归档文件不存在')
    return FileResponse(path, filename=filename,
                        media_type='application/json' if format == 'json' else 'text/csv; charset=utf-8')
