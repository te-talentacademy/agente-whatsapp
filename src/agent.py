"""El punto donde el agente decide qué contestar.

Hoy (fase inicial) responde un acuse simple si AUTO_REPLY está encendido.
Cada fase del curso enciende aquí un órgano nuevo mediante su variable de
entorno; los órganos apagados ni siquiera se cargan, así que un problema en
uno de ellos jamás afecta a lo que ya funciona.
"""

import logging
import os

from src import config
from src.queue.store import Job, complete, fail
from src.whatsapp.send import send_text

logger = logging.getLogger("agente")


def _compose_reply(job: Job) -> str | None:
    # --- Cerebro (se enciende en su fase con FEATURE_BRAIN=on) -------------
    if config.flag("FEATURE_BRAIN"):
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            logger.warning(
                "FEATURE_BRAIN esta encendido pero falta OPENROUTER_API_KEY: "
                "respondo con el acuse simple mientras tanto."
            )
        else:
            # Carga perezosa: el módulo del cerebro solo se importa si su
            # interruptor está encendido Y su llave existe.
            from src import llm  # noqa: F401  (despierta en su fase)

            # El cerebro llegará en su fase; hasta entonces, acuse.
            pass

    # --- Acuse simple ------------------------------------------------------
    if config.auto_reply_enabled():
        return config.auto_reply_text()
    return None


def handle_job(job: Job) -> None:
    """Atiende una ficha de la fila. Jamás deja escapar un error."""
    try:
        reply = _compose_reply(job)
        if reply is None:
            # Nada que decir: la tanda queda atendida igualmente.
            complete(job)
            return
        result = send_text(job.sender, reply)
        if result.ok:
            complete(job)
        elif result.retryable:
            logger.warning("Envio a %s fallo (%s); reintento con pausa.", job.sender, result.reason)
            fail(job, result.reason)
        else:
            # Un error que no se arregla reintentando: se registra y la tanda
            # se cierra para no atascar la fila.
            logger.error("Envio a %s rechazado sin remedio (%s).", job.sender, result.reason)
            complete(job)
    except Exception as exc:  # red de seguridad total
        logger.exception("Fallo inesperado atendiendo a %s", job.sender)
        try:
            fail(job, f"{exc.__class__.__name__}: {exc}")
        except Exception:
            logger.exception("No pude registrar el fallo de %s", job.sender)
