"""Conexión con Cartesia: los oídos (entender audio) y la voz (hablar).

Privacidad: el audio de las notas de voz y el texto a locutar viajan a
Cartesia con la cuenta y la llave del dueño del agente, bajo los términos de
esa cuenta. Nada de esto ocurre con los ramales de voz apagados.

Nunca lanza errores. Devuelve un resultado claro con `refundable` para el
cupo (igual que el cerebro): solo un rechazo 4xx del proveedor devuelve la
solicitud apartada.
"""

import logging
from dataclasses import dataclass

import httpx

from src import config

logger = logging.getLogger("agente")

BASE = "https://api.cartesia.ai"
VERSION = "2026-08-14"  # verificada en vivo contra /stt y /tts/bytes
TTS_MODEL = "sonic-3.6"
STT_MODEL = "ink-whisper"      # verificado en vivo contra el endpoint por lotes
STT_FALLBACK_MODEL = "ink-2"   # plan B si algún día el primario se retira
TIMEOUT_SECONDS = 30.0


@dataclass
class VoiceResult:
    ok: bool
    data: bytes = b""
    text: str = ""
    retryable: bool = False   # fallo pasajero (red, 5xx): vale esperar y reintentar
    refundable: bool = False  # rechazo del proveedor sin procesar: devuelve el cupo
    reason: str = ""


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.cartesia_api_key()}",
        "Cartesia-Version": VERSION,
    }


def _classify(status: int, body: str) -> VoiceResult:
    if status in (401, 403):
        # Recuperable rotando la llave: el turno espera (backoff) y el cupo
        # apartado se devuelve — la nota de voz no se pierde por una rotación.
        return VoiceResult(ok=False, retryable=True, refundable=True,
                           reason="llave de Cartesia rechazada: revisa CARTESIA_API_KEY")
    if status == 402:
        return VoiceResult(ok=False, retryable=True, refundable=True, reason="sin saldo en Cartesia")
    if status == 429:
        return VoiceResult(ok=False, retryable=True, refundable=True, reason="Cartesia saturada (429)")
    if 400 <= status < 500:
        return VoiceResult(ok=False, refundable=True, reason=f"rechazado por Cartesia ({status}): {body[:150]}")
    return VoiceResult(ok=False, retryable=True, reason=f"Cartesia con problemas ({status})")


def transcribe(data: bytes, mime: str) -> VoiceResult:
    """Los oídos: audio -> texto. Modelo primario con un único plan B."""
    for model in (STT_MODEL, STT_FALLBACK_MODEL):
        try:
            response = httpx.post(
                f"{BASE}/stt",
                headers=_headers(),
                files={"file": ("nota" + _ext(mime), data, mime)},
                data={"model": model, "language": "es"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return VoiceResult(ok=False, retryable=True, reason=f"red: {exc.__class__.__name__}")
        if response.status_code < 300:
            try:
                text = response.json().get("text", "")
            except ValueError:
                return VoiceResult(ok=False, reason="respuesta de Cartesia con forma inesperada")
            text = text.strip() if isinstance(text, str) else ""
            if not text:
                return VoiceResult(ok=False, refundable=False, reason="audio sin palabras reconocibles")
            return VoiceResult(ok=True, text=text[:4000])
        model_gone = response.status_code in (400, 404) and "model" in response.text.lower()
        if model_gone and model == STT_MODEL:
            continue  # el modelo primario ya no existe o no aplica aquí: plan B
        return _classify(response.status_code, response.text)
    return VoiceResult(ok=False, reason="ningún modelo de oídos disponible")


def synthesize(text: str) -> VoiceResult:
    """La voz: texto -> audio MP3 con el clon del dueño."""
    voice_id = config.cartesia_voice_id()
    if not voice_id:
        return VoiceResult(ok=False, refundable=True, reason="falta CARTESIA_VOICE_ID")
    try:
        response = httpx.post(
            f"{BASE}/tts/bytes",
            headers={**_headers(), "Content-Type": "application/json"},
            json={
                "model_id": TTS_MODEL,
                "transcript": text,
                "voice": voice_id,
                "language": "es",
                "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return VoiceResult(ok=False, reason=f"red: {exc.__class__.__name__}")
    if response.status_code < 300:
        if not response.content:
            return VoiceResult(ok=False, reason="audio vacío de Cartesia")
        return VoiceResult(ok=True, data=response.content)
    return _classify(response.status_code, response.text)


def _ext(mime: str) -> str:
    return {
        "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/aac": ".aac",
        "audio/amr": ".amr", "audio/mp4": ".m4a",
    }.get(mime, ".bin")
