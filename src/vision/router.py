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


def build_image_parts(attachments: list) -> list[dict]:
    """Descarga las fotos del turno (con tope) y las prepara para el modelo."""
    parts: list[dict] = []
    for kind, media_id in attachments:
        if kind != "image":
            continue
        if len(parts) >= config.IMAGES_PER_TURN:
            logger.info("Mas fotos de las que atiendo por turno; ignoro las demas.")
            break
        result = media.download(media_id, "image")
        if result is None:
            continue
        data, mime = result
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
        })
    return parts
