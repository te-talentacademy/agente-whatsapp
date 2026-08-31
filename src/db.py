"""Memoria del servicio: un archivo SQLite.

Dos tablas:
- messages: cada mensaje entrante, una sola vez (aunque Meta lo reenvíe).
- jobs: la fila de trabajo por cada persona que escribió y espera respuesta.
"""

import sqlite3
import threading
from contextlib import contextmanager

from src import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wamid TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL,
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_pending
    ON messages (sender) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS jobs (
    sender TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    first_pending_at REAL NOT NULL,
    claimed_at REAL,
    last_error TEXT,
    reply TEXT
);

-- Historial de conversación por persona (lo que dijo cada quien), para que
-- el cerebro recuerde el hilo. Se llena solo cuando el cerebro está encendido.
CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_sender ON conversation (sender, id);

-- Contador diario de respuestas con cerebro: el cinturón de seguridad de gasto.
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    replies INTEGER NOT NULL DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    """Abre (una sola vez) la conexión al archivo de memoria."""
    global _conn
    with _lock:
        if _conn is None:
            conn = sqlite3.connect(
                config.db_path(), check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            _migrate(conn)
            _conn = conn
        return _conn


# Columnas añadidas después de la primera versión. Una memoria creada con la
# versión anterior (en tu volumen) se completa sola al arrancar.
_ADDED_COLUMNS = [
    ("jobs", "reply", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


@contextmanager
def transaction():
    """Una transacción a la vez, venga del timbre o del trabajador de fondo.

    El timbre y el trabajador comparten la misma conexión desde hilos
    distintos; este candado garantiza que sus transacciones jamás se pisan.
    """
    conn = connect()
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def healthy() -> bool:
    """Comprobación de salud de solo lectura: no toca ni reclama trabajos."""
    try:
        with _lock:
            connect().execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False
