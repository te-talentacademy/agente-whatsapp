"""Lectura del sobre que manda Meta.

Meta envía los avisos muy anidados y mezcla de todo: mensajes nuevos, acuses
de entrega, cambios de estado. Aquí se extrae SOLO lo que importa (mensajes
dirigidos a TU número) y se ignora el resto sin quejarse.

Este módulo es puro a propósito: no toca red ni base de datos, y tolera
cualquier forma extraña del contenido sin lanzar errores.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class InboundMessage:
    wamid: str
    sender: str
    kind: str  # text | audio | image | video | document | sticker | other
    body: str
    timestamp: str
    media_id: str = ""  # identificador del adjunto en Meta (caduca en minutos)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def extract_messages(payload: Any, own_phone_number_id: str) -> list[InboundMessage]:
    """Devuelve los mensajes entrantes válidos dirigidos a tu número.

    Cualquier pieza con forma inesperada (nulos, tipos cambiados, campos
    ausentes) simplemente se salta: un sobre raro jamás tumba el servicio.
    """
    result: list[InboundMessage] = []
    root = _as_dict(payload)
    if root.get("object") != "whatsapp_business_account":
        return result
    for entry in _as_list(root.get("entry")):
        for change in _as_list(_as_dict(entry).get("changes")):
            value = _as_dict(_as_dict(change).get("value"))
            metadata = _as_dict(value.get("metadata"))
            if metadata.get("phone_number_id") != own_phone_number_id:
                # Aviso de otro número de la misma cuenta: no es asunto nuestro.
                continue
            for message in _as_list(value.get("messages")):
                msg = _as_dict(message)
                wamid = msg.get("id")
                sender = msg.get("from")
                if not isinstance(wamid, str) or not wamid:
                    continue
                if not isinstance(sender, str) or not sender:
                    continue
                kind = msg.get("type")
                if not isinstance(kind, str) or not kind:
                    kind = "other"
                body = ""
                media_id = ""
                if kind == "text":
                    body_value = _as_dict(msg.get("text")).get("body")
                    body = body_value if isinstance(body_value, str) else ""
                else:
                    attachment = _as_dict(msg.get(kind))
                    caption = attachment.get("caption")
                    body = caption if isinstance(caption, str) else ""
                    mid = attachment.get("id")
                    media_id = mid if isinstance(mid, str) else ""
                timestamp = msg.get("timestamp")
                result.append(
                    InboundMessage(
                        wamid=wamid,
                        sender=sender,
                        kind=kind,
                        body=body,
                        timestamp=timestamp if isinstance(timestamp, str) else "",
                        media_id=media_id,
                    )
                )
    return result
