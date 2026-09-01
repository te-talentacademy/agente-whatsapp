"""Llamadas salientes: el permiso primero, siempre.

WhatsApp no deja llamar a nadie sin su permiso expreso: primero se envía
una solicitud (un mensaje con botones que la persona acepta o rechaza) y
solo con el sí se marca. Este módulo maneja las dos puntas:

1. El COMANDO DEL DUEÑO: el dueño (CALL_OWNER_NUMBER) escribe al agente
   "llamar +52..." por WhatsApp y el agente hace el resto. Nadie más puede
   ordenar llamadas, y el comando es literal — no pasa por el cerebro.
2. La RESPUESTA de la persona: cuando acepta, el agente marca en ese
   momento (si los cupos lo permiten) y le confirma cada paso al dueño con
   estados claros: solicitud enviada / permiso recibido / llamando /
   rechazado. Nunca un "listo" antes de tiempo.

Los límites de solicitudes de Meta (1 por día, 2 por semana por persona) se
respetan LOCALMENTE antes de tocar su API (ver state.reserve_request).
"""

import asyncio
import logging
import re
import time

from src import config
from src.calls import numbers, state
from src.webhook.parse import CallEvent

logger = logging.getLogger("agente")

# "llamar +52 33 1234 5678" / "llama al +34611222333" — literal y nada más.
COMMAND_RE = re.compile(r"^\s*llamar?\s+(?:al?\s+)?(\+?[\d\s().-]{7,20})\s*$", re.IGNORECASE)

# El lazo de eventos del servicio; main.py lo registra al arrancar para que
# el trabajador de fondo (un hilo) pueda encargar llamadas al teléfono.
_loop: asyncio.AbstractEventLoop | None = None


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def handle_text_command(sender: str, text: str) -> str | None:
    """Si `text` es un comando de llamada del dueño, lo ejecuta y devuelve la
    respuesta para el dueño. Si no, devuelve None y el turno sigue normal.

    Corre en el trabajador de fondo (hilo): todo lo que toca red aquí es
    corto (una solicitud de permiso); marcar la llamada va al lazo async.
    """
    if not config.outbound_calls_enabled():
        return None
    owner = config.call_owner_number()
    if not owner or not numbers.same(sender, owner):
        return None
    match = COMMAND_RE.match(text or "")
    if not match:
        return None
    target = numbers.canonical(match.group(1))
    if len(target) < 7:
        return "Ese número no se ve completo. Escríbeme: llamar +52..."

    if state.permission_valid(target):
        submitted = _submit_call(target)
        if submitted is None:
            return f"Permiso vigente de +{target}: llamando ahora."
        return f"Permiso vigente de +{target}, pero no pude llamar: {submitted}."

    ok, reason = state.reserve_request(target)
    if not ok:
        return f"No puedo pedir permiso a +{target} todavía: {reason}."
    from src.calls import graph

    result = graph.send_permission_request(target)
    if result.ok:
        logger.info("LLAMADA saliente: solicitud de permiso enviada a %s.", target)
        return (
            f"Solicitud de permiso enviada a +{target}. "
            "En cuanto acepte, marco y te aviso."
        )
    if result.ambiguous:
        # La red falló a medias: la solicitud pudo llegar. La reserva local se
        # conserva (no se repite sola) y el dueño decide si insistir mañana.
        logger.warning("LLAMADA saliente: solicitud a %s AMBIGUA (%s).", target, result.reason)
        return (
            f"No estoy seguro de que la solicitud a +{target} haya salido "
            f"({result.reason}). No la repito sola para no molestar; "
            "si no llega respuesta, inténtalo de nuevo mañana."
        )
    logger.warning("LLAMADA saliente: solicitud a %s rechazada (%s).", target, result.reason)
    return f"No pude enviar la solicitud a +{target}: {result.reason}."


async def handle_reply(event: CallEvent) -> None:
    """La persona respondió a la solicitud de permiso (llega por el timbre).

    Corre como tarea CON DUEÑO del teléfono (manager la registra): aplicar
    la transición, avisar al dueño y, con el sí, marcar — sin frenar el
    timbre y sin que un reenvío dispare nada dos veces (la transición
    guardada lo impide).
    """
    target = numbers.canonical(event.caller)
    expires_at = None
    if event.expiration_timestamp:
        try:
            expires_at = float(event.expiration_timestamp)
        except ValueError:
            expires_at = None
    if expires_at is None and not event.is_permanent and event.response == "accept":
        # Sin caducidad explícita, Meta concede una ventana de 72 h.
        expires_at = time.time() + 72 * 3600

    outcome = await asyncio.to_thread(
        state.apply_permission_reply, target, event.response,
        event.is_permanent, expires_at,
    )
    if outcome == "stale":
        # Reenvío o respuesta tardía de una solicitud vieja: no dispara nada.
        logger.info("Permiso de %s: respuesta repetida o tardía; la ignoro.", target)
        return
    if outcome == "denied":
        logger.info("Permiso de %s: la persona dijo que no.", target)
        await asyncio.to_thread(
            _notify_owner,
            f"+{target} no aceptó la llamada. No insistas hoy: los permisos "
            "tienen límite semanal.",
        )
        return
    logger.info("Permiso de %s: ACEPTADO%s.", target,
                " (permanente)" if event.is_permanent else "")
    await asyncio.to_thread(
        _notify_owner, f"Permiso recibido de +{target}: llamando ahora."
    )
    from src.calls import manager

    refusal = await manager.start_outbound(target)
    if refusal is not None:
        await asyncio.to_thread(
            _notify_owner, f"No pude llamar a +{target}: {refusal}."
        )


def _submit_call(target: str) -> str | None:
    """Encarga la llamada al teléfono DESDE EL HILO del trabajador de fondo.
    None = en camino; texto = motivo del no. (El lazo async jamás entra aquí:
    esperar un future del propio lazo lo congelaría.)"""
    from src.calls import manager

    if _loop is None or _loop.is_closed():
        return "el teléfono aún no está listo"
    future = asyncio.run_coroutine_threadsafe(manager.start_outbound(target), _loop)
    try:
        return future.result(timeout=30.0)
    except Exception as exc:
        logger.exception("LLAMADA saliente a %s: fallo al iniciarla.", target)
        return f"fallo inesperado ({exc.__class__.__name__})"


def _notify_owner(text: str) -> None:
    """Confirmaciones al dueño, con su estado claro (best-effort)."""
    owner = config.call_owner_number()
    if not owner:
        return
    from src.whatsapp.send import send_text

    result = send_text(numbers.canonical(owner), text)
    if not result.ok:
        logger.warning("No pude avisar al dueño (%s).", result.reason)
