"""HTTP 层检查：权限、状态流转与页面所需的契约字段。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.config import database, settings
    import migrations.initial as migration
    from src.config.database import Base

    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    monkeypatch.setattr(settings, 'ARTIFACT_DIR', str(artifacts))

    # 替换全局引擎与会话工厂，不重载模块，避免影响其他测试。
    engine = create_engine(f'sqlite:///{tmp_path/"api.db"}')
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'Session', Session)
    monkeypatch.setattr(migration, 'engine', engine)
    monkeypatch.setenv('SEED_DEMO', 'false')

    import src.main as main
    Base.metadata.create_all(engine)

    from src.models import Dataset, User
    from src.services.business import seed_records
    from src.utils.security import hash_password
    from src.utils.serialization import checksum
    with Session() as db:
        db.add_all([
            User(email='admin@api.local', name='负责人', role='admin', password=hash_password('Demo123!')),
            User(email='analyst@api.local', name='分析员', role='analyst', password=hash_password('Demo123!')),
            User(email='viewer@api.local', name='观察员', role='viewer', password=hash_password('Demo123!')),
        ])
        rows = seed_records()
        db.add(Dataset(name='接口测试批次', source='api.csv', records=rows,
                       checksum=checksum(rows), created_at=0))
        db.commit()
    return TestClient(main.app)


def token(client, email):
    response = client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def test_review_flow_permissions_and_statuses(client):
    admin, analyst, viewer = (token(client, f'{role}@api.local') for role in ('admin', 'analyst', 'viewer'))
    dataset_id = client.get('/datasets', headers=admin).json()[0]['id']

    created = client.post('/review/plans', headers=admin, json={
        'name': '接口周期评审', 'destination': '业务复核组', 'weekdays': [0, 1, 2, 3, 4],
        'run_time': '08:00', 'start_date': '2026-11-02', 'holidays': [],
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()['id']

    # 分析员与观察员不能维护计划。
    assert client.post('/review/plans', headers=analyst, json={
        'name': 'x', 'weekdays': [0], 'start_date': '2026-11-02'}).status_code == 403
    assert client.post(f'/review/plans/{plan_id}/lines', headers=viewer, json={
        'code': 'P01', 'name': '冲压一线'}).status_code == 403

    line = client.post(f'/review/plans/{plan_id}/lines', headers=admin, json={
        'code': 'P01', 'name': '冲压一线', 'dataset_id': dataset_id,
        'params': {'max_features': 4, 'scenario_pct': 10},
    })
    assert line.status_code == 201, line.text
    missing = client.post(f'/review/plans/{plan_id}/lines', headers=admin, json={
        'code': 'P02', 'name': '冲压二线', 'dataset_id': None})
    assert missing.status_code == 201

    generated = client.post(f'/review/plans/{plan_id}/generate', headers=admin)
    assert generated.status_code == 200, generated.text
    assert generated.json()['created']

    periods = client.get('/review/periods', headers=viewer)
    assert periods.status_code == 200
    first = periods.json()[0]
    assert set(first) >= {'id', 'due_date', 'status', 'lines', 'briefing', 'snapshot'}
    # P01 已入队、P02 缺资料：整期未齐，先暴露待资料阻塞项而非待批准。
    assert first['status'] == 'waiting_data'
    states = {line['line_code']: line['status'] for line in first['lines']}
    assert states == {'P01': 'queued', 'P02': 'waiting_data'}

    summary = client.get('/review/periods/summary', headers=viewer).json()
    assert sum(summary['counts'].values()) >= 1

    # 整期不齐：不能批准。
    denied = client.post(f"/review/periods/{first['id']}/approve", headers=admin)
    assert denied.status_code == 409

    # 补齐资料（分析员可操作），等待 worker 计算后再批准。
    branch = next(line for line in first['lines'] if line['line_code'] == 'P02')
    filled = client.post(f"/review/branches/{branch['id']}/data", headers=analyst,
                         json={'dataset_id': dataset_id})
    assert filled.status_code == 200, filled.text
    assert client.post(f"/review/branches/{branch['id']}/retry", headers=viewer).status_code == 403
