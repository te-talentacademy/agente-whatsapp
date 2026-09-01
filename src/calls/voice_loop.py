"""El lazo de conversación de una llamada en curso.

Escucha → entiende → piensa → habla, en turnos, hasta que algo la termine:
la persona cuelga, se alcanza el tope de la llamada, el silencio se alarga
o el servicio se apaga. Devuelve siempre el MOTIVO del final — el mismo
vocabulario que usa el runbook.

La detección de turno es por energía (volumen): cuando la persona habla,
sube; cuando calla un momento, su intervención se cierra y se responde.
Sencillo y suficiente para una llamada telefónica; los umbrales de abajo
están calibrados con audio real de llamada (ver el informe de esta fase).
Mientras el agente habla no se escucha (sin interrupciones a mitad de
frase): es una limitación conocida y documentada de esta versión.
"""

import asyncio
import io
import logging
import math
import time
import wave
from array import array

from src import config
from src.calls import webrtc

logger = logging.getLogger("agente")

# --- Detección de turno (energía) — calibrar con audio real de la batería ---
VAD_FRAME_MS = 20
VAD_FRAME_BYTES = webrtc.HEAR_RATE * VAD_FRAME_MS // 1000 * 2  # PCM s16 mono
VAD_RMS_SPEECH = 350.0        # por encima: la persona está hablando
VAD_START_FRAMES = 3           # ~60 ms seguidos de voz abren la intervención
VAD_END_SILENCE_SECONDS = 0.8  # esta pausa cierra la intervención
MIN_UTTERANCE_SECONDS = 0.3    # menos que esto es un ruido, no una frase

REPEAT_TEXT = "Perdón, no te escuché bien. ¿Me lo repites?"
MAX_TURN_FAILURES = 2          # dos turnos seguidos sin poder responder: colgar

CALL_STYLE_OVERLAY = (
    "Estás atendiendo una LLAMADA de voz: responde hablado, en dos o tres "
    "frases cortas y naturales, sin listas, sin formato y sin leer enlaces. "
    "Si no entendiste, pide que lo repitan."
)
CALL_MAX_TOKENS = 200
GOODBYE_DRAIN_SECONDS = 10.0   # tiempo máximo para que la despedida se oiga


def _rms(chunk: bytes) -> float:
    samples = array("h", chunk[: (len(chunk) // 2) * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(webrtc.HEAR_RATE)
        writer.writeframes(pcm)
    return out.getvalue()


class _Ended(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


async def converse(session: webrtc.MediaSession, caller: str, call_id: str,
                   deadline_at: float, stop_event: asyncio.Event) -> str:
    """Conversa hasta que la llamada termina. Devuelve el motivo del final."""
    failures = 0
    try:
        if not await _say(session, config.call_greeting_text()):
            return "sin-voz"
        while True:
            utterance = await _collect_utterance(session, deadline_at, stop_event)
            text = await _transcribe(utterance)
            if text is None:
                failures += 1
                if failures > MAX_TURN_FAILURES:
                    raise _Ended("sin-oidos")
                await _say(session, REPEAT_TEXT)
                continue
            reply = await _think(caller, text)
            if reply is None:
                failures += 1
                if failures > MAX_TURN_FAILURES:
                    raise _Ended("sin-cerebro")
                await _say(session, REPEAT_TEXT)
                continue
            failures = 0
            if not await _say(session, reply):
                raise _Ended("sin-voz")
    except _Ended as ended:
        if ended.reason in ("tope-llamada", "silencio", "apagado"):
            await _say(session, config.call_goodbye_text())
            await _drain(session)
        return ended.reason


async def _collect_utterance(session: webrtc.MediaSession, deadline_at: float,
                             stop_event: asyncio.Event) -> bytes:
    """Espera y junta UNA intervención de la persona.

    Vigila a la vez: el tope de la llamada, el silencio prolongado, el
    apagado del servicio y el colgado del otro lado.
    """
    pending = b""
    utterance = bytearray()
    speech_frames = 0
    silence_frames = 0
    collecting = False
    last_speech_at = time.time()
    max_frames = int(config.MAX_UTTERANCE_SECONDS * 1000 / VAD_FRAME_MS)
    end_silence_frames = int(VAD_END_SILENCE_SECONDS * 1000 / VAD_FRAME_MS)
    min_frames = int(MIN_UTTERANCE_SECONDS * 1000 / VAD_FRAME_MS)
    collected_frames = 0

    while True:
        if stop_event.is_set():
            raise _Ended("apagado")
        if session.closed.is_set():
            raise _Ended("colgado")
        if time.time() >= deadline_at:
            raise _Ended("tope-llamada")
        if not collecting and time.time() - last_speech_at > config.CALL_IDLE_TIMEOUT_SECONDS:
            raise _Ended("silencio")
        try:
            chunk = await asyncio.wait_for(session.hear_queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if session.speaking_seconds_left() > 0.05:
            # Mientras el agente habla no se escucha (sin interrupciones).
            last_speech_at = time.time()
            continue
        pending += chunk
        while len(pending) >= VAD_FRAME_BYTES:
            frame = pending[:VAD_FRAME_BYTES]
            pending = pending[VAD_FRAME_BYTES:]
            speaking = _rms(frame) >= VAD_RMS_SPEECH
            if not collecting:
                if speaking:
                    speech_frames += 1
                    utterance.extend(frame)
                    if speech_frames >= VAD_START_FRAMES:
                        collecting = True
                        last_speech_at = time.time()
                else:
                    speech_frames = 0
                    utterance.clear()
                continue
            utterance.extend(frame)
            collected_frames += 1
            if speaking:
                silence_frames = 0
                last_speech_at = time.time()
            else:
                silence_frames += 1
            if silence_frames >= end_silence_frames or collected_frames >= max_frames:
                if collected_frames >= max_frames:
                    logger.info("LLAMADA: intervención cortada al tope de %.0f s.",
                                config.MAX_UTTERANCE_SECONDS)
                if len(utterance) // VAD_FRAME_BYTES < min_frames:
                    # Demasiado corto para ser una frase: se descarta y se sigue.
                    utterance.clear()
                    collecting = False
                    speech_frames = 0
                    silence_frames = 0
                    collected_frames = 0
                    continue
                seconds = len(utterance) / 2 / webrtc.HEAR_RATE
                logger.info(
                    "LLAMADA: intervención de %.1f s (energía media %.0f).",
                    seconds, _rms(bytes(utterance)),
                )
                return bytes(utterance)


async def _transcribe(utterance: bytes) -> str | None:
    """Los oídos del teléfono: la intervención -> texto (cupo de voz incluido)."""
    from src.voice import cartesia, quota

    if not quota.reserve():
        logger.warning("LLAMADA: tope diario de voz alcanzado a mitad de llamada.")
        raise _Ended("cupo-voz")
    result = await asyncio.to_thread(cartesia.transcribe, _wav(utterance), "audio/wav")
    if result.ok:
        if config.log_message_text():
            logger.info('LLAMADA: escuché "%s"', result.text[:200])
        return result.text
    if result.refundable:
        await asyncio.to_thread(quota.release)
    logger.warning("LLAMADA: no entendí la intervención (%s).", result.reason)
    return None


async def _think(caller: str, text: str) -> str | None:
    from src.llm import brain

    thought = await asyncio.to_thread(
        brain.think, caller, text, None, CALL_STYLE_OVERLAY, CALL_MAX_TOKENS
    )
    if thought.text:
        if config.log_message_text():
            logger.info('LLAMADA: respondo "%s"', thought.text[:200])
        return thought.text
    logger.warning("LLAMADA: el cerebro no pudo responder (%s).", thought.reason)
    return None


async def _say(session: webrtc.MediaSession, text: str) -> bool:
    """La voz del teléfono: texto -> audio en el aire (cupo de voz incluido)."""
    from src.voice import cartesia, quota

    if not text:
        return True
    if not quota.reserve():
        logger.warning("LLAMADA: tope diario de voz alcanzado; no puedo hablar más hoy.")
        return False
    result = await asyncio.to_thread(cartesia.synthesize, text, cartesia.PCM_FORMAT)
    if not result.ok:
        if result.refundable:
            await asyncio.to_thread(quota.release)
        logger.warning("LLAMADA: no pude hablar (%s).", result.reason)
        return False
    pcm = webrtc.resample_to_speak(result.data, cartesia.PCM_SAMPLE_RATE)
    session.speak(pcm)
    return True


async def _drain(session: webrtc.MediaSession) -> None:
    """Espera (con tope) a que lo dicho termine de salir antes de colgar."""
    waited = 0.0
    while session.speaking_seconds_left() > 0.05 and waited < GOODBYE_DRAIN_SECONDS:
        await asyncio.sleep(0.2)
        waited += 0.2
