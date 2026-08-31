"""Los ojos: preparar las fotos del turno para el segundo motor.

Regla única y predecible: la visión SOLO trabaja cuando hay una imagen
adjunta. Sin foto no se invoca (ni se gasta); con foto, el turno viaja al
modelo con visión (`OPENROUTER_VISION_MODEL`) — así una imagen jamás llega a
un motor que la ignoraría en silencio.
"""

import base64
import logging

from src import config
from src.whatsapp import media

logger = logging.getLogger("agente")


def build_image_parts(attachments: list) -> tuple[list[dict], str | None]:
    """Descarga las fotos del turno (con tope) y las prepara para el modelo.

    Devuelve (partes, motivo_de_espera). Un fallo PASAJERO de descarga no
    descarta la foto: pospone el turno para reintentarlo con pausa; solo los
    descartes definitivos (caducado, tipo falso, tamaño) se ignoran con log.
    """
    parts: list[dict] = []
    for kind, media_id in attachments:
        if kind != "image":
            continue
        if len(parts) >= config.IMAGES_PER_TURN:
            logger.info("Mas fotos de las que atiendo por turno; ignoro las demas.")
            break
        result = media.download(media_id, "image")
        if result.retryable:
            return [], f"foto: {result.reason}"
        if not result.ok:
            logger.warning("Foto descartada (%s).", result.reason)
            continue
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{result.mime};base64,{base64.b64encode(result.data).decode()}"},
        })
    return parts, None
