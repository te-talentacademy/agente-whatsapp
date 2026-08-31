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
from dataclasses import dataclass

import httpx

from src import config
from src.whatsapp.send import GRAPH_BASE

logger = logging.getLogger("agente")

TIMEOUT_SECONDS = 20.0


@dataclass
class MediaResult:
    ok: bool
    data: bytes = b""
    mime: str = ""
    retryable: bool = False  # fallo pasajero: el turno debe esperar y reintentar
    reason: str = ""

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
]


def _sniff(data: bytes, table: list) -> str | None:
    for magic, mime in table:
        if data.startswith(magic):
            return mime
    # mp4/m4a: la firma va en el byte 4
    if table is AUDIO_MAGIC and data[4:8] == b"ftyp":
        return "audio/mp4"
    # webp: RIFF a secas tambien es WAV/AVI; hay que ver la marca WEBP
    if table is IMAGE_MAGIC and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _status_result(status: int, where: str) -> MediaResult:
    # 404/410: el identificador caducó o no existe — reintentar no lo revive.
    # El resto (credencial, saturación, 5xx) es pasajero o arreglable: esperar.
    if status in (404, 410):
        return MediaResult(ok=False, reason=f"{where}: el adjunto ya no esta disponible ({status})")
    return MediaResult(ok=False, retryable=True, reason=f"{where}: respuesta {status}")


def download(media_id: str, kind: str) -> MediaResult:
    """Baja un adjunto, distinguiendo el fallo pasajero del descarte definitivo."""
    token = config.whatsapp_token()
    if not token or not media_id:
        return MediaResult(ok=False, reason="sin credenciales o sin identificador")
    cap = config.MAX_AUDIO_BYTES if kind == "audio" else config.MAX_IMAGE_BYTES
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta = httpx.get(f"{GRAPH_BASE}/{media_id}", headers=headers, timeout=TIMEOUT_SECONDS)
        if meta.status_code >= 300:
            return _status_result(meta.status_code, "direccion")
        url = meta.json().get("url")
        if not isinstance(url, str) or not url:
            return MediaResult(ok=False, reason="Meta no entrego la direccion del adjunto")
        data = b""
        with httpx.stream("GET", url, headers=headers, timeout=TIMEOUT_SECONDS) as response:
            if response.status_code >= 300:
                return _status_result(response.status_code, "descarga")
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > cap:
                    return MediaResult(ok=False, reason="excede el tope de tamano")
    except httpx.HTTPError as exc:
        return MediaResult(ok=False, retryable=True, reason=f"red: {exc.__class__.__name__}")
    except ValueError:
        return MediaResult(ok=False, reason="respuesta de Meta con forma inesperada")
    mime = _sniff(data, AUDIO_MAGIC if kind == "audio" else IMAGE_MAGIC)
    if mime is None:
        return MediaResult(ok=False, reason="el contenido no es del tipo esperado")
    return MediaResult(ok=True, data=data, mime=mime)
