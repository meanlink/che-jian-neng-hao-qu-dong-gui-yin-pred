"""周期评审：按业务日历与线体清单建期，逐线归因后汇总为整期简报。

职责边界：
- 建期只由 ``generate_periods`` 完成，``(plan_id, due_date)`` 唯一约束兜底并发。
- 分支计算与人工试算共用 ``Job`` 表，worker 走同一执行路径；分支 Job 以 ``origin='review'`` 标记，期次状态由分支汇总得出。
- 审批与分发只认整期齐全（快照内全部线体均 completed）；分发以 ``period_id`` 唯一约束兜底。
- 日历、线体、停用变更只作用于未建期；已建期按快照收尾。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from datetime import date, time as dtime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.config.settings import ARTIFACT_DIR
from src.constants.enums import ReviewPeriodStatus
from src.models import (
    Dataset, Job, ReviewBranch, ReviewDelivery, ReviewLine, ReviewPeriod, ReviewPlan, User,
)
from src.services.audit import record as audit
from src.services.jobs import normalize_params

WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
PERIOD_LABELS = {
    ReviewPeriodStatus.waiting_data.value: '待资料',
    ReviewPeriodStatus.running.value: '执行中',
    ReviewPeriodStatus.partial_failed.value: '部分失败',
    ReviewPeriodStatus.pending_approval.value: '待批准',
    ReviewPeriodStatus.approved.value: '已批准',
    ReviewPeriodStatus.distributed.value: '已分发',
}
STATUS_ORDER = [status.value for status in ReviewPeriodStatus]

# 分发占用超过该秒数视为执行器已退出，其他执行器可以接管（唯一约束仍兜底重复归档）。
DELIVERY_CLAIM_SECONDS = 600


def _now():
    return time.time()


def _parse_day(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(422, '日期须为 YYYY-MM-DD') from None


def _parse_run_time(value):
    try:
        hour, minute = str(value).split(':')
        parsed = dtime(int(hour), int(minute))
    except (TypeError, ValueError):
        raise HTTPException(422, '应跑时刻须为 HH:MM') from None
    if not (0 <= parsed.hour <= 23 and 0 <= parsed.minute <= 59):
        raise HTTPException(422, '应跑时刻须在 00:00 至 23:59 之间')
    return parsed


def _is_iso(value):
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _validate_calendar(weekdays, holidays):
    if not isinstance(weekdays, list) or not weekdays:
        raise HTTPException(422, '至少选择一个应跑日')
    if any(not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6 for day in weekdays):
        raise HTTPException(422, '应跑日须为 0（周一）至 6（周日）')
    if any(not isinstance(day, str) or not _is_iso(day) for day in (holidays or [])):
        raise HTTPException(422, '节假日须为 YYYY-MM-DD 日期列表')
    return sorted(set(weekdays)), sorted(set(holidays or []))


# ---------------------------------------------------------------- 计划与线体

def plan_dict(plan, lines=None):
    data = {
        'id': plan.id, 'name': plan.name, 'destination': plan.destination,
        'weekdays': plan.weekdays, 'run_time': plan.run_time,
        'start_date': str(plan.start_date), 'holidays': plan.holidays,
        'cursor_date': str(plan.cursor_date), 'enabled': plan.enabled,
        'disabled_at': plan.disabled_at, 'created_at': plan.created_at,
        'updated_at': plan.updated_at,
    }
    due = next_due(plan, _parse_day(plan.cursor_date)) if plan.enabled else None
    data['next_due'] = str(due) if due else None
    if lines is not None:
        data['lines'] = [line_dict(line) for line in lines]
    return data


def line_dict(line):
    return {
        'id': line.id, 'plan_id': line.plan_id, 'code': line.code, 'name': line.name,
        'dataset_id': line.dataset_id, 'params': line.params, 'enabled': line.enabled,
        'ready': bool(line.enabled and line.dataset_id),
    }


def create_plan(db, data, user):
    name = data.name.strip()
    if db.scalar(select(ReviewPlan).where(ReviewPlan.name == name)):
        raise HTTPException(409, '该评审计划名称已存在')
    weekdays, holidays = _validate_calendar(data.weekdays, data.holidays)
    start = _parse_day(data.start_date)
    _parse_run_time(data.run_time)
    plan = ReviewPlan(
        name=name, destination=data.destination, weekdays=weekdays, run_time=data.run_time,
        start_date=start, holidays=holidays, cursor_date=start - timedelta(days=1),
        enabled=True, created_by=user.id, created_at=_now(), updated_at=_now(),
    )
    db.add(plan)
    db.flush()
    return plan


def update_plan(db, plan, data):
    if data.destination is not None:
        plan.destination = data.destination
    if data.weekdays is not None or data.holidays is not None:
        plan.weekdays, plan.holidays = _validate_calendar(
            data.weekdays if data.weekdays is not None else plan.weekdays,
            data.holidays if data.holidays is not None else plan.holidays,
        )
    if data.run_time is not None:
        _parse_run_time(data.run_time)
        plan.run_time = data.run_time
    plan.updated_at = _now()
    db.flush()
    return plan


def set_plan_enabled(db, plan, enabled, actor, today=None):
    """停用只影响未建期：游标推进到当天，恢复后从下一个应跑日继续，停用空档不补建。"""
    if enabled == plan.enabled:
        return plan
    today = today or date.today()
    plan.enabled = enabled
    plan.updated_at = _now()
    if not enabled:
        plan.disabled_at = _now()
        if _parse_day(plan.cursor_date) < today:
            plan.cursor_date = today
    else:
        plan.disabled_at = None
        # 恢复当日也应建期；更早的停用空档不再补建。
        if _parse_day(plan.cursor_date) < today - timedelta(days=1):
            plan.cursor_date = today - timedelta(days=1)
    db.flush()
    audit(db, actor, 'review.plan_toggle', {'plan': plan.id, 'enabled': enabled})
    return plan


def upsert_line(db, plan, data, actor=None):
    code = (data.code or '').strip()
    name = (data.name or '').strip()
    if not code or not name:
        raise HTTPException(422, '线体编码与名称不能为空')
    if data.dataset_id is not None and not db.get(Dataset, data.dataset_id):
        raise HTTPException(404, '数据批次不存在')
    params = normalize_params(data.params or {})
    line = db.scalar(select(ReviewLine).where(ReviewLine.plan_id == plan.id, ReviewLine.code == code))
    now = _now()
    if line is None:
        line = ReviewLine(plan_id=plan.id, code=code, name=name, dataset_id=data.dataset_id,
                          params=params, enabled=data.enabled, created_at=now, updated_at=now)
        db.add(line)
    else:
        line.name = name
        line.dataset_id = data.dataset_id
        line.params = params
        line.enabled = data.enabled
        line.updated_at = now
    db.flush()
    if actor:
        audit(db, actor, 'review.line_saved', {'plan': plan.id, 'line': line.code, 'dataset': line.dataset_id})
    return line


# ---------------------------------------------------------------- 建期

def next_due(plan, after):
    """返回 after 之后的下一个应跑日（不含 after）。"""
    day = after + timedelta(days=1)
    holidays = set(plan.holidays or [])
    weekdays = set(plan.weekdays or [])
    for _ in range(3660):
        if day.weekday() in weekdays and str(day) not in holidays:
            return day
        day += timedelta(days=1)
    return None


def _existing_period(db, plan_id, due):
    return db.scalar(select(ReviewPeriod).where(ReviewPeriod.plan_id == plan_id, ReviewPeriod.due_date == due))


def generate_periods(db, plan, actor='system', today=None):
    """把游标与今天之间所有应跑日逐个写成期次；停机跨多日时按日历补建。

    期次以业务日为单位：应跑日当天任意时刻扫描即建期，``run_time`` 仅作为展示的计划时刻。
    """
    if not plan.enabled:
        return []
    today = today or date.today()
    cursor = _parse_day(plan.cursor_date)
    created = []
    while True:
        # 每轮重读停用状态与游标：停用与其他执行器推进都立即生效，已建期保留收尾。
        db.refresh(plan)
        if not plan.enabled:
            break
        cursor = max(cursor, _parse_day(plan.cursor_date))
        due = next_due(plan, cursor)
        if due is None or due > today:
            break
        if due < _parse_day(plan.start_date):
            cursor = due
            plan.cursor_date = due
            continue
        if _existing_period(db, plan.id, due):
            cursor = due
            plan.cursor_date = due
            continue
        period = _build_period(db, plan, due, actor)
        cursor = due
        plan.cursor_date = due
        if period is not None:
            created.append(period)
    plan.updated_at = _now()
    db.flush()
    return created


def _build_period(db, plan, due, actor):
    lines = list(db.scalars(select(ReviewLine).where(
        ReviewLine.plan_id == plan.id, ReviewLine.enabled == True).order_by(ReviewLine.id)))
    if not lines:
        return None
    snapshot = {
        'plan_name': plan.name, 'destination': plan.destination, 'due_date': str(due),
        'run_time': plan.run_time, 'weekday': WEEKDAY_LABELS[due.weekday()],
        'weekdays': list(plan.weekdays or []), 'holidays': list(plan.holidays or []),
        'lines': [],
    }
    for line in lines:
        item = {
            'code': line.code, 'name': line.name, 'dataset_id': line.dataset_id,
            'params': dict(line.params), 'dataset_name': '', 'dataset_checksum': '',
        }
        if line.dataset_id:
            dataset = db.get(Dataset, line.dataset_id)
            if dataset:
                item['dataset_name'] = dataset.name
                item['dataset_checksum'] = dataset.checksum
            else:
                item['dataset_id'] = None
        snapshot['lines'].append(item)
    period = ReviewPeriod(plan_id=plan.id, due_date=due, snapshot=snapshot, briefing={},
                          delivery_state='idle', delivery_error='', created_at=_now())
    # SAVEPOINT：并发下唯一约束冲突只回滚这一条期次，不牵连本执行器已建的其他期次。
    savepoint = db.begin_nested()
    try:
        db.add(period)
        savepoint.commit()
    except IntegrityError:
        # 另一执行器已建期；保留其快照，本流程只推进游标。
        savepoint.rollback()
        if period in db:
            db.expunge(period)
        return None
    for item in snapshot['lines']:
        db.add(ReviewBranch(
            period_id=period.id, line_code=item['code'], line_name=item['name'],
            dataset_id=item['dataset_id'], dataset_name=item['dataset_name'],
            dataset_checksum=item['dataset_checksum'], params=dict(item['params']),
            status='waiting_data', result={}, error='', attempts=0, created_at=_now(),
        ))
    db.flush()
    audit(db, actor, 'review.period_created', {
        'period': period.id, 'due_date': str(due), 'lines': len(snapshot['lines']),
    })
    return period


# ---------------------------------------------------------------- 资料与分支

def refresh_branch_data(db, branch):
    """资料补齐或批次被替换时，绑定当前批次内容（快照仍保留原批次标识）。"""
    if not branch.dataset_id:
        return False
    dataset = db.get(Dataset, branch.dataset_id)
    if not dataset:
        return False
    if dataset.checksum != (branch.dataset_checksum or ''):
        branch.dataset_name = dataset.name
        branch.dataset_checksum = dataset.checksum
        return True
    return False


def _job_name(period, branch):
    return f'周期评审 {period.due_date} · {branch.line_name}'


def _get_or_create_job(db, period, branch):
    if branch.job_id:
        job = db.get(Job, branch.job_id)
        if job:
            return job
    owner = db.scalar(select(User).where(User.role == 'admin').order_by(User.id))
    job = Job(name=_job_name(period, branch), dataset_id=branch.dataset_id,
              params=dict(branch.params or {}), status='queued', result={}, error='',
              approved=False, origin='review', created_by=owner.id if owner else None,
              created_at=time.time(), started_at=None, finished_at=None)
    db.add(job)
    db.flush()
    branch.job_id = job.id
    return job


def _queue_branch(db, period, branch, actor):
    job = _get_or_create_job(db, period, branch)
    job.status = 'queued'
    job.error = ''
    job.result = {}
    job.started_at = None
    job.finished_at = None
    job.approved = False
    branch.status = 'queued'
    branch.error = ''
    branch.result = {}
    branch.finished_at = None
    branch.attempts = (branch.attempts or 0) + 1
    if period.approved_at:
        period.approved_at = None
        period.approved_by = None
    if period.delivery_state not in ('idle', None):
        period.delivery_state = 'idle'
        period.delivery_error = ''
    db.flush()
    audit(db, actor, 'review.branch_queued', {
        'period': period.id, 'branch': branch.id, 'line': branch.line_code, 'job': job.id,
    })
    return job


def dispatch_ready_branches(db, plan, actor='system'):
    """待资料分支补齐后入队；已入队/执行中/失败的分支保持原状，失败只允许单独重试。"""
    jobs = []
    for period in db.scalars(select(ReviewPeriod).where(ReviewPeriod.plan_id == plan.id)):
        if period.delivery_state not in ('idle', None):
            continue
        for branch in branches_of(db, period):
            if branch.status != 'waiting_data':
                continue
            refresh_branch_data(db, branch)
            if not branch.dataset_id:
                continue
            jobs.append(_queue_branch(db, period, branch, actor))
    return jobs


def fill_branch_data(db, branch, dataset_id, actor):
    """为已建期次中缺少资料的线体补齐样本，随后自动入队。"""
    period = db.get(ReviewPeriod, branch.period_id)
    if period.distributed_at:
        raise HTTPException(409, '该期次已分发，资料补齐只影响未建期')
    if branch.status not in ('waiting_data', 'failed'):
        raise HTTPException(409, '该分支已在执行或已完成，无需补资料')
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')
    branch.dataset_id = dataset.id
    branch.dataset_name = dataset.name
    branch.dataset_checksum = dataset.checksum
    branch.error = ''
    db.flush()
    job = _queue_branch(db, period, branch, actor)
    audit(db, actor, 'review.branch_data_filled', {
        'period': period.id, 'branch': branch.id, 'dataset': dataset.id,
    })
    return job


def retry_branch(db, branch, actor):
    if branch.status not in ('failed', 'waiting_data'):
        raise HTTPException(409, '仅失败或待资料的分支可以重试')
    period = db.get(ReviewPeriod, branch.period_id)
    if period.distributed_at:
        raise HTTPException(409, '该期次已分发，新结果请建立新期次')
    refresh_branch_data(db, branch)
    if not branch.dataset_id:
        raise HTTPException(409, '该线体尚未绑定数据批次，请先补齐资料')
    return _queue_branch(db, period, branch, actor)


def run_branch_sync(db, branch, actor='system'):
    """单进程内同步执行一条已排队分支，供单元检查使用。

    生产路径由 worker 用原子占用领取 Job 后执行；这里只在 Job 仍为 queued 时计算，
    已完成/失败的 Job 只回写分支，避免重复计算。
    """
    from src.services.business import analyze

    period = db.get(ReviewPeriod, branch.period_id)
    job = _get_or_create_job(db, period, branch)
    if job.status in ('completed', 'failed'):
        sync_branches(db, period, actor)
        db.refresh(branch)
        return branch
    if job.status != 'queued':
        return branch
    job.status = 'running'
    job.started_at = _now()
    db.flush()
    try:
        dataset = db.get(Dataset, branch.dataset_id)
        if dataset is None:
            raise ValueError('该线体尚未绑定数据批次')
        result = analyze(dataset.records, job.params)
        json.dumps(result, allow_nan=False)
        job.result = result
        job.status = 'completed'
        job.error = ''
        job.finished_at = _now()
        branch.status = 'completed'
        branch.result = result
        branch.error = ''
        branch.finished_at = job.finished_at
    except Exception as exc:
        job.status = 'failed'
        job.error = str(exc)[:990]
        job.finished_at = _now()
        branch.status = 'failed'
        branch.error = job.error
        branch.finished_at = job.finished_at
    db.flush()
    if job.status == 'completed':
        sync_branches(db, period, actor)
    return branch


# ---------------------------------------------------------------- 期次汇总

def branches_of(db, period):
    return list(db.scalars(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id).order_by(ReviewBranch.id)))


def sync_branches(db, period, actor='system'):
    """把 Job 执行状态回写分支；worker 与读取路径都可安全重复调用。"""
    changed = False
    completed = False
    for branch in branches_of(db, period):
        if not branch.job_id:
            continue
        job = db.get(Job, branch.job_id)
        if not job:
            continue
        if job.status == 'completed' and branch.status != 'completed':
            branch.status = 'completed'
            branch.result = job.result
            branch.error = ''
            branch.finished_at = job.finished_at or _now()
            completed = True
            changed = True
        elif job.status == 'failed' and branch.status != 'failed':
            branch.status = 'failed'
            branch.error = job.error
            branch.finished_at = job.finished_at or _now()
            changed = True
        elif job.status == 'running' and branch.status == 'queued':
            branch.status = 'running'
            changed = True
        elif job.status == 'queued' and branch.status in ('running', 'queued'):
            branch.status = 'queued'
            changed = True
    if changed:
        db.flush()
    if completed:
        audit(db, actor, 'review.branch_completed', {'period': period.id})
    return changed


def period_state(period, branches):
    """期次状态按「需要人工动作的阻塞项」优先：
    待资料（缺样本，必须补数据）→ 执行中（其余在算）→ 部分失败（可单独重试）→ 待批准。
    """
    if period.distributed_at:
        return 'distributed'
    if period.approved_at:
        return 'approved'
    statuses = {branch.status for branch in branches}
    if not branches or 'waiting_data' in statuses:
        return 'waiting_data'
    if statuses == {'completed'}:
        return 'pending_approval'
    if statuses & {'queued', 'running'}:
        return 'running'
    if 'failed' in statuses:
        return 'partial_failed'
    return 'waiting_data'


def _compose_briefing(period, branches):
    lines = []
    for branch in branches:
        result = branch.result or {}
        lines.append({
            'line_code': branch.line_code, 'line_name': branch.line_name,
            'status': branch.status, 'dataset_id': branch.dataset_id,
            'dataset_name': branch.dataset_name, 'dataset_checksum': branch.dataset_checksum,
            'params': branch.params, 'error': branch.error, 'attempts': branch.attempts,
            'summary': result.get('summary', ''), 'metrics': result.get('metrics', []),
            'findings': result.get('findings', []), 'warnings': result.get('warnings', []),
            'chart': result.get('chart', {}), 'table': result.get('table', {}),
            'scenario_pct': (branch.params or {}).get('scenario_pct'),
        })
    done = [line for line in lines if line['status'] == 'completed']
    rmse_values = [m.get('value') for line in done for m in line['metrics']
                   if m.get('label') == '验证RMSE' and isinstance(m.get('value'), (int, float))]
    avg_rmse = round(sum(rmse_values) / len(rmse_values), 3) if rmse_values else None
    return {
        'plan_name': (period.snapshot or {}).get('plan_name', ''),
        'due_date': str(period.due_date), 'generated_at': _now(),
        'line_count': len(lines), 'completed_count': len(done),
        'lines': lines,
        'averages': [
            {'label': '线体数', 'value': len(lines)},
            {'label': '已完成', 'value': len(done)},
            {'label': '平均验证RMSE', 'value': avg_rmse, 'unit': 'kWh'},
        ],
        'summary': f'{period.due_date} 期次覆盖 {len(lines)} 条线体，{len(done)} 条已完成归因'
                   + (f'，平均验证RMSE {avg_rmse}kWh。' if avg_rmse is not None else '。'),
    }


def evaluate_period(db, period):
    """读取路径汇总：回写 Job 状态、重算简报与完成时间。"""
    sync_branches(db, period)
    branches = branches_of(db, period)
    state = period_state(period, branches)
    if state == 'pending_approval':
        if not period.completed_at:
            period.completed_at = _now()
        period.briefing = _compose_briefing(period, branches)
    elif state in ('waiting_data', 'running', 'partial_failed'):
        period.completed_at = None
        period.briefing = _compose_briefing(period, branches) if branches else {}
    db.flush()
    return state, branches


def branch_dict(branch):
    result = branch.result or {}
    return {
        'id': branch.id, 'period_id': branch.period_id, 'line_code': branch.line_code,
        'line_name': branch.line_name, 'dataset_id': branch.dataset_id,
        'dataset_name': branch.dataset_name, 'params': branch.params,
        'status': branch.status, 'job_id': branch.job_id, 'error': branch.error,
        'attempts': branch.attempts, 'finished_at': branch.finished_at,
        'summary': result.get('summary', ''), 'metrics': result.get('metrics', []),
        'findings': result.get('findings', []), 'warnings': result.get('warnings', []),
        'table': result.get('table', {}),
    }


def period_dict(db, period, branches=None):
    state, branches = evaluate_period(db, period)
    plan = db.get(ReviewPlan, period.plan_id)
    return {
        'id': period.id, 'plan_id': period.plan_id,
        'plan_name': plan.name if plan else (period.snapshot or {}).get('plan_name', ''),
        'due_date': str(period.due_date), 'status': state,
        'status_label': PERIOD_LABELS[state],
        'snapshot': period.snapshot, 'briefing': period.briefing or {},
        'completed_at': period.completed_at, 'approved_by': period.approved_by,
        'approved_at': period.approved_at, 'distributed_at': period.distributed_at,
        'delivery_state': period.delivery_state, 'delivery_error': period.delivery_error,
        'created_at': period.created_at,
        'lines': [branch_dict(branch) for branch in branches],
    }


# ---------------------------------------------------------------- 审批与分发

def approve_period(db, period, actor):
    state, branches = evaluate_period(db, period)
    if state == 'distributed':
        raise HTTPException(409, '该期次已分发')
    if state in ('waiting_data', 'running', 'partial_failed'):
        raise HTTPException(409, {
            'waiting_data': '仍有线体资料未补齐，整期齐全后才可批准',
            'running': '仍有线体在执行，请等待完成',
            'partial_failed': '存在计算失败的分支，请先单独重试',
        }[state])
    if period.approved_at:
        return period
    period.approved_at = _now()
    period.approved_by = actor
    period.completed_at = period.completed_at or _now()
    period.briefing = _compose_briefing(period, branches)
    db.flush()
    audit(db, actor, 'review.period_approved', {'period': period.id, 'due_date': str(period.due_date)})
    return period


def _claim_distribution(db, period_id, stale_seconds=DELIVERY_CLAIM_SECONDS):
    """并发执行器只有一个能把 delivery_state 从 idle 原子改成 sending。"""
    now = _now()
    claimed = db.execute(
        update(ReviewPeriod)
        .where(
            ReviewPeriod.id == period_id,
            ReviewPeriod.approved_at.is_not(None),
            ReviewPeriod.distributed_at.is_(None),
            (ReviewPeriod.delivery_state.is_(None))
            | (ReviewPeriod.delivery_state == 'idle')
            | ((ReviewPeriod.delivery_state == 'sending')
               & (ReviewPeriod.delivery_claimed_at < now - stale_seconds)),
        )
        .values(delivery_state='sending', delivery_claimed_at=now)
    ).rowcount
    db.commit()
    return claimed == 1


def _safe(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def write_delivery(db, period_id, actor='worker'):
    """生成整期简报 CSV/JSON；period_id 唯一约束保证重复分发只留一份归档。"""
    if not _claim_distribution(db, period_id):
        return None
    period = db.get(ReviewPeriod, period_id)
    branches = branches_of(db, period)
    # 已批准后仍要求全部线体分支完成，防止资料/分支状态在批准后被改动。
    if period_state(period, branches) != 'approved' or any(b.status != 'completed' for b in branches):
        period.delivery_state = 'idle'
        db.commit()
        return None
    briefing = _compose_briefing(period, branches)
    plan = db.get(ReviewPlan, period.plan_id)
    destination = ((period.snapshot or {}).get('destination')
                   or (plan.destination if plan else '业务复核组'))
    out = io.StringIO()
    out.write('﻿')
    writer = csv.writer(out)
    writer.writerow(['期次', '应跑日', '线体', '归因状态', '数据批次', '验证RMSE', '验证R²', '情景日电耗', '归因摘要'])
    for line in briefing['lines']:
        metrics = {m.get('label'): m.get('value') for m in line.get('metrics', [])}
        writer.writerow([
            _safe(briefing.get('plan_name', '')), _safe(briefing['due_date']),
            _safe(line['line_name']), _safe('已完成' if line['status'] == 'completed' else line['status']),
            _safe(line['dataset_name']), _safe(metrics.get('验证RMSE', '')),
            _safe(metrics.get('验证R²', '')), _safe(metrics.get('情景日电耗', '')),
            _safe(line['summary']),
        ])
    raw = out.getvalue().encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    delivery = ReviewDelivery(period_id=period.id, created_at=_now(), checksum=digest,
                              filename='', destination=destination)
    savepoint = db.begin_nested()
    try:
        db.add(delivery)
        savepoint.commit()
    except IntegrityError:
        # 另一执行器已写入归档；本执行器只确认状态。
        savepoint.rollback()
        if delivery in db:
            db.expunge(delivery)
        period = db.get(ReviewPeriod, period_id)
        period.delivery_state = 'sent'
        db.commit()
        return None
    delivery.filename = f'review-period-{period.id}-delivery-{delivery.id}.csv'
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR, delivery.filename), 'wb') as handle:
        handle.write(raw)
    with open(os.path.join(ARTIFACT_DIR, delivery.filename + '.json'), 'w') as handle:
        json.dump({'period': period.id, 'due_date': str(period.due_date), 'briefing': briefing,
                   'checksum': delivery.checksum}, handle, ensure_ascii=False, allow_nan=False)
    period.briefing = briefing
    period.distributed_at = _now()
    period.delivery_state = 'sent'
    period.delivery_error = ''
    audit(db, actor, 'review.period_distributed', {'period': period.id, 'delivery': delivery.id})
    db.commit()
    return delivery


def distribute_period(db, period, actor):
    """人工分发入口：整期齐全且已批准才可分发。"""
    state, _ = evaluate_period(db, period)
    if state == 'distributed':
        raise HTTPException(409, '该期次已分发')
    if not period.approved_at:
        raise HTTPException(409, '请先批准整期简报')
    delivery = write_delivery(db, period.id, actor)
    if delivery is None:
        raise HTTPException(409, '该期次正在被其他执行器分发或已分发')
    return delivery


def distribute_approved_periods(db, actor='worker'):
    """worker 汇总入口：只分发已批准且未分发的期次。"""
    ids = list(db.scalars(select(ReviewPeriod.id).where(
        ReviewPeriod.approved_at.is_not(None),
        ReviewPeriod.distributed_at.is_(None),
        (ReviewPeriod.delivery_state.is_(None)) | (ReviewPeriod.delivery_state != 'sent'),
    ).order_by(ReviewPeriod.id).limit(10)))
    db.commit()
    delivered = []
    for period_id in ids:
        try:
            delivery = write_delivery(db, period_id, actor)
            if delivery:
                delivered.append(delivery)
        except Exception:
            db.rollback()
            period = db.get(ReviewPeriod, period_id)
            if period:
                period.delivery_state = 'idle'
                period.delivery_error = '分发失败，将按周期重试'
                db.commit()
    return delivered


def period_deliveries(db, period_ids=None):
    query = select(ReviewDelivery).order_by(ReviewDelivery.id.desc())
    if period_ids is not None:
        query = query.where(ReviewDelivery.period_id.in_(period_ids))
    return list(db.scalars(query.limit(100)))
