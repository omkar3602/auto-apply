"""Adds columns that newer code expects to a database created by older code.

`db.create_all()` creates missing *tables* but never alters an existing one, and
this app has no migration tool, so new Job columns are added here. Safe to run on
every start: it only adds what's missing.
"""

from sqlalchemy import inspect, text

# table -> {column: SQL type}
NEW_COLUMNS = {
    "jobs": {
        "posted_at": "DATETIME",
        "years_min": "INTEGER",
        "sponsorship": "VARCHAR(20)",
        "match_score": "INTEGER",
        "tsenta_job_id": "VARCHAR(64)",
        "tsenta_application_id": "VARCHAR(64)",
    },
    "auto_apply_decisions": {"posted_at": "DATETIME", "age_minutes": "INTEGER"},
    "auto_apply_runs": {"median_age_min": "INTEGER"},
}
NEW_INDEXES = {
    "ix_jobs_tsenta_job_id": ("jobs", "tsenta_job_id"),
    "ix_jobs_tsenta_application_id": ("jobs", "tsenta_application_id"),
}


def upgrade(engine):
    """Returns the list of columns it added (empty when already up to date)."""
    added = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            have = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in have:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}'))
                    added.append(f"{table}.{name}")
        for index, (table, column) in NEW_INDEXES.items():
            if inspector.has_table(table):
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" ("{column}")'))
    return added
