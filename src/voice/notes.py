"""La voz saliente: convertir la respuesta en nota de voz y enviarla.

Es 100% "además de": el texto YA salió cuando esto corre. Cualquier fallo
aquí se registra y se olvida — la conversación jamás depende del audio.

Candados:
- Caducidad: un turno viejo (más de 10 minutos) no se locuta; audio tarde es
  peor que sin audio.
- Largo: una respuesta muy larga va solo en texto.
- Cupo diario de voz (compartido con los oídos), apartado antes de llamar.
- Si ffmpeg está disponible, el MP3 de Cartesia se convierte a OGG/Opus mono
  para que WhatsApp lo muestre como nota de voz de verdad; si no, se envía el
  MP3 como audio normal.
"""

import logging
import shutil
import subprocess
import time

from src import config
from src.voice import cartesia, quota
from src.whatsapp import send

logger = logging.getLogger("agente")


def _to_voice_note(mp3: bytes) -> tuple[bytes, str, str]:
    """MP3 -> OGG/Opus mono si hay ffmpeg; si no, se queda como MP3."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-c:a", "libopus", "-b:a", "24k", "-ac", "1", "-f", "ogg", "pipe:1"],
                input=mp3, capture_output=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout, "audio/ogg", "nota.ogg"
            logger.warning("ffmpeg no pudo convertir el audio (%s); envio MP3.", result.stderr[:120])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("ffmpeg fallo (%s); envio MP3.", exc.__class__.__name__)
    return mp3, "audio/mpeg", "nota.mp3"


def speak_reply(to: str, text: str, turn_started_at: float) -> None:
    """Best-effort absoluto: sintetiza y envía la nota de voz. Nunca lanza."""
    try:
        if time.time() - turn_started_at > config.VOICE_MAX_AGE_SECONDS:
            logger.info("Voz omitida: el turno es demasiado viejo para un audio.")
            return
        if len(text) > config.VOICE_MAX_CHARS:
            logger.info("Voz omitida: la respuesta es larga; va solo en texto.")
            return
        if not config.cartesia_api_key():
            logger.warning("FEATURE_VOICE_OUT esta encendido pero falta CARTESIA_API_KEY.")
            return
        if not quota.reserve():
            logger.warning("Tope diario de voz alcanzado (DAILY_VOICE_LIMIT); va solo texto.")
            return
        result = cartesia.synthesize(text)
        if not result.ok:
            if result.refundable:
                quota.release()
            logger.warning("La voz fallo (%s); la respuesta ya salio en texto.", result.reason)
            return
        audio, mime, filename = _to_voice_note(result.data)
        media_id = send.upload_media(audio, mime, filename)
        if not media_id:
            logger.warning("No pude subir la nota de voz a WhatsApp; va solo texto.")
            return
        outcome = send.send_audio(to, media_id)
        if outcome.ok:
            logger.info("Nota de voz enviada a %s (%s).", to, mime)
        else:
            logger.warning("El envio de la nota de voz fallo (%s); va solo texto.", outcome.reason)
    except Exception:
        logger.exception("Fallo inesperado en la voz; la conversacion sigue en texto.")
