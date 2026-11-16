from sqlalchemy import (
    JSON, Boolean, Date, Double, ForeignKey, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class ReviewPlan(Base):
    """周期评审计划：业务日历 + 线体清单的版本化入口。"""
    __tablename__ = 'review_plans'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    destination: Mapped[str] = mapped_column(String(120), default='业务复核组 · 本地收件箱')
    weekdays: Mapped[list] = mapped_column(JSON, default=list)
    run_time: Mapped[str] = mapped_column(String(5), default='08:00')
    start_date: Mapped[object] = mapped_column(Date)
    holidays: Mapped[list] = mapped_column(JSON, default=list)
    cursor_date: Mapped[object] = mapped_column(Date)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class ReviewLine(Base):
    """线体清单：样本批次与分析参数的当前配置，只进入其后新建的期次快照。"""
    __tablename__ = 'review_lines'
    __table_args__ = (UniqueConstraint('plan_id', 'code', name='uq_review_line_plan_code'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('review_plans.id'))
    code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class ReviewPeriod(Base):
    """一个应跑日形成一个期次；日历和线体在创建时冻结为快照。"""
    __tablename__ = 'review_periods'
    __table_args__ = (UniqueConstraint('plan_id', 'due_date', name='uq_review_period_plan_due'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('review_plans.id'))
    due_date: Mapped[object] = mapped_column(Date)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    briefing: Mapped[dict] = mapped_column(JSON, default=dict)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    approved_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    distributed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    delivery_state: Mapped[str] = mapped_column(String(12), default='idle')
    delivery_claimed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    delivery_error: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[float] = mapped_column(Double)


class ReviewBranch(Base):
    """期次内的一条线体分支；每条分支独立计算、独立重试。"""
    __tablename__ = 'review_branches'
    __table_args__ = (UniqueConstraint('period_id', 'line_code', name='uq_review_branch_period_line'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('review_periods.id'))
    line_code: Mapped[str] = mapped_column(String(80))
    line_name: Mapped[str] = mapped_column(String(120))
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'), nullable=True)
    dataset_name: Mapped[str] = mapped_column(String(160), default='')
    dataset_checksum: Mapped[str] = mapped_column(String(64), default='')
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default='waiting_data')
    job_id: Mapped[int | None] = mapped_column(ForeignKey('jobs.id'), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class ReviewDelivery(Base):
    """整期简报归档；period_id 唯一保证并发执行器不会重复分发。"""
    __tablename__ = 'review_deliveries'
    __table_args__ = (UniqueConstraint('period_id', name='uq_review_delivery_period'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('review_periods.id'))
    created_at: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(120))
