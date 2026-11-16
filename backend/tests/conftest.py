import os
import tempfile

import pytest

# 测试必须使用独立的临时数据库与产物目录，不能触达演示数据卷。
_ARTIFACT_DIR = tempfile.mkdtemp(prefix='review-artifacts-')
os.environ['ARTIFACT_DIR'] = _ARTIFACT_DIR  # 强制隔离，避免写入真实产物卷
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/15')


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.config import database

    engine = create_engine(f'sqlite:///{tmp_path/"test.db"}')
    Session = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'Session', Session)

    from src.config.database import Base
    from src import models  # noqa: F401  注册全部模型
    Base.metadata.create_all(engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def session_factory(db):
    """独立会话工厂：模拟多个执行器各自持有一个数据库连接。"""
    from src.config import database
    return database.Session


@pytest.fixture()
def fixture_data(db):
    import time

    from src.models import Dataset, User
    from src.services.business import seed_records
    from src.utils.security import hash_password
    from src.utils.serialization import checksum

    admin = User(email='admin@test.local', name='负责人', role='admin', password=hash_password('x'))
    analyst = User(email='analyst@test.local', name='分析员', role='analyst', password=hash_password('x'))
    viewer = User(email='viewer@test.local', name='观察员', role='viewer', password=hash_password('x'))
    db.add_all([admin, analyst, viewer])
    db.flush()

    datasets = {}
    for code in ('P01', 'P02', 'P03'):
        records = seed_records()
        dataset = Dataset(name=f'澄川_{code}_160日能耗.csv', source=f'{code}.csv', records=records,
                          checksum=checksum(records), created_at=time.time())
        db.add(dataset)
        db.flush()
        datasets[code] = dataset
    db.commit()
    return {'admin': admin, 'analyst': analyst, 'viewer': viewer, 'datasets': datasets}
