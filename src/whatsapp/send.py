"""Salida hacia WhatsApp: enviar un mensaje de texto.

Este módulo nunca lanza errores hacia arriba. Devuelve siempre un resultado
claro con tres posibilidades:
- ok        -> el mensaje salió.
- retryable -> falló por algo pasajero (red, saturación); vale reintentar.
- fatal     -> falló por algo que un reintento no arregla (petición inválida).
"""

from dataclasses import dataclass

import httpx

from src import config

GRAPH_BASE = "https://graph.facebook.com/v23.0"
TIMEOUT_SECONDS = 15.0
MAX_TEXT_LENGTH = 4096  # límite de WhatsApp por mensaje de texto


@dataclass
class SendResult:
    ok: bool
    retryable: bool = False
    reason: str = ""


def send_text(to: str, text: str) -> SendResult:
    token = config.whatsapp_token()
    number_id = config.phone_number_id()
    if not token or not number_id:
        return SendResult(ok=False, retryable=False, reason="faltan credenciales")
    body = text[:MAX_TEXT_LENGTH]
    try:
        response = httpx.post(
            f"{GRAPH_BASE}/{number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return SendResult(ok=False, retryable=True, reason=f"red: {exc.__class__.__name__}")

    if response.status_code < 300:
        return SendResult(ok=True)
    if response.status_code in (401, 403):
        # Credencial vencida o sin permiso: se reintenta por si la renuevas,
        # y el registro te avisa para que revises la llave.
        return SendResult(
            ok=False,
            retryable=True,
            reason=f"credencial rechazada ({response.status_code}): revisa WHATSAPP_TOKEN",
        )
    if response.status_code == 429 or response.status_code >= 500:
        return SendResult(
            ok=False, retryable=True, reason=f"servicio saturado ({response.status_code})"
        )
    # Resto de errores 4xx: la petición no es válida; reintentar no lo arregla.
    detail = response.text[:200]
    return SendResult(ok=False, retryable=False, reason=f"rechazado ({response.status_code}): {detail}")
