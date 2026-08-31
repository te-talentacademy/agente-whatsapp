"""Conexión con OpenRouter: la central que conecta con el modelo que piensa.

Nunca lanza errores hacia arriba. Devuelve un resultado claro:
- ok        -> texto de la respuesta.
- retryable -> falló por algo pasajero (red, saturación); vale reintentar.
- fatal     -> falló por algo que un reintento no arregla (llave inválida,
               modelo inexistente, respuesta vacía).
"""

from dataclasses import dataclass

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 45.0
MAX_OUTPUT_TOKENS = 700


@dataclass
class LlmResult:
    ok: bool
    text: str = ""
    retryable: bool = False
    reason: str = ""


def complete(messages: list[dict], model: str, api_key: str, title: str = "") -> LlmResult:
    """Pide una respuesta al modelo.

    `messages` ya viene en orden: el mensaje de sistema SIEMPRE primero, luego
    el historial y al final el turno del usuario. Algunos proveedores se
    confunden si el sistema llega en otra posición.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if title:
        headers["X-Title"] = title[:60]
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.6,
    }
    try:
        response = httpx.post(OPENROUTER_URL, headers=headers, json=payload, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return LlmResult(ok=False, retryable=True, reason=f"red: {exc.__class__.__name__}")

    if response.status_code in (401, 403):
        return LlmResult(ok=False, retryable=False, reason="llave rechazada: revisa OPENROUTER_API_KEY")
    if response.status_code == 402:
        return LlmResult(ok=False, retryable=False, reason="sin saldo en OpenRouter: recarga tu cuenta")
    if response.status_code == 404:
        return LlmResult(ok=False, retryable=False, reason="modelo no encontrado: revisa OPENROUTER_MODEL")
    if response.status_code == 429 or response.status_code >= 500:
        return LlmResult(ok=False, retryable=True, reason=f"servicio saturado ({response.status_code})")
    if response.status_code >= 400:
        return LlmResult(ok=False, retryable=False, reason=f"rechazado ({response.status_code}): {response.text[:200]}")

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return LlmResult(ok=False, retryable=False, reason="respuesta con forma inesperada")
    if not isinstance(text, str) or not text.strip():
        return LlmResult(ok=False, retryable=False, reason="respuesta vacía del modelo")
    return LlmResult(ok=True, text=text.strip())
