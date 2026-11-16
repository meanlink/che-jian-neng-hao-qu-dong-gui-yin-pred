from sqlalchemy import inspect, text

from src.config.database import Base, engine
from src import models


# 轻量迁移：只补新增列，不重建已有表，避免丢失业务数据。
COLUMNS = {
    'jobs': {'started_at': 'FLOAT NULL', 'origin': "VARCHAR(12) DEFAULT 'manual'"},
    'review_periods': {
        'delivery_state': "VARCHAR(12) DEFAULT 'idle'",
        'delivery_claimed_at': 'FLOAT NULL',
        'delivery_error': "VARCHAR(1000) DEFAULT ''",
    },
}


def migrate():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in COLUMNS.items():
            if table not in inspector.get_table_names():
                continue
            existing = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))


if __name__ == '__main__':
    migrate()
