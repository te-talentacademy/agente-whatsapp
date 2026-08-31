"""El punto donde el agente decide qué contestar.

Cada fase del curso enciende aquí un órgano nuevo mediante su variable de
entorno; los órganos apagados ni siquiera se cargan, así que un problema en
uno de ellos jamás afecta a lo que ya funciona.

Orden de decisión por turno:
1. Cerebro encendido y con llave -> respuesta pensada.
2. Si el cerebro no puede (sin llave, llave inválida, respuesta vacía) -> acuse.
3. Si el cerebro falló por algo pasajero (red, saturación) -> se reintenta
   con pausa: mejor tarde que un acuse vacío.
"""

import logging

from src import config
from src.queue.store import Job, complete, fail, save_reply
from src.whatsapp.send import send_text

logger = logging.getLogger("agente")


def _user_text(job: Job) -> str:
    # Varios mensajes seguidos de la misma persona se atienden como uno solo.
    return "\n".join(t.strip() for t in job.texts if t and t.strip()).strip()


def _compose_reply(job: Job) -> tuple[str | None, str | None]:
    """Devuelve (texto, motivo_de_reintento). Si hay motivo, no se responde aún."""
    text = _user_text(job)

    # --- Cerebro (FEATURE_BRAIN=on) -------------------------------------------
    if config.brain_enabled() and text:
        if not config.openrouter_api_key():
            logger.warning(
                "FEATURE_BRAIN esta encendido pero falta OPENROUTER_API_KEY: "
                "respondo con el acuse simple mientras tanto."
            )
        else:
            from src.llm import brain  # carga perezosa: solo con interruptor y llave

            thought = brain.think(job.sender, text)
            if thought.text:
                return thought.text, None
            if thought.retryable:
                return None, f"cerebro: {thought.reason}"
            logger.error("El cerebro no pudo responder (%s): respondo con el acuse simple.", thought.reason)

    # --- Acuse simple ---------------------------------------------------------
    if config.auto_reply_enabled():
        return config.auto_reply_text(), None
    return None, None


def handle_job(job: Job) -> None:
    """Atiende una ficha de la fila. Jamás deja escapar un error."""
    try:
        if not job.message_ids:
            # Turno vacío (nada pendiente que cubrir): se cierra sin decir nada.
            complete(job)
            return
        if job.reply:
            # Reintento de envío: la respuesta ya se pensó; no se vuelve a pagar.
            reply = job.reply
        else:
            reply, retry_reason = _compose_reply(job)
            if retry_reason:
                logger.warning("Turno de %s pospuesto (%s); reintento con pausa.", job.sender, retry_reason)
                fail(job, retry_reason)
                return
            if reply is None:
                complete(job)
                return
            save_reply(job, reply)
            if config.log_message_text():
                logger.info('RESPUESTA a %s: "%s"', job.sender, reply[:300])
        result = send_text(job.sender, reply)
        if result.ok:
            complete(job)
        elif result.retryable:
            logger.warning("Envio a %s fallo (%s); reintento con pausa.", job.sender, result.reason)
            fail(job, result.reason)
        else:
            logger.error("Envio a %s rechazado sin remedio (%s).", job.sender, result.reason)
            complete(job)
    except Exception as exc:  # red de seguridad total
        logger.exception("Fallo inesperado atendiendo a %s", job.sender)
        try:
            fail(job, f"{exc.__class__.__name__}: {exc}")
        except Exception:
            logger.exception("No pude registrar el fallo de %s", job.sender)
