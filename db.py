import sqlite3

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Put every SQLite connection in WAL mode.

    In the default "delete" journal mode a writer blocks readers outright, so
    a sync - which holds its write transaction open while it drives a browser -
    makes every page load and every other writer fail with "database is
    locked". WAL lets readers carry on against the last committed snapshot.

    Writers still serialize against each other; the busy timeout in
    Config.SQLALCHEMY_ENGINE_OPTIONS is what waits those out.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()
