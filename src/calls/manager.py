"""El encargado del teléfono: decide qué llamada entra, la atiende y cuelga.

Contratos que este módulo garantiza (y los tests vigilan):

- CANDADOS EN ORDEN, del más barato al más caro: interruptor, lista de
  permitidos, una-llamada-a-la-vez, credenciales, cupos del día. El primero
  que falla rechaza la llamada con un motivo claro; nada se gasta después
  de un candado cerrado.
- RESERVA ANTES QUE ACCEPT: los segundos del día se apartan y quedan
  escritos en la memoria ANTES de mandar el accept a Meta. Un rechazo
  inequívoco de Meta devuelve la reserva; un fallo ambiguo (red, timeout)
  la CONSERVA — el lado seguro es contar de más, jamás de menos.
- CADA TAREA TIENE DUEÑO: la tarea de una llamada queda registrada aquí con
  referencia fuerte antes de soltar el timbre, envuelta en una frontera
  total de fallo (una excepción produce log + limpieza + estado terminal,
  nunca una llamada colgada en el aire).
- NADA SE ABANDONA: al arrancar, las llamadas que un reinicio dejó a medias
  se cierran con motivo `reinicio`; un aviso repetido sobre una llamada
  huérfana la cierra con motivo `lease-vencido`. Ambos compiten por la
  misma transición y solo uno actúa (ver state.recover).
"""

import asyncio
import logging
import time

from src import config
from src.calls import numbers, state
from src.webhook.parse import CallEvent

logger = logging.getLogger("agente")

STOP_EVENT = asyncio.Event()
ACCEPT_DEADLINE_SECONDS = 25.0   # margen propio dentro de la ventana de Meta
CLOSE_TIMEOUT_SECONDS = 6.0      # cerrar el audio jamás retrasa la contabilidad

_sessions: dict[str, object] = {}   # call_id -> MediaSession (llamadas vivas)
_tasks: dict[str, asyncio.Task] = {}
_outbound_pending: dict[str, object] = {}  # call_id -> MediaSession esperando answer


def active_calls() -> int:
    # Las tareas breves (recuperación, permisos) no ocupan línea de llamada.
    return sum(1 for key in _tasks if ":recover" not in key and not key.startswith("perm:"))


# ---------------------------------------------------------------------------
# Entrada desde el timbre (rápida: reclama y agenda; la red va en tareas)
# ---------------------------------------------------------------------------


async def dispatch_events(events: list[CallEvent]) -> None:
    for event in events:
        try:
            if event.kind == "connect":
                _on_connect(event)
            elif event.kind == "terminate":
                await _on_terminate(event)
            elif event.kind == "permission_reply":
                _on_permission_reply(event)
        except Exception:
            logger.exception("Fallo inesperado atendiendo un aviso del teléfono.")


def _register_task(call_id: str, coro) -> None:
    """Crea la tarea de la llamada con dueño: referencia fuerte + limpieza."""
    task = asyncio.create_task(coro)
    _tasks[call_id] = task

    def _done(t: asyncio.Task) -> None:
        _tasks.pop(call_id, None)
        _sessions.pop(call_id, None)
        _outbound_pending.pop(call_id, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Tarea del teléfono %s terminó con error: %s", call_id, exc,
                exc_info=exc,
            )

    task.add_done_callback(_done)


def _on_connect(event: CallEvent) -> None:
    # ¿Es la respuesta de una llamada saliente nuestra que la persona descolgó?
    pending = _outbound_pending.get(event.call_id)
    if pending is not None and event.sdp:
        _register_task(event.call_id, _run_outbound_answer(event, pending))
        return

    if state.claim(event.call_id, event.caller, "in"):
        _register_task(event.call_id, _run_inbound(event))
        return

    # Reclamo perdido: la fila ya existe. ¿Duplicado en vuelo, ya atendida o huérfana?
    row = state.get(event.call_id)
    if row is None or row["state"] in state.TERMINAL_STATES:
        return  # ya hubo acción terminal para esta llamada
    if event.call_id in _sessions or event.call_id in _tasks:
        return  # en vuelo aquí mismo: el aviso es un reenvío de Meta
    # Huérfana (reinicio con reenvío posterior): cerrar con motivo explícito.
    if state.recover(event.call_id, "lease-vencido", require_lease=True):
        logger.warning(
            "LLAMADA %s: quedó a medias (lease vencido); la cierro y rechazo el reaviso.",
            event.call_id,
        )
        _register_task(event.call_id + ":recover", _best_effort_reject(event.call_id))


async def _on_terminate(event: CallEvent) -> None:
    ended = state.transition(
        event.call_id, "ended", ("claimed", "accepting", "active", "ending"),
        ended_at=time.time(),
    )
    if not ended:
        return  # reenvío: la llamada ya estaba cerrada (limpieza no-op)
    session = _sessions.get(event.call_id)
    if session is not None:
        session.closed.set()
    logger.info("LLAMADA %s: la otra parte colgó.", event.call_id)


def _on_permission_reply(event: CallEvent) -> None:
    from src.calls import permissions

    # Tarea con dueño (misma frontera M2): el timbre no espera a Meta.
    _register_task(
        f"perm:{event.caller}:{event.wamid or int(time.time())}",
        permissions.handle_reply(event),
    )


# ---------------------------------------------------------------------------
# Llamada entrante
# ---------------------------------------------------------------------------


def _gate_reason(caller: str) -> str | None:
    """La cadena de candados, del más barato al más caro. None = pasa."""
    if not config.calls_enabled():
        return "FEATURE_CALLS está apagado"
    allowed = config.call_allowed_numbers()
    if allowed and numbers.canonical(caller) not in {numbers.canonical(n) for n in allowed}:
        return "número fuera de la lista de permitidos"
    if active_calls() > config.CALL_MAX_CONCURRENT:
        return "ya hay una llamada en curso"
    if not (config.brain_enabled() and config.openrouter_api_key()):
        return "el cerebro está apagado o sin llave (las llamadas lo necesitan)"
    if not config.cartesia_api_key() or not config.cartesia_voice_id():
        return "faltan CARTESIA_API_KEY / CARTESIA_VOICE_ID (la voz de la llamada)"
    if not config.turn_key_id() or not config.turn_api_token():
        return "sin-relay-vigente: faltan las llaves TURN de Cloudflare"
    from src.llm import brain
    from src.voice import quota

    if not brain.quota_available():
        return "tope diario del cerebro alcanzado"
    if not quota.available():
        return "tope diario de voz alcanzado"
    remaining = state.daily_seconds_remaining()
    if remaining is not None and remaining <= 0:
        return "tope diario de minutos de llamada alcanzado"
    return None


def _granted_seconds() -> int:
    remaining = state.daily_seconds_remaining()
    per_call = config.call_max_minutes() * 60
    if remaining is None:
        return per_call
    return min(per_call, remaining)


async def _run_inbound(event: CallEvent) -> None:
    """La vida completa de una llamada entrante, con frontera total de fallo."""
    from src.calls import graph, relay, voice_loop, webrtc

    call_id = event.call_id
    session = None
    granted = 0
    answered_at = None
    try:
        reason = _gate_reason(event.caller)
        if reason is not None:
            logger.warning("LLAMADA %s rechazada: %s.", call_id, reason)
            state.transition(call_id, "rejected", ("claimed",),
                             last_error=reason, ended_at=time.time())
            await asyncio.to_thread(graph.reject, call_id)
            return

        started = time.time()
        granted = _granted_seconds()
        # Reserva CONFIRMADA en la memoria antes de cualquier accept (el cupo
        # jamás corre detrás de una llamada ya aceptada).
        if not state.reserve_seconds(call_id, granted):
            state.transition(call_id, "rejected", ("claimed",),
                             last_error="cupo diario agotado", ended_at=time.time())
            await asyncio.to_thread(graph.reject, call_id)
            return

        ttl = granted + config.TURN_TTL_MARGIN_SECONDS
        relay_result = await asyncio.to_thread(relay.fetch, ttl)
        if not relay_result.ok:
            logger.warning("LLAMADA %s rechazada: %s.", call_id, relay_result.reason)
            state.refund_full_reserve(call_id)  # nada llegó a Meta: rechazo propio
            state.transition(call_id, "rejected", ("claimed",),
                             last_error=relay_result.reason, ended_at=time.time())
            await asyncio.to_thread(graph.reject, call_id)
            return

        state.transition(call_id, "accepting", ("claimed",))
        session = webrtc.MediaSession()
        _sessions[call_id] = session
        answer_sdp = await asyncio.wait_for(
            session.answer_inbound(event.sdp, relay_result),
            timeout=ACCEPT_DEADLINE_SECONDS,
        )
        if time.time() - started > ACCEPT_DEADLINE_SECONDS:
            raise TimeoutError("la preparación no cupo en la ventana de Meta")

        pre = await asyncio.to_thread(graph.pre_accept, call_id, answer_sdp)
        if not pre.ok:
            logger.warning("LLAMADA %s: pre_accept falló (%s); sigo al accept.", call_id, pre.reason)
        if time.time() - started > ACCEPT_DEADLINE_SECONDS:
            # La ventana de Meta se agotó antes del accept: rechazo honesto
            # (nada se conectó) en lugar de un accept tardío que fallaría mudo.
            logger.warning("LLAMADA %s: la preparación agotó la ventana; la rechazo.", call_id)
            state.refund_full_reserve(call_id)
            state.transition(call_id, "failed", ("accepting",),
                             last_error="ventana-agotada", ended_at=time.time())
            await asyncio.to_thread(graph.reject, call_id)
            return
        accept = await asyncio.to_thread(graph.accept, call_id, answer_sdp)
        if not accept.ok:
            if accept.ambiguous:
                # Meta pudo haber conectado: la reserva SE CONSERVA completa.
                logger.error("LLAMADA %s: accept AMBIGUO (%s); conservo la reserva.",
                             call_id, accept.reason)
                state.transition(call_id, "failed", ("accepting",),
                                 last_error=f"accept ambiguo: {accept.reason}",
                                 ended_at=time.time())
            else:
                logger.warning("LLAMADA %s: Meta rechazó el accept (%s).", call_id, accept.reason)
                state.refund_full_reserve(call_id)
                state.transition(call_id, "failed", ("accepting",),
                                 last_error=f"accept rechazado: {accept.reason}",
                                 ended_at=time.time())
            return

        answered_at = time.time()
        state.transition(call_id, "active", ("accepting",), answered_at=answered_at)
        logger.info("LLAMADA %s: descolgada (concedidos %d s).", call_id, granted)
        deadline_at = answered_at + granted
        end_reason = await voice_loop.converse(
            session, event.caller, call_id, deadline_at, STOP_EVENT
        )
        logger.info("LLAMADA %s: terminó (%s).", call_id, end_reason)
    except Exception as exc:
        logger.exception("LLAMADA %s: fallo inesperado; la cierro.", call_id)
        state.transition(
            call_id, "failed",
            ("claimed", "accepting", "active", "ending"),
            last_error=f"{exc.__class__.__name__}: {exc}", ended_at=time.time(),
        )
        if answered_at is None:
            await asyncio.to_thread(graph.reject, call_id)
        else:
            await asyncio.to_thread(graph.terminate, call_id)
    finally:
        if answered_at is not None:
            # La contabilidad va PRIMERO: cerrar el audio puede tardar y el
            # cupo del día no espera a nadie.
            end_at = time.time()
            state.transition(call_id, "ended", ("active", "ending"), ended_at=end_at)
            state.reconcile_seconds(call_id, int(end_at - answered_at))
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("LLAMADA %s: el cierre del audio no terminó a tiempo.", call_id)
        if answered_at is not None:
            await asyncio.to_thread(graph.terminate, call_id)


# ---------------------------------------------------------------------------
# Llamada saliente (la coloca permissions.py con permiso vigente)
# ---------------------------------------------------------------------------


async def start_outbound(to: str) -> str | None:
    """Inicia una saliente. Devuelve el motivo del rechazo o None si va en camino."""
    from src.calls import graph, relay, webrtc

    if not config.outbound_calls_enabled():
        return "FEATURE_OUTBOUND_CALLS está apagado"
    reason = _gate_reason(to)
    if reason is not None and not reason.startswith("número fuera"):
        # La lista de permitidos regula quién puede LLAMARTE; aquí decides tú.
        return reason
    if not state.permission_valid(to):
        return "sin permiso vigente de esa persona"

    granted = _granted_seconds()
    ttl = granted + config.TURN_TTL_MARGIN_SECONDS
    relay_result = await asyncio.to_thread(relay.fetch, ttl)
    if not relay_result.ok:
        return relay_result.reason
    session = webrtc.MediaSession()
    offer_sdp = await session.offer_outbound(relay_result)
    result = await asyncio.to_thread(graph.connect, to, offer_sdp)
    if not result.ok:
        await session.close()
        if result.ambiguous:
            return f"orden de llamada ambigua ({result.reason}); no la repito sola"
        return f"Meta rechazó la llamada ({result.reason})"
    call_id = result.call_id or f"out:{to}:{int(time.time())}"
    if not state.claim(call_id, to, "out"):
        # Rarísimo (id repetido): mejor no duplicar nada.
        await session.close()
        return "identificador de llamada repetido"
    if not state.reserve_seconds(call_id, granted):
        await session.close()
        state.transition(call_id, "rejected", ("claimed",),
                         last_error="cupo diario agotado", ended_at=time.time())
        await asyncio.to_thread(graph.terminate, call_id)
        return "cupo diario agotado"
    state.transition(call_id, "accepting", ("claimed",))
    _sessions[call_id] = session
    _outbound_pending[call_id] = session
    logger.info("LLAMADA saliente a %s en camino (%s).", to, call_id)
    return None


async def _run_outbound_answer(event: CallEvent, session) -> None:
    """La persona descolgó nuestra llamada: conectar audio y conversar."""
    from src.calls import graph, voice_loop

    call_id = event.call_id
    _outbound_pending.pop(call_id, None)
    answered_at = None
    try:
        if not await session.complete_outbound(event.sdp):
            raise RuntimeError("no pude conectar la respuesta de audio")
        answered_at = time.time()
        state.transition(call_id, "active", ("accepting",), answered_at=answered_at)
        row = state.get(call_id)
        granted = row["reserved_seconds"] if row else config.call_max_minutes() * 60
        logger.info("LLAMADA %s: contestada (concedidos %d s).", call_id, granted)
        end_reason = await voice_loop.converse(
            session, event.caller or (row["wa_id"] if row else ""), call_id,
            answered_at + granted, STOP_EVENT,
        )
        logger.info("LLAMADA %s: terminó (%s).", call_id, end_reason)
    except Exception as exc:
        logger.exception("LLAMADA %s: fallo inesperado; la cierro.", call_id)
        state.transition(call_id, "failed",
                         ("claimed", "accepting", "active", "ending"),
                         last_error=f"{exc.__class__.__name__}: {exc}",
                         ended_at=time.time())
        await asyncio.to_thread(graph.terminate, call_id)
    finally:
        if answered_at is not None:
            end_at = time.time()
            state.transition(call_id, "ended", ("active", "ending"), ended_at=end_at)
            state.reconcile_seconds(call_id, int(end_at - answered_at))
        try:
            await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("LLAMADA %s: el cierre del audio no terminó a tiempo.", call_id)
        if answered_at is not None:
            await asyncio.to_thread(graph.terminate, call_id)


async def _best_effort_reject(call_id: str) -> None:
    from src.calls import graph

    real_id = call_id.split(":recover")[0]
    await asyncio.to_thread(graph.reject, real_id)


# ---------------------------------------------------------------------------
# Arranque y apagado
# ---------------------------------------------------------------------------


def boot_sweep() -> None:
    """Cierra con motivo explícito las llamadas que un reinicio dejó a medias."""
    from src.calls import graph

    for row in state.orphans():
        call_id = row["call_id"]
        if not state.recover(call_id, "reinicio", require_lease=False):
            continue  # otro (un reaviso simultáneo) ganó la misma transición
        logger.warning(
            "LLAMADA %s: quedó a medias por un reinicio (%s); la cierro.",
            call_id, row["state"],
        )
        if row["state"] == "active":
            graph.terminate(call_id)
        else:
            graph.reject(call_id)


async def shutdown(timeout: float = 20.0) -> None:
    """Despide y cuelga las llamadas vivas; espera a que sus tareas terminen."""
    STOP_EVENT.set()
    tasks = list(_tasks.values())
    if not tasks:
        return
    logger.info("Apagando el teléfono: cuelgo %d llamada(s) en curso.", len(tasks))
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        logger.warning("Alguna llamada no terminó a tiempo durante el apagado.")
