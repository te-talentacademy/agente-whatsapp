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


@dataclass
class CallEvent:
    """Un aviso del teléfono: llamada que suena, llamada que cuelga o
    respuesta a una solicitud de permiso de llamada saliente."""

    kind: str            # connect | terminate | permission_reply
    call_id: str = ""
    caller: str = ""     # el número de la otra persona
    sdp: str = ""        # la propuesta técnica de conexión (solo en connect)
    sdp_type: str = ""
    response: str = ""   # accept | reject (solo en permission_reply)
    is_permanent: bool = False
    expiration_timestamp: str = ""
    wamid: str = ""      # solo en permission_reply: id del mensaje que la trae
    timestamp: str = ""


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_stamp(value: Any) -> str:
    """Meta manda marcas de tiempo a veces como texto y a veces como número."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    return ""


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


def extract_call_events(payload: Any, own_phone_number_id: str) -> list[CallEvent]:
    """Devuelve los avisos del teléfono dirigidos a TU número.

    La autoridad es exacta: cualquier aviso cuyo metadata.phone_number_id no
    coincida con el número propio se ignora por completo — en una cuenta con
    varios números, cada servicio atiende SOLO su teléfono. Igual que
    extract_messages, tolera cualquier forma extraña sin lanzar errores.
    """
    result: list[CallEvent] = []
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
            # --- Llamadas (suena / cuelga) --------------------------------
            for call in _as_list(value.get("calls")):
                call_dict = _as_dict(call)
                call_id = _as_str(call_dict.get("id"))
                event = _as_str(call_dict.get("event")).lower()
                if not call_id or event not in ("connect", "terminate"):
                    continue
                session = _as_dict(call_dict.get("session"))
                result.append(
                    CallEvent(
                        kind=event,
                        call_id=call_id,
                        caller=_as_str(call_dict.get("from")),
                        sdp=_as_str(session.get("sdp")),
                        sdp_type=_as_str(session.get("sdp_type")),
                        timestamp=_as_str(call_dict.get("timestamp")),
                    )
                )
            # --- Respuestas a la solicitud de permiso ---------------------
            # Llegan como mensaje interactivo de la persona; aquí se separan
            # para que el teléfono las procese y el timbre no las encole como
            # conversación (ver routes.py).
            for message in _as_list(value.get("messages")):
                msg = _as_dict(message)
                interactive = _as_dict(msg.get("interactive"))
                reply = _as_dict(interactive.get("call_permission_reply"))
                if not reply:
                    continue
                sender = _as_str(msg.get("from"))
                wamid = _as_str(msg.get("id"))
                if not sender or not wamid:
                    continue
                result.append(
                    CallEvent(
                        kind="permission_reply",
                        caller=sender,
                        response=_as_str(reply.get("response")).lower(),
                        is_permanent=bool(reply.get("is_permanent")),
                        expiration_timestamp=_as_stamp(reply.get("expiration_timestamp")) or _as_stamp(reply.get("expiration_time")),
                        wamid=wamid,
                        timestamp=_as_str(msg.get("timestamp")),
                    )
                )
    return result
