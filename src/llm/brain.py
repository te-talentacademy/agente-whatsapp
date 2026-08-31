"""El turno completo del cerebro.

Dado lo que escribió una persona, arma el contexto (personalidad + reglas +
libreta + historial), consulta al modelo, guarda el intercambio y devuelve el
texto. Con un cinturón de seguridad: tope diario de respuestas.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from src import config, db
from src.llm import client, prompt

logger = logging.getLogger("agente")

HISTORY_TURNS = 10
MAX_REPLY_CHARS = 4096   # límite de WhatsApp por mensaje
LIMIT_REACHED_TEXT = (
    "Hoy ya atendí el máximo de mensajes que tengo permitido. "
    "Mañana sigo con gusto; si es urgente, una persona del equipo te contacta."
)


@dataclass
class Thought:
    text: str | None = None
    retryable: bool = False
    reason: str = ""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _limit_reached() -> bool:
    limit = config.daily_message_limit()
    if limit is None:
        return False
    with db.transaction() as conn:
        row = conn.execute("SELECT replies FROM usage WHERE day = ?", (_today(),)).fetchone()
    return bool(row and row["replies"] >= limit)


def _count_reply() -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage (day, replies) VALUES (?, 1)"
            " ON CONFLICT(day) DO UPDATE SET replies = replies + 1",
            (_today(),),
        )


def _history(sender: str) -> list[dict]:
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation WHERE sender = ?"
            " ORDER BY id DESC LIMIT ?",
            (sender, HISTORY_TURNS),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _remember(sender: str, role: str, content: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO conversation (sender, role, content, created_at) VALUES (?, ?, ?, ?)",
            (sender, role, content, time.time()),
        )


def _single_message(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_REPLY_CHARS:
        return text
    cut = text[: MAX_REPLY_CHARS - 20].rstrip()
    return cut + "\n(… sigo si me preguntas)"


def think(sender: str, user_text: str) -> Thought:
    """Piensa la respuesta para `user_text`. Nunca lanza errores hacia arriba."""
    api_key = config.openrouter_api_key()
    if not api_key:
        return Thought(reason="falta OPENROUTER_API_KEY")

    if _limit_reached():
        logger.warning("Tope diario de respuestas alcanzado (DAILY_MESSAGE_LIMIT); respondo el aviso fijo.")
        return Thought(text=LIMIT_REACHED_TEXT)

    notes = []
    if config.rag_enabled():
        from src.rag import index  # carga perezosa: solo si la libreta está encendida

        notes = index.search(user_text)

    messages = [{"role": "system", "content": prompt.build_system_prompt(notes)}]
    messages.extend(_history(sender))
    messages.append({"role": "user", "content": user_text})

    result = client.complete(messages, config.openrouter_model(), api_key, title=config.agent_name())
    if not result.ok:
        return Thought(retryable=result.retryable, reason=result.reason)

    text = _single_message(result.text)
    _remember(sender, "user", user_text)
    _remember(sender, "assistant", text)
    _count_reply()
    return Thought(text=text)
