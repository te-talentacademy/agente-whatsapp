"""Descarga de adjuntos entrantes (notas de voz, fotos).

Meta entrega los adjuntos en dos saltos: primero el identificador se cambia
por una dirección temporal, y de esa dirección se bajan los bytes. El
identificador caduca en minutos — por eso el agente descarga en cuanto
atiende el turno, no antes.

Reglas: tope de tamaño, y el tipo real se comprueba mirando los primeros
bytes del archivo (el tipo declarado puede mentir). Cualquier fallo devuelve
None: un adjunto imposible de bajar jamás tumba el turno.
"""

import logging

import httpx

from src import config
from src.whatsapp.send import GRAPH_BASE

logger = logging.getLogger("agente")

TIMEOUT_SECONDS = 20.0

# Firmas de los primeros bytes (magia) de los formatos que aceptamos.
AUDIO_MAGIC = [
    (b"OggS", "audio/ogg"),          # notas de voz de WhatsApp (opus)
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf1", "audio/aac"),
    (b"#!AMR", "audio/amr"),
]
IMAGE_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG", "image/png"),
    (b"RIFF", "image/webp"),
]


def _sniff(data: bytes, table: list) -> str | None:
    for magic, mime in table:
        if data.startswith(magic):
            return mime
    # mp4/m4a: la firma va en el byte 4
    if table is AUDIO_MAGIC and data[4:8] == b"ftyp":
        return "audio/mp4"
    return None


def download(media_id: str, kind: str) -> tuple[bytes, str] | None:
    """Baja un adjunto. Devuelve (bytes, tipo_real) o None."""
    token = config.whatsapp_token()
    if not token or not media_id:
        return None
    cap = config.MAX_AUDIO_BYTES if kind == "audio" else config.MAX_IMAGE_BYTES
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta = httpx.get(f"{GRAPH_BASE}/{media_id}", headers=headers, timeout=TIMEOUT_SECONDS)
        if meta.status_code >= 300:
            logger.warning("Adjunto %s: Meta respondio %s al pedir la direccion.", media_id[-8:], meta.status_code)
            return None
        url = meta.json().get("url")
        if not isinstance(url, str) or not url:
            return None
        data = b""
        with httpx.stream("GET", url, headers=headers, timeout=TIMEOUT_SECONDS) as response:
            if response.status_code >= 300:
                logger.warning("Adjunto %s: la descarga respondio %s.", media_id[-8:], response.status_code)
                return None
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > cap:
                    logger.warning("Adjunto %s: excede el tope de tamano; se ignora.", media_id[-8:])
                    return None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Adjunto %s: fallo la descarga (%s).", media_id[-8:], exc.__class__.__name__)
        return None
    mime = _sniff(data, AUDIO_MAGIC if kind == "audio" else IMAGE_MAGIC)
    if mime is None:
        logger.warning("Adjunto %s: el contenido no es del tipo esperado; se ignora.", media_id[-8:])
        return None
    return data, mime
