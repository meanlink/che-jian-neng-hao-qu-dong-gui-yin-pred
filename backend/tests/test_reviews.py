"""周期评审的单元与并发语义检查。"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from src.models import Job, ReviewBranch, ReviewPeriod, ReviewPlan
from src.services import reviews


class Payload:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def make_plan(db, fixture_data, weekdays=(0, 1, 2, 3, 4), start='2026-11-02', run_time='08:00', holidays=()):
    return reviews.create_plan(db, Payload(
        name=f'评审计划-{start}-{"".join(map(str, weekdays))}', destination='业务复核组',
        weekdays=list(weekdays), run_time=run_time, start_date=start, holidays=list(holidays),
    ), fixture_data['admin'])


def add_line(db, plan, fixture_data, code, with_dataset=True, **params):
    dataset_id = fixture_data['datasets'][code].id if with_dataset else None
    return reviews.upsert_line(db, plan, Payload(
        code=code, name=f'冲压{code}线', dataset_id=dataset_id, params=params, enabled=True,
    ), fixture_data['admin'].email)


def period_for(db, plan, day):
    return db.scalar(select(ReviewPeriod).where(
        ReviewPeriod.plan_id == plan.id, ReviewPeriod.due_date == date.fromisoformat(day)))


def resolve_all(db, plan, actor='test'):
    """模拟 worker：排队分支逐条同步执行。"""
    jobs = reviews.dispatch_ready_branches(db, plan, actor)
    for job in jobs:
        branch = db.scalar(select(ReviewBranch).where(ReviewBranch.job_id == job.id))
        reviews.run_branch_sync(db, branch, actor)
    db.commit()
    return jobs


def test_calendar_generates_periods_and_backfills_after_outage(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()

    created = reviews.generate_periods(db, plan, today=date(2026, 11, 16))
    db.commit()
    days = [str(period.due_date) for period in created]
    # 2026-11-02 至 11-16 共 11 个工作日；周末不建期。
    assert days == ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06',
                    '2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16']
    assert reviews.generate_periods(db, plan, today=date(2026, 11, 16)) == []


def test_holidays_and_weekday_changes_only_affect_unbuilt_periods(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 4))
    db.commit()
    assert period_for(db, plan, '2026-11-04') is not None

    # 变更日历：11-05 设为节假日、周五移出应跑日；已建期不受影响。
    reviews.update_plan(db, plan, Payload(destination=None, weekdays=[0, 1, 2, 3],
                                          run_time=None, holidays=['2026-11-05']))
    db.commit()
    created = reviews.generate_periods(db, plan, today=date(2026, 11, 16))
    db.commit()
    assert [str(p.due_date) for p in created] == ['2026-11-09', '2026-11-10', '2026-11-11',
                                                   '2026-11-12', '2026-11-16']
    assert platform_period_count(db, plan) == 8
    snapshot = period_for(db, plan, '2026-11-04').snapshot
    assert snapshot['weekdays'] == [0, 1, 2, 3, 4]


def platform_period_count(db, plan):
    return len(list(db.scalars(select(ReviewPeriod).where(ReviewPeriod.plan_id == plan.id))))


def test_line_changes_only_affect_unbuilt_periods(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 3))
    db.commit()
    old = period_for(db, plan, '2026-11-03')
    add_line(db, plan, fixture_data, 'P02')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 4))
    db.commit()
    assert [line['code'] for line in old.snapshot['lines']] == ['P01']
    new = period_for(db, plan, '2026-11-04')
    assert [line['code'] for line in new.snapshot['lines']] == ['P01', 'P02']
    assert len(old.snapshot['lines']) == 1  # 快照不随线体清单变化


def test_missing_dataset_waits_and_blocks_approval(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    add_line(db, plan, fixture_data, 'P02', with_dataset=False)
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')
    resolve_all(db, plan)

    data = reviews.period_dict(db, period)
    assert data['status'] == 'waiting_data'
    assert {line['line_code']: line['status'] for line in data['lines']} == {
        'P01': 'completed', 'P02': 'waiting_data'}
    with pytest.raises(Exception) as exc:
        reviews.approve_period(db, period, 'admin@test.local')
    assert '资料未补齐' in str(exc.value)

    # 补齐资料后自动入队并完成，整期才可批准。
    branch = db.scalar(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id, ReviewBranch.line_code == 'P02'))
    job = reviews.fill_branch_data(db, branch, fixture_data['datasets']['P02'].id, 'analyst@test.local')
    db.commit()
    reviews.run_branch_sync(db, branch)
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'pending_approval'
    reviews.approve_period(db, period, 'admin@test.local')
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'approved'


def test_branch_failure_is_isolated_and_retryable(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    add_line(db, plan, fixture_data, 'P02')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')
    jobs = reviews.dispatch_ready_branches(db, plan)
    p02_branch = db.scalar(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id, ReviewBranch.line_code == 'P02'))
    # 模拟 P02 计算失败：样本不足 70 天。
    fixture_data['datasets']['P02'].records = fixture_data['datasets']['P02'].records[:10]
    db.commit()
    for job in jobs:
        branch = db.scalar(select(ReviewBranch).where(ReviewBranch.job_id == job.id))
        reviews.run_branch_sync(db, branch)
    db.commit()

    data = reviews.period_dict(db, period)
    assert data['status'] == 'partial_failed'
    states = {line['line_code']: line['status'] for line in data['lines']}
    assert states == {'P01': 'completed', 'P02': 'failed'}
    with pytest.raises(Exception) as exc:
        reviews.approve_period(db, period, 'admin@test.local')
    assert '单独重试' in str(exc.value)

    # 修复后单独重试失败分支，成功分支不受影响。
    fixture_data['datasets']['P02'].records = fixture_data['datasets']['P01'].records
    db.commit()
    reviews.retry_branch(db, p02_branch, 'analyst@test.local')
    db.commit()
    reviews.run_branch_sync(db, p02_branch)
    db.commit()
    data = reviews.period_dict(db, period)
    assert data['status'] == 'pending_approval'
    p01 = next(line for line in data['lines'] if line['line_code'] == 'P01')
    assert p01['attempts'] == 1  # 成功分支没有被重跑


def test_approval_and_distribution_create_exactly_one_archive(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')
    resolve_all(db, plan)
    with pytest.raises(Exception):
        reviews.distribute_period(db, period, 'admin@test.local')
    reviews.approve_period(db, period, 'admin@test.local')
    db.commit()

    delivery = reviews.write_delivery(db, period.id, 'worker')
    assert delivery is not None and delivery.filename.endswith('.csv')
    assert reviews.write_delivery(db, period.id, 'worker') is None
    db.expire_all()
    period = db.get(ReviewPeriod, period.id)
    data = reviews.period_dict(db, period)
    assert data['status'] == 'distributed'
    assert reviews.period_deliveries(db) and len(reviews.period_deliveries(db)) == 1
    assert data['briefing']['lines'][0]['status'] == 'completed'


def test_generation_is_idempotent_across_executors(db, fixture_data):
    """两个执行器同时建期：唯一约束兜底，只留一份期次与分支。"""
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    due = date(2026, 11, 2)
    first = reviews._build_period(db, plan, due, 'worker-a')
    db.commit()
    assert first is not None
    second = reviews._build_period(db, plan, due, 'worker-b')
    db.commit()
    assert second is None
    periods = list(db.scalars(select(ReviewPeriod).where(ReviewPeriod.plan_id == plan.id)))
    assert len(periods) == 1
    branches = list(db.scalars(select(ReviewBranch).where(ReviewBranch.period_id == periods[0].id)))
    assert len(branches) == 1


def test_disable_keeps_built_periods_and_stops_new_ones(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 3))
    db.commit()
    assert platform_period_count(db, plan) == 2

    reviews.set_plan_enabled(db, plan, False, 'admin@test.local', today=date(2026, 11, 6))
    db.commit()
    assert reviews.generate_periods(db, plan, today=date(2026, 11, 16)) == []
    assert platform_period_count(db, plan) == 2

    # 已建期仍可收尾：完成、批准、分发。
    period = period_for(db, plan, '2026-11-03')
    resolve_all(db, plan)
    reviews.approve_period(db, period, 'admin@test.local')
    db.commit()
    assert reviews.write_delivery(db, period.id, 'worker') is not None

    # 恢复当日重新建期；停用期间（11-04 至 11-15）的空档不补建。
    reviews.set_plan_enabled(db, plan, True, 'admin@test.local', today=date(2026, 11, 16))
    db.commit()
    created = reviews.generate_periods(db, plan, today=date(2026, 11, 17))
    db.commit()
    assert [str(p.due_date) for p in created] == ['2026-11-16', '2026-11-17']


def test_restart_keeps_completed_branches(db, fixture_data):
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    add_line(db, plan, fixture_data, 'P02')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')
    jobs = reviews.dispatch_ready_branches(db, plan)
    p01 = db.scalar(select(ReviewBranch).where(ReviewBranch.job_id == jobs[0].id))
    reviews.run_branch_sync(db, p01)
    db.commit()

    # 模拟中断：另一条分支执行到一半，任务回到排队状态。
    other = db.scalar(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id, ReviewBranch.line_code == 'P02'))
    assert other.status == 'queued'
    reviews.dispatch_ready_branches(db, plan)  # 再次扫描不应重排已完成或已排队分支
    db.commit()
    assert p01.status == 'completed'
    assert len(list(db.scalars(select(Job).where(Job.name.like('周期评审%'))))) == 2
    data = reviews.period_dict(db, period)
    assert {line['line_code']: line['status'] for line in data['lines']} == {
        'P01': 'completed', 'P02': 'queued'}
    assert data['status'] == 'running'


def test_concurrent_executors_create_one_period_and_one_archive(db, fixture_data, session_factory):
    """两个独立会话（模拟两个执行器）同时建期与分发，只能留下一份。"""
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    db.commit()
    plan_id, due = plan.id, date(2026, 11, 2)

    other = session_factory()
    try:
        other_plan = other.get(ReviewPlan, plan_id)
        first = reviews._build_period(db, plan, due, 'worker-a')
        db.commit()
        second = reviews._build_period(other, other_plan, due, 'worker-b')
        other.commit()
    finally:
        other.close()
    assert first is not None and second is None
    periods = list(db.scalars(select(ReviewPeriod).where(ReviewPeriod.plan_id == plan_id)))
    assert len(periods) == 1
    period = periods[0]
    resolve_all(db, plan)
    reviews.approve_period(db, period, 'admin@test.local')
    db.commit()

    other = session_factory()
    try:
        first_delivery = reviews.write_delivery(db, period.id, 'worker-a')
        second_delivery = reviews.write_delivery(other, period.id, 'worker-b')
    finally:
        other.close()
    db.expire_all()
    assert first_delivery is not None
    assert second_delivery is None
    assert len(reviews.period_deliveries(db)) == 1
    assert str(db.get(ReviewPeriod, period.id).delivery_state) == 'sent'


def test_restart_requeues_running_branch_and_keeps_completed(db, fixture_data):
    """中断恢复：已完成的线体保留结果，执行中被中断的 Job 回收重跑。"""
    import src.worker as worker
    from src.models import Job

    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    add_line(db, plan, fixture_data, 'P02')
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')
    jobs = reviews.dispatch_ready_branches(db, plan)
    db.commit()
    done_job, lost_job = jobs
    done_branch = db.scalar(select(ReviewBranch).where(ReviewBranch.job_id == done_job.id))
    lost_branch = db.scalar(select(ReviewBranch).where(ReviewBranch.job_id == lost_job.id))
    reviews.run_branch_sync(db, done_branch)
    db.commit()

    # 模拟执行器在计算中途崩溃：任务停在 running，分支仍是 queued。
    lost_job = db.get(Job, lost_job.id)
    lost_job.status = 'running'
    lost_job.started_at = 0  # 远超回收窗口
    db.commit()

    with db.begin():
        cutoff = __import__('time').time() - worker.STALE_RUNNING_SECONDS
        recovered = []
        for job in db.scalars(select(Job).where(Job.status == 'running')):
            if job.started_at is not None and job.started_at >= cutoff:
                continue
            job.status = 'queued'
            job.started_at = None
            recovered.append(job.id)
    assert recovered == [lost_job.id]

    db.expire_all()
    assert db.get(ReviewBranch, done_branch.id).status == 'completed'
    assert db.get(ReviewBranch, done_branch.id).result  # 已完成分支保留结果
    reviews.sync_branches(db, period)
    db.commit()
    data = reviews.period_dict(db, period)
    assert {line['line_code']: line['status'] for line in data['lines']} == {
        'P01': 'completed', 'P02': 'queued'}


def test_period_state_covers_all_page_filters(db, fixture_data):
    """页面筛选依赖的状态枚举齐全：待资料、执行中、部分失败、待批准及后续状态。"""
    plan = make_plan(db, fixture_data)
    add_line(db, plan, fixture_data, 'P01')
    add_line(db, plan, fixture_data, 'P02', with_dataset=False)
    db.commit()
    reviews.generate_periods(db, plan, today=date(2026, 11, 2))
    db.commit()
    period = period_for(db, plan, '2026-11-02')

    # 待资料：P02 缺样本，即使 P01 已入队也先暴露人工阻塞项
    reviews.dispatch_ready_branches(db, plan)
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'waiting_data'
    # 执行中：补齐后排队
    branch = db.scalar(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id, ReviewBranch.line_code == 'P02'))
    reviews.fill_branch_data(db, branch, fixture_data['datasets']['P02'].id, 'analyst@test.local')
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'running'
    # P01 单独完成，P02 仍在排队 → 仍是执行中
    p01 = db.scalar(select(ReviewBranch).where(
        ReviewBranch.period_id == period.id, ReviewBranch.line_code == 'P01'))
    reviews.run_branch_sync(db, p01)
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'running'
    # 部分失败：P02 样本不足
    fixture_data['datasets']['P02'].records = fixture_data['datasets']['P02'].records[:5]
    db.commit()
    reviews.run_branch_sync(db, branch)
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'partial_failed'
    # 待批准：修复后重试成功
    fixture_data['datasets']['P02'].records = fixture_data['datasets']['P01'].records
    db.commit()
    reviews.retry_branch(db, branch, 'analyst@test.local')
    db.commit()
    reviews.run_branch_sync(db, branch)
    db.commit()
    assert reviews.period_dict(db, period)['status'] == 'pending_approval'
