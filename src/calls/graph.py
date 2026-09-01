"""Acciones de llamada contra la API de Meta.

Descolgar, rechazar, colgar e iniciar llamadas viajan por el mismo endpoint
(POST /<numero>/calls) con una acción distinta. Igual que el envío de texto,
este módulo nunca lanza errores: clasifica y devuelve un resultado claro.

Distinción que importa para el cupo: un RECHAZO inequívoco (4xx con
respuesta de Meta) confirma que la llamada no se conectó; un fallo de red o
timeout es AMBIGUO — el que llama decide qué hacer con esa ambigüedad.
"""

import logging
from dataclasses import dataclass

import httpx

from src import config

logger = logging.getLogger("agente")

TIMEOUT_SECONDS = 15.0


@dataclass
class CallActionResult:
    ok: bool
    call_id: str = ""      # solo al iniciar una saliente
    ambiguous: bool = False  # red/timeout: Meta pudo haber recibido la orden
    reason: str = ""


def _post_calls(payload: dict) -> CallActionResult:
    token = config.whatsapp_token()
    number_id = config.phone_number_id()
    if not token or not number_id:
        return CallActionResult(ok=False, reason="faltan credenciales de WhatsApp")
    try:
        response = httpx.post(
            f"{config.graph_base_url()}/{number_id}/calls",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", **payload},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return CallActionResult(
            ok=False, ambiguous=True, reason=f"red: {exc.__class__.__name__}"
        )
    if response.status_code < 300:
        call_id = ""
        try:
            body = response.json()
            calls = body.get("calls")
            if isinstance(calls, list) and calls and isinstance(calls[0], dict):
                cid = calls[0].get("id")
                call_id = cid if isinstance(cid, str) else ""
            elif isinstance(body.get("id"), str):
                call_id = body["id"]
        except ValueError:
            pass
        return CallActionResult(ok=True, call_id=call_id)
    if response.status_code == 429 or response.status_code >= 500:
        # Saturación: la orden no entró, pero el estado de Meta es dudoso.
        return CallActionResult(
            ok=False, ambiguous=True,
            reason=f"servicio saturado ({response.status_code})",
        )
    # 4xx con respuesta: rechazo inequívoco de Meta (la llamada no se conectó).
    return CallActionResult(
        ok=False,
        reason=f"rechazado ({response.status_code}): {response.text[:200]}",
    )


def pre_accept(call_id: str, sdp: str) -> CallActionResult:
    return _post_calls({
        "call_id": call_id,
        "action": "pre_accept",
        "session": {"sdp_type": "answer", "sdp": sdp},
    })


def accept(call_id: str, sdp: str) -> CallActionResult:
    return _post_calls({
        "call_id": call_id,
        "action": "accept",
        "session": {"sdp_type": "answer", "sdp": sdp},
    })


def reject(call_id: str) -> CallActionResult:
    return _post_calls({"call_id": call_id, "action": "reject"})


def terminate(call_id: str) -> CallActionResult:
    return _post_calls({"call_id": call_id, "action": "terminate"})


def connect(to: str, sdp: str) -> CallActionResult:
    """Inicia una llamada saliente (SOLO con permiso vigente de la persona)."""
    return _post_calls({
        "to": to,
        "action": "connect",
        "session": {"sdp_type": "offer", "sdp": sdp},
    })


def send_permission_request(to: str) -> CallActionResult:
    """Envía la solicitud de permiso de llamada (mensaje interactivo oficial)."""
    token = config.whatsapp_token()
    number_id = config.phone_number_id()
    if not token or not number_id:
        return CallActionResult(ok=False, reason="faltan credenciales de WhatsApp")
    name = config.agent_name()
    try:
        response = httpx.post(
            f"{config.graph_base_url()}/{number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "call_permission_request",
                    "body": {"text": f"{name} quisiera llamarte por WhatsApp. ¿Aceptas la llamada?"},
                    "action": {"name": "call_permission_request"},
                },
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return CallActionResult(ok=False, ambiguous=True, reason=f"red: {exc.__class__.__name__}")
    if response.status_code < 300:
        return CallActionResult(ok=True)
    if response.status_code == 429 or response.status_code >= 500:
        return CallActionResult(ok=False, ambiguous=True, reason=f"servicio saturado ({response.status_code})")
    return CallActionResult(ok=False, reason=f"rechazado ({response.status_code}): {response.text[:200]}")
