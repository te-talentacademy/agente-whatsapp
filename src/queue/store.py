"""La fila de espera (cola) del agente.

Reglas de la casa:
- Cada mensaje entra UNA sola vez (aunque Meta lo reenvíe, se reconoce por su
  identificador wamid y no se duplica).
- Hay una sola ficha de trabajo por persona: si alguien manda tres mensajes
  seguidos, el agente espera un par de segundos y los atiende juntos.
- Si algo falla, se reintenta con pausas cada vez más largas; tras cinco
  intentos la ficha se marca como agotada (dead) y queda visible en la memoria
  para diagnosticarla, sin frenar al resto.

Toda operación corre dentro de db.transaction(): una transacción a la vez,
así el timbre y el trabajador de fondo jamás se pisan.
"""

import time
from dataclasses import dataclass

from src import db
from src.webhook.parse import InboundMessage

DEBOUNCE_SECONDS = 2.0   # espera corta tras cada mensaje nuevo
DEBOUNCE_CEILING = 8.0   # pero nunca más de esto desde el primer pendiente
LEASE_SECONDS = 60.0     # tiempo máximo con una ficha reclamada
MAX_ATTEMPTS = 5
BACKOFF_CEILING = 300.0


@dataclass
class Job:
    sender: str
    attempts: int
    message_ids: list[int]
    texts: list[str]
    kinds: list[str]
    reply: str | None = None  # respuesta ya pensada en un intento anterior
    reply_upto: int | None = None  # último mensaje (id) que esa respuesta cubre


def enqueue(msg: InboundMessage) -> bool:
    """Guarda un mensaje y actualiza la ficha de su remitente.

    Devuelve False si el mensaje ya se había visto (reenvío de Meta): en ese
    caso no se toca nada.
    """
    now = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages (wamid, sender, kind, body, received_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (msg.wamid, msg.sender, msg.kind, msg.body, now),
        )
        if cur.rowcount == 0:
            return False
        row = conn.execute(
            "SELECT status, first_pending_at, reply FROM jobs WHERE sender = ?",
            (msg.sender,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO jobs (sender, status, attempts, available_at,"
                " first_pending_at) VALUES (?, 'pending', 0, ?, ?)",
                (msg.sender, now + DEBOUNCE_SECONDS, now),
            )
        elif row["reply"] is not None:
            # Hay una respuesta ya pensada (y pagada) esperando su envío: se
            # conserva tal cual con su límite `reply_upto`; este mensaje nuevo
            # queda pendiente para el turno siguiente. Si la ficha estaba
            # agotada, el mensaje nuevo la revive sin tirar esa respuesta.
            if row["status"] == "dead":
                conn.execute(
                    "UPDATE jobs SET status='pending', attempts=0, available_at=?,"
                    " first_pending_at=?, last_error=NULL WHERE sender=?",
                    (now, now, msg.sender),
                )
        elif row["status"] in ("pending", "dead"):
            # Sin respuesta pensada, el mensaje nuevo se suma al turno: espera
            # corta con techo, y otra oportunidad incluso a una ficha agotada.
            first = row["first_pending_at"] if row["status"] == "pending" else now
            available = min(now + DEBOUNCE_SECONDS, first + DEBOUNCE_CEILING)
            conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, available_at=?,"
                " first_pending_at=?, last_error=NULL WHERE sender=?",
                (available, first, msg.sender),
            )
        # Si está 'claimed', el trabajador en curso contará los pendientes al
        # cerrar y volverá a poner la ficha en espera; con respuesta guardada,
        # solo cubre hasta `reply_upto` y este mensaje espera su propio turno.
        return True


def claim_ready_job() -> Job | None:
    """Reclama la siguiente ficha lista, si la hay.

    Antes de reclamar, rescata fichas cuyo trabajador se quedó a medias
    (reclamadas hace más del tiempo de gracia).
    """
    now = time.time()
    with db.transaction() as conn:
        # Rescate: una ficha reclamada hace demasiado vuelve a la fila.
        conn.execute(
            "UPDATE jobs SET status='pending', available_at=?, claimed_at=NULL"
            " WHERE status='claimed' AND claimed_at < ?",
            (now, now - LEASE_SECONDS),
        )
        row = conn.execute(
            "SELECT sender, attempts, reply, reply_upto FROM jobs"
            " WHERE status='pending' AND available_at <= ?"
            " ORDER BY available_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        sender = row["sender"]
        attempts = row["attempts"] + 1
        conn.execute(
            "UPDATE jobs SET status='claimed', claimed_at=?, attempts=?"
            " WHERE sender=?",
            (now, attempts, sender),
        )
        # Con una respuesta ya pensada, el turno es EXACTAMENTE el conjunto de
        # mensajes que la originó (hasta reply_upto). Lo que llegó después
        # espera su propio turno: jamás se cierra con una respuesta ajena.
        reply_upto = row["reply_upto"] if row["reply"] else None
        messages = conn.execute(
            "SELECT id, kind, body FROM messages"
            " WHERE sender=? AND processed_at IS NULL AND (? IS NULL OR id <= ?)"
            " ORDER BY received_at, id LIMIT 10",
            (sender, reply_upto, reply_upto),
        ).fetchall()
        return Job(
            sender=sender,
            attempts=attempts,
            message_ids=[m["id"] for m in messages],
            texts=[m["body"] for m in messages if m["body"]],
            kinds=[m["kind"] for m in messages],
            reply=row["reply"] if messages else None,
            reply_upto=reply_upto,
        )


def save_reply(job: Job, text: str) -> None:
    """Guarda la respuesta pensada ANTES de enviarla, atada a sus mensajes.

    Si el envío falla y toca reintentar, se reutiliza tal cual: un turno se
    piensa una sola vez (y se paga una sola vez). `reply_upto` fija hasta qué
    mensaje cubre esa respuesta.
    """
    job.reply = text
    job.reply_upto = max(job.message_ids) if job.message_ids else None
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET reply=?, reply_upto=? WHERE sender=?",
            (text, job.reply_upto, job.sender),
        )


def complete(job: Job) -> None:
    """Cierra la ficha: marca la tanda como atendida.

    Si mientras tanto llegaron mensajes nuevos, la ficha vuelve a la fila en
    lugar de cerrarse: nada se queda sin respuesta.
    """
    now = time.time()
    with db.transaction() as conn:
        if job.message_ids:
            marks = ",".join("?" * len(job.message_ids))
            conn.execute(
                f"UPDATE messages SET processed_at=? WHERE id IN ({marks})",
                (now, *job.message_ids),
            )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM messages"
            " WHERE sender=? AND processed_at IS NULL",
            (job.sender,),
        ).fetchone()["n"]
        if remaining > 0:
            # Turno nuevo: la respuesta guardada era del turno anterior.
            conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, available_at=?,"
                " first_pending_at=?, claimed_at=NULL, reply=NULL, reply_upto=NULL"
                " WHERE sender=?",
                (now + DEBOUNCE_SECONDS, now, job.sender),
            )
        else:
            conn.execute("DELETE FROM jobs WHERE sender=?", (job.sender,))


def fail(job: Job, error: str) -> None:
    """Registra un fallo y decide: reintentar con pausa o marcar agotada."""
    now = time.time()
    with db.transaction() as conn:
        if job.attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jobs SET status='dead', claimed_at=NULL, last_error=?"
                " WHERE sender=?",
                (error[:500], job.sender),
            )
            return
        delay = min(5.0 * (2 ** job.attempts), BACKOFF_CEILING)
        conn.execute(
            "UPDATE jobs SET status='pending', claimed_at=NULL, available_at=?,"
            " last_error=? WHERE sender=?",
            (now + delay, error[:500], job.sender),
        )
