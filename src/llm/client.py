"""Conexión con OpenRouter: la central que conecta con el modelo que piensa.

Privacidad: cada solicitud lleva la política `data_collection: deny`, con la
que OpenRouter EXCLUYE a los proveedores marcados como que recopilan datos o
entrenan con ellos. OJO con lo que NO garantiza: no es cero retención (eso es
el control aparte `zdr`), y tanto el registro/retención del proveedor elegido
según sus propios términos como las políticas del propio OpenRouter siguen
aplicando. Sin esta política, OpenRouter podría además elegir un proveedor
que recopile las conversaciones.

Nunca lanza errores hacia arriba. Devuelve un resultado claro:
- ok         -> texto de la respuesta.
- retryable  -> falló por algo pasajero (red, saturación); vale reintentar.
- fatal      -> falló por algo que un reintento no arregla.
- refundable -> la solicitud fue RECHAZADA antes de procesarse (4xx); es el
                único caso en que se devuelve la cuota reservada. Un timeout es
                ambiguo (pudo cobrarse) y NO se devuelve.
"""

from dataclasses import dataclass

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 45.0
MAX_OUTPUT_TOKENS = 700

# Política de privacidad exigida en CADA solicitud (ver docstring).
PRIVACY_POLICY = {"data_collection": "deny"}


@dataclass
class LlmResult:
    ok: bool
    text: str = ""
    retryable: bool = False
    refundable: bool = False
    reason: str = ""


def build_payload(messages: list[dict], model: str) -> dict:
    return {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.6,
        "provider": dict(PRIVACY_POLICY),
    }


def complete(messages: list[dict], model: str, api_key: str, title: str = "") -> LlmResult:
    """Pide una respuesta al modelo.

    `messages` ya viene en orden: el mensaje de sistema SIEMPRE primero, luego
    el historial y al final el turno del usuario. Algunos proveedores se
    confunden si el sistema llega en otra posición.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if title:
        headers["X-Title"] = title[:60]
    try:
        response = httpx.post(OPENROUTER_URL, headers=headers, json=build_payload(messages, model), timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        # Red o timeout: ambiguo, pudo haberse procesado. No se devuelve cuota.
        return LlmResult(ok=False, retryable=True, reason=f"red: {exc.__class__.__name__}")

    code = response.status_code
    if code in (401, 403):
        return LlmResult(ok=False, refundable=True, reason="llave rechazada: revisa OPENROUTER_API_KEY")
    if code == 402:
        return LlmResult(ok=False, refundable=True, reason="sin saldo en OpenRouter: recarga tu cuenta")
    if code == 404:
        return LlmResult(ok=False, refundable=True, reason="modelo o proveedor no disponible con la política de privacidad: revisa OPENROUTER_MODEL")
    if code == 429:
        return LlmResult(ok=False, retryable=True, refundable=True, reason="servicio saturado (429)")
    if code >= 500:
        return LlmResult(ok=False, retryable=True, reason=f"servicio con problemas ({code})")
    if code >= 400:
        return LlmResult(ok=False, refundable=True, reason=f"rechazado ({code}): {response.text[:200]}")

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return LlmResult(ok=False, reason="respuesta con forma inesperada")
    if not isinstance(text, str) or not text.strip():
        return LlmResult(ok=False, reason="respuesta vacía del modelo")
    return LlmResult(ok=True, text=text.strip())
