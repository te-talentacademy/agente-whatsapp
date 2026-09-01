"""La memoria del teléfono: llamadas, permisos y minutos del día.

Tres reglas que este módulo hace cumplir a rajatabla:

1. IDEMPOTENCIA. Meta reenvía sus avisos: el primer aviso que logra el
   INSERT de la llamada es el único que actúa; los reenvíos encuentran la
   fila. Toda transición de estado es un UPDATE con condición — si otra
   parte ya la hizo, el rowcount lo dice y no se repite nada.
2. NADA SE ABANDONA EN SILENCIO. Una llamada que quedó a medias (reinicio,
   caída) se cierra con un motivo explícito, sea por el barrido de arranque
   o por el siguiente reenvío de Meta; ambos compiten por la MISMA
   transición y solo uno gana.
3. EL CUPO NUNCA SE SUB-CUENTA. Los segundos del día se apartan COMPLETOS
   antes de aceptar la llamada; al colgar se devuelve lo que no se usó. Si
   el proceso muere a mitad de llamada, lo apartado queda contado: el
   cinturón de gasto siempre falla del lado seguro.
"""

import logging
import time
from datetime import datetime, timezone

from src import config, db

logger = logging.getLogger("agente")

# Estados de una llamada. Terminales: ended / rejected / failed.
ACTIVE_STATES = ("claimed", "accepting", "active", "ending")
TERMINAL_STATES = ("ended", "rejected", "failed")

DAY_SECONDS = 24 * 3600.0
WEEK_SECONDS = 7 * DAY_SECONDS
REQUESTS_PER_DAY = 1   # límites de solicitudes de permiso de Meta,
REQUESTS_PER_WEEK = 2  # respetados aquí ANTES de tocar su API


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# La llamada: reclamo, transiciones y recuperación
# ---------------------------------------------------------------------------


def claim(call_id: str, wa_id: str, direction: str) -> bool:
    """Reclama la llamada. Solo el primer aviso la gana; el resto recibe False."""
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO calls (call_id, wa_id, direction, state, claimed_at)"
            " VALUES (?, ?, ?, 'claimed', ?)",
            (call_id, wa_id, direction, time.time()),
        )
        return cur.rowcount == 1


def get(call_id: str):
    with db.transaction() as conn:
        return conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()


def transition(call_id: str, to_state: str, from_states: tuple[str, ...], **fields) -> bool:
    """Cambia el estado SOLO si sigue en uno de los esperados (idempotente)."""
    sets = ["state = ?"]
    params: list = [to_state]
    for column, value in fields.items():
        sets.append(f"{column} = ?")
        params.append(value)
    marks = ", ".join("?" for _ in from_states)
    params.extend([call_id, *from_states])
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE calls SET {', '.join(sets)}"
            f" WHERE call_id = ? AND state IN ({marks})",
            params,
        )
        return cur.rowcount == 1


def recover(call_id: str, reason: str, require_lease: bool) -> bool:
    """Cierra una llamada huérfana con motivo explícito. Transición ÚNICA:
    el barrido de arranque y un aviso reenviado compiten por este mismo
    UPDATE y solo uno gana (el otro recibe False y no toca nada)."""
    params: list = [f"recuperada: {reason}", time.time(), call_id, *ACTIVE_STATES]
    lease_clause = ""
    if require_lease:
        lease_clause = " AND claimed_at < ?"
        params.append(time.time() - config.CALL_CLAIM_LEASE_SECONDS)
    marks = ", ".join("?" for _ in ACTIVE_STATES)
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE calls SET state = 'failed', last_error = ?, ended_at = ?"
            f" WHERE call_id = ? AND state IN ({marks})" + lease_clause,
            params,
        )
        return cur.rowcount == 1


def by_remote_id(remote_id: str):
    """La llamada saliente local a la que Meta asignó este identificador."""
    with db.transaction() as conn:
        return conn.execute(
            "SELECT * FROM calls WHERE remote_id = ?", (remote_id,)
        ).fetchone()


def set_remote_id(call_id: str, remote_id: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE calls SET remote_id = ? WHERE call_id = ?", (remote_id, call_id))


def orphans() -> list:
    """Las llamadas no terminales que sobrevivieron a un reinicio."""
    marks = ", ".join("?" for _ in ACTIVE_STATES)
    with db.transaction() as conn:
        rows = conn.execute(
            f"SELECT * FROM calls WHERE state IN ({marks})", ACTIVE_STATES
        ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# Minutos del día (cinturón de gasto de llamadas)
# ---------------------------------------------------------------------------


def daily_seconds_remaining() -> int | None:
    """Segundos de llamada que quedan hoy. None = sin límite (decisión expresa)."""
    limit_minutes = config.call_daily_minutes_limit()
    if limit_minutes is None:
        return None
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT call_seconds FROM usage WHERE day = ?", (_today(),)
        ).fetchone()
    used = row["call_seconds"] if row else 0
    return max(limit_minutes * 60 - used, 0)


def reserve_seconds(call_id: str, granted: int) -> bool:
    """Aparta los segundos concedidos ANTES de aceptar la llamada.

    La reserva queda confirmada en la memoria en la misma transacción que la
    anota en la llamada: cuando Meta reciba el accept, el gasto ya está
    contado aquí. Devuelve False si el cupo del día ya no alcanza.
    """
    limit_minutes = config.call_daily_minutes_limit()
    with db.transaction() as conn:
        if limit_minutes is not None:
            row = conn.execute(
                "SELECT call_seconds FROM usage WHERE day = ?", (_today(),)
            ).fetchone()
            used = row["call_seconds"] if row else 0
            if used >= limit_minutes * 60:
                return False
        conn.execute(
            "INSERT INTO usage (day, replies, voice, call_seconds) VALUES (?, 0, 0, ?)"
            " ON CONFLICT(day) DO UPDATE SET call_seconds = call_seconds + ?",
            (_today(), granted, granted),
        )
        conn.execute(
            "UPDATE calls SET reserved_seconds = ? WHERE call_id = ?",
            (granted, call_id),
        )
    logger.info(
        "LLAMADA %s: aparto %d segundos del cupo del día (reserva).", call_id, granted
    )
    return True


def reconcile_seconds(call_id: str, used_seconds: int) -> None:
    """Al colgar: devuelve al cupo lo apartado que no se usó.

    Solo se ejecuta cuando el final de la llamada es conocido. Si el proceso
    murió antes, lo apartado se queda contado (jamás se regala cupo).
    """
    used_seconds = max(int(used_seconds), 0)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT reserved_seconds FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        granted = row["reserved_seconds"] if row else 0
        if granted <= 0:
            return  # ya conciliada (o nunca reservada): idempotente
        refund = max(granted - used_seconds, 0)
        if refund:
            conn.execute(
                "UPDATE usage SET call_seconds = MAX(call_seconds - ?, 0) WHERE day = ?",
                (refund, _today()),
            )
        # La reserva queda en 0: una segunda conciliación no devuelve nada.
        conn.execute(
            "UPDATE calls SET seconds = ?, reserved_seconds = 0 WHERE call_id = ?",
            (used_seconds, call_id),
        )
    logger.info(
        "LLAMADA %s: consumo real %d s; devuelvo %d s al cupo del día (conciliación).",
        call_id, used_seconds, refund,
    )


def refund_full_reserve(call_id: str) -> None:
    """Devuelve la reserva completa: SOLO ante un rechazo inequívoco anterior
    a la conexión (Meta dijo que no antes de conectar). Un timeout ambiguo
    jamás pasa por aquí — la reserva se conserva."""
    reconcile_seconds(call_id, 0)


# ---------------------------------------------------------------------------
# Permisos de llamada saliente
# ---------------------------------------------------------------------------


def permission_valid(wa_id: str) -> bool:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM call_permissions WHERE wa_id = ?", (wa_id,)
        ).fetchone()
    if row is None or row["status"] != "approved":
        return False
    if row["is_permanent"]:
        return True
    expires = row["expires_at"]
    return expires is not None and expires > time.time()


def reserve_request(wa_id: str) -> tuple[bool, str]:
    """Aparta UNA solicitud de permiso respetando los límites locales.

    Atómico y ANTES de tocar Meta: si la red luego falla con timeout, la
    reserva se conserva (el proveedor pudo recibirla) — no se libera sola.
    Devuelve (ok, motivo).
    """
    now = time.time()
    with db.transaction() as conn:
        day_count = conn.execute(
            "SELECT COUNT(*) AS n FROM call_permission_requests"
            " WHERE wa_id = ? AND requested_at > ?",
            (wa_id, now - DAY_SECONDS),
        ).fetchone()["n"]
        if day_count >= REQUESTS_PER_DAY:
            return False, "ya le pedí permiso hoy; espera 24 horas"
        week_count = conn.execute(
            "SELECT COUNT(*) AS n FROM call_permission_requests"
            " WHERE wa_id = ? AND requested_at > ?",
            (wa_id, now - WEEK_SECONDS),
        ).fetchone()["n"]
        if week_count >= REQUESTS_PER_WEEK:
            return False, "ya le pedí permiso dos veces esta semana"
        cur = conn.execute(
            "INSERT INTO call_permission_requests (wa_id, requested_at) VALUES (?, ?)",
            (wa_id, now),
        )
        conn.execute(
            "INSERT INTO call_permissions (wa_id, status, is_permanent, expires_at,"
            " generation, updated_at) VALUES (?, 'pending_request', 0, NULL, ?, ?)"
            " ON CONFLICT(wa_id) DO UPDATE SET status = 'pending_request',"
            " is_permanent = 0, expires_at = NULL, generation = ?, updated_at = ?",
            (wa_id, now, now, now, now),
        )
        _last_request_id[wa_id] = cur.lastrowid
    return True, ""


_last_request_id: dict[str, int] = {}


def release_request(wa_id: str) -> None:
    """Devuelve la última reserva de solicitud: SOLO ante un rechazo inequívoco
    de Meta (4xx con respuesta) — la solicitud jamás llegó a la persona. Un
    timeout o saturación NO pasa por aquí (pudo llegar)."""
    row_id = _last_request_id.pop(wa_id, None)
    with db.transaction() as conn:
        if row_id is not None:
            conn.execute("DELETE FROM call_permission_requests WHERE id = ?", (row_id,))
        conn.execute(
            "UPDATE call_permissions SET status = 'request_failed', updated_at = ?"
            " WHERE wa_id = ? AND status = 'pending_request'",
            (time.time(), wa_id),
        )


def apply_permission_reply(wa_id: str, response: str, is_permanent: bool,
                           expires_at: float | None) -> str:
    """Aplica la respuesta de la persona a la solicitud VIGENTE.

    Devuelve 'approved' | 'denied' | 'stale'. Solo la transición desde
    pending_request cuenta (idempotente): un reenvío o una respuesta tardía
    de una solicitud vieja recibe 'stale' y no dispara nada.
    """
    approved = response == "accept"
    now = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE call_permissions SET status = ?, is_permanent = ?,"
            " expires_at = ?, updated_at = ?"
            " WHERE wa_id = ? AND status = 'pending_request'",
            (
                "approved" if approved else "denied",
                1 if is_permanent else 0,
                expires_at,
                now,
                wa_id,
            ),
        )
        if cur.rowcount != 1:
            return "stale"
    return "approved" if approved else "denied"
