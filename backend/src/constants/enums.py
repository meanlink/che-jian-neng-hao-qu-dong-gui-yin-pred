from enum import Enum


class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'


class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'


class ReviewBranchStatus(str, Enum):
    waiting_data = 'waiting_data'
    queued = 'queued'
    running = 'running'
    failed = 'failed'
    completed = 'completed'


class ReviewPeriodStatus(str, Enum):
    waiting_data = 'waiting_data'
    running = 'running'
    partial_failed = 'partial_failed'
    pending_approval = 'pending_approval'
    approved = 'approved'
    distributed = 'distributed'
