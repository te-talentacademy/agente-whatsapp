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
    processed_at REAL,
    media_id TEXT
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
    reply TEXT,
    reply_upto INTEGER
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
    replies INTEGER NOT NULL DEFAULT 0,
    voice INTEGER NOT NULL DEFAULT 0
);

-- Páginas escaneadas de la libreta ya leídas por el motor de visión. Cada
-- lectura cuesta dinero: se guarda por página para no volver a pagarla en el
-- siguiente redespliegue (deploy-first rearma la libreta en cada cambio).
-- `engine` versiona la lógica y el modelo: si cambian, se relee sola.
CREATE TABLE IF NOT EXISTS ocr_cache (
    doc_sha256 TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    engine TEXT NOT NULL,
    text TEXT NOT NULL,
    extracted_at REAL NOT NULL,
    PRIMARY KEY (doc_sha256, page_no, engine)
);

-- El teléfono: una fila por llamada. El primer aviso de Meta que logra el
-- INSERT es el único que actúa (los reenvíos encuentran la fila y se apartan);
-- los estados no terminales que sobreviven a un reinicio se cierran con
-- motivo explícito al arrancar (jamás se abandonan en silencio).
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    wa_id TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'in',
    state TEXT NOT NULL DEFAULT 'claimed',
    claimed_at REAL NOT NULL,
    answered_at REAL,
    ended_at REAL,
    seconds INTEGER NOT NULL DEFAULT 0,
    reserved_seconds INTEGER NOT NULL DEFAULT 0,
    remote_id TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_remote ON calls (remote_id);

-- Permisos de llamada saliente por persona: el estado vigente y la marca de
-- la última solicitud (`generation`, solo contabilidad). El aviso de Meta no
-- identifica a qué solicitud responde, así que la vigencia del permiso se
-- verifica siempre justo antes de marcar (ver calls/manager.py).
CREATE TABLE IF NOT EXISTS call_permissions (
    wa_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    is_permanent INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    generation REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

-- Registro de solicitudes de permiso enviadas (para respetar los límites de
-- Meta LOCALMENTE, antes de tocar su API: 1 por día y 2 por semana por persona).
CREATE TABLE IF NOT EXISTS call_permission_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL,
    requested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_requests
    ON call_permission_requests (wa_id, requested_at);
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
    ("jobs", "reply_upto", "INTEGER"),
    ("messages", "media_id", "TEXT"),
    ("usage", "voice", "INTEGER NOT NULL DEFAULT 0"),
    ("usage", "call_seconds", "INTEGER NOT NULL DEFAULT 0"),
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
