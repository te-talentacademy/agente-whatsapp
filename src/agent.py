"""El punto donde el agente decide qué contestar.

Cada fase del curso enciende aquí un órgano nuevo mediante su variable de
entorno; los órganos apagados ni siquiera se cargan, así que un problema en
uno de ellos jamás afecta a lo que ya funciona.

Orden de decisión por turno:
1. Oídos (FEATURE_VOICE_IN): las notas de voz se transcriben y entran como
   texto de la persona — heredan todos los blindajes del texto.
2. Ojos (FEATURE_VISION): con foto adjunta, el turno viaja al motor con
   visión; sin foto, la visión ni se invoca.
3. Cerebro encendido y con llave -> respuesta pensada.
4. Si el cerebro no puede (sin llave, rechazo) -> acuse. Fallo pasajero ->
   reintento con pausa.
5. Voz (FEATURE_VOICE_OUT): tras enviar el texto, la misma respuesta sale
   como nota de voz — best-effort absoluto, jamás frena la conversación.
"""

import logging
import threading
import time

from src import config
from src.queue.store import Job, complete, fail, save_reply
from src.whatsapp.send import send_text

logger = logging.getLogger("agente")


def _brain_ready() -> bool:
    return config.brain_enabled() and bool(config.openrouter_api_key())


def _senses_allowed(job: Job) -> bool:
    """Un turno que morirá en el aviso de tope del cerebro no debe oír ni ver."""
    from src.llm import brain

    if brain.quota_available():
        return True
    if any(kind in ("audio", "image") for kind, _ in job.attachments or []):
        logger.info("Cupo del cerebro agotado: no gasto en oidos/ojos para este turno.")
    return False


def _listen(job: Job) -> tuple[list[str], str | None]:
    """Los oídos: transcribe las notas de voz del turno (con topes).

    Devuelve (transcripciones, motivo_de_espera): un fallo PASAJERO de
    descarga o de Cartesia pospone el turno (backoff) en vez de perder la
    nota; los descartes definitivos se ignoran con log.
    """
    heard: list[str] = []
    if not (config.voice_in_enabled() and _brain_ready() and _senses_allowed(job)):
        return heard, None
    if not any(kind == "audio" for kind, _ in job.attachments or []):
        return heard, None
    if not config.cartesia_api_key():
        logger.warning("FEATURE_VOICE_IN esta encendido pero falta CARTESIA_API_KEY.")
        return heard, None
    from src.voice import cartesia, quota  # carga perezosa
    from src.whatsapp import media

    for kind, media_id in job.attachments or []:
        if kind != "audio":
            continue
        if len(heard) >= config.AUDIO_NOTES_PER_TURN:
            logger.info("Mas notas de voz de las que atiendo por turno; ignoro las demas.")
            break
        downloaded = media.download(media_id, "audio")
        if downloaded.retryable:
            return heard, f"nota de voz: {downloaded.reason}"
        if not downloaded.ok:
            logger.warning("Nota de voz descartada (%s).", downloaded.reason)
            continue
        if not quota.reserve():
            logger.warning("Tope diario de voz alcanzado (DAILY_VOICE_LIMIT); no transcribo mas hoy.")
            break
        result = cartesia.transcribe(downloaded.data, downloaded.mime)
        if result.ok:
            heard.append(f"[Nota de voz] {result.text}")
            if config.log_message_text():
                logger.info('NOTA DE VOZ transcrita de %s: "%s"', job.sender, result.text[:200])
            continue
        if result.refundable:
            quota.release()
        if result.retryable:
            return heard, f"oidos: {result.reason}"
        logger.warning("No pude transcribir una nota de voz (%s).", result.reason)
    return heard, None


def _see(job: Job) -> tuple[list[dict], str | None]:
    """Los ojos: prepara las fotos SOLO si hay adjuntos y el ramal está encendido."""
    if not (config.vision_enabled() and _brain_ready() and _senses_allowed(job)):
        return [], None
    if not any(kind == "image" for kind, _ in job.attachments or []):
        return [], None
    from src.vision import router  # carga perezosa

    return router.build_image_parts(job.attachments or [])


def _is_canned(reply: str) -> bool:
    """Los textos fijos (acuse, aviso de tope) jamás se locutan."""
    if reply == config.auto_reply_text():
        return True
    from src.llm import brain

    return reply == brain.LIMIT_REACHED_TEXT


def _compose_reply(job: Job) -> tuple[str | None, str | None]:
    """Devuelve (texto, motivo_de_reintento). Si hay motivo, no se responde aún."""
    written = [t.strip() for t in job.texts if t and t.strip()]

    # --- Comando del dueño (FEATURE_OUTBOUND_CALLS=on) ------------------------
    # "llamar +52..." desde el número del dueño ordena una llamada saliente.
    # Es literal (no pasa por el cerebro) y solo el dueño puede usarlo.
    if config.outbound_calls_enabled() and len(written) == 1:
        from src.calls import permissions  # carga perezosa

        command_reply = permissions.handle_text_command(job.sender, written[0])
        if command_reply is not None:
            return command_reply, None

    heard, postpone = _listen(job)
    if postpone:
        return None, postpone
    image_parts, postpone = _see(job)
    if postpone:
        return None, postpone
    text = "\n".join(written + heard).strip()
    has_media = any(kind in ("audio", "image") for kind, _ in job.attachments or [])

    # --- Cerebro (FEATURE_BRAIN=on) -------------------------------------------
    if config.brain_enabled() and (text or image_parts or has_media):
        if not config.openrouter_api_key():
            logger.warning(
                "FEATURE_BRAIN esta encendido pero falta OPENROUTER_API_KEY: "
                "respondo con el acuse simple mientras tanto."
            )
        else:
            from src.llm import brain  # carga perezosa: solo con interruptor y llave

            if not brain.quota_available():
                # También los turnos de solo-adjunto reciben el aviso honesto
                # (los sentidos ya se saltaron para no gastar en Cartesia).
                logger.warning("Tope diario del cerebro alcanzado; respondo el aviso fijo.")
                return brain.LIMIT_REACHED_TEXT, None
            if text or image_parts:
                thought = brain.think(job.sender, text, image_parts=image_parts)
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
            if config.voice_out_enabled() and not _is_canned(reply):
                # La voz corre aparte y en paralelo: si tarda o falla, el texto
                # ya está entregado y la fila sigue su ritmo.
                from src.voice import notes  # carga perezosa

                threading.Thread(
                    target=notes.speak_reply,
                    args=(job.sender, reply, job.oldest_at or time.time()),
                    daemon=True,
                ).start()
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
