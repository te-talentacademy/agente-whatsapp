"""El encargado del teléfono: decide qué llamada entra, la atiende y cuelga.

Contratos que este módulo garantiza (y los tests vigilan):

- CANDADOS EN ORDEN, del más barato al más caro: interruptor, lista de
  permitidos, una-llamada-a-la-vez, credenciales, cupos del día. El primero
  que falla rechaza la llamada con un motivo claro; nada se gasta después
  de un candado cerrado.
- RESERVA ANTES QUE META: los segundos del día se apartan y quedan escritos
  en la memoria ANTES de mandar el accept (entrante) o el connect
  (saliente). Un rechazo inequívoco de Meta devuelve la reserva; un fallo
  ambiguo (red, timeout) la CONSERVA — contar de más es el lado seguro.
- CADA TAREA TIENE DUEÑO: la tarea de una llamada queda registrada aquí con
  referencia fuerte antes de soltar el timbre, envuelta en una frontera
  total de fallo; el registro jamás pisa una tarea viva. Una llamada
  saliente es UNA sola tarea de principio a fin: admisión, connect, espera
  del descuelgue, conversación y cierre — la respuesta de la persona entra
  por una transición guardada, nunca crea otra tarea.
- NADA SE ABANDONA: al arrancar, las llamadas que un reinicio dejó a medias
  se cierran con motivo `reinicio`; un aviso repetido sobre una llamada
  huérfana la cierra con motivo `lease-vencido`. Ambos compiten por la
  misma transición y solo uno actúa (ver state.recover).
"""

import asyncio
import logging
import time
import uuid

from src import config
from src.calls import numbers, state
from src.webhook.parse import CallEvent

logger = logging.getLogger("agente")

STOP_EVENT = asyncio.Event()
ACCEPT_DEADLINE_SECONDS = 25.0   # margen propio dentro de la ventana de Meta
CLOSE_TIMEOUT_SECONDS = 6.0      # cerrar el audio jamás retrasa la contabilidad
RING_TIMEOUT_SECONDS = 60.0      # una saliente que nadie descuelga se cierra sola

_sessions: dict[str, object] = {}   # call_id local -> MediaSession (llamadas vivas)
_tasks: dict[str, asyncio.Task] = {}
_remote_to_local: dict[str, str] = {}   # id de Meta de una saliente -> id local


class _Outbound:
    """Lo que la tarea de una saliente espera de la persona: su respuesta."""

    def __init__(self) -> None:
        self.answer_event = asyncio.Event()
        self.answer_sdp = ""


_outbound: dict[str, _Outbound] = {}


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


def _register_task(call_id: str, coro) -> bool:
    """Crea la tarea con dueño: referencia fuerte + limpieza. Jamás pisa una
    tarea viva con la misma clave (devuelve False y descarta la corrutina)."""
    live = _tasks.get(call_id)
    if live is not None and not live.done():
        coro.close()
        logger.warning("LLAMADA %s: ya hay una tarea viva; ignoro el duplicado.", call_id)
        return False
    task = asyncio.create_task(coro)
    _tasks[call_id] = task

    def _done(t: asyncio.Task) -> None:
        if _tasks.get(call_id) is t:
            _tasks.pop(call_id, None)
        _sessions.pop(call_id, None)
        _outbound.pop(call_id, None)
        for remote, local in list(_remote_to_local.items()):
            if local == call_id:
                _remote_to_local.pop(remote, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Tarea del teléfono %s terminó con error: %s", call_id, exc,
                exc_info=exc,
            )

    task.add_done_callback(_done)
    return True


def _local_id_for(remote_id: str) -> str | None:
    """El id local de una saliente a partir del id de Meta (memoria, luego BD)."""
    local = _remote_to_local.get(remote_id)
    if local is not None:
        return local
    row = state.by_remote_id(remote_id)
    return row["call_id"] if row is not None else None


def _on_connect(event: CallEvent) -> None:
    # --- Respuesta de la persona a una saliente NUESTRA ---------------------
    # Entra por transición guardada: la tarea dueña de la saliente sigue con
    # ella; un reenvío pierde la transición y no hace nada (M1). Jamás crea
    # otra tarea.
    local_id = _local_id_for(event.call_id)
    if local_id is not None or event.sdp_type.lower() == "answer":
        if local_id is None:
            return  # respuesta a una saliente que ya no existe: nada que hacer
        runtime = _outbound.get(local_id)
        if runtime is None:
            # Huérfana de un reinicio: cerrar con motivo explícito, una sola vez.
            if state.recover(local_id, "lease-vencido", require_lease=True):
                logger.warning("LLAMADA %s: saliente a medias (lease vencido); la cierro.", local_id)
                _register_task(local_id + ":recover", _best_effort_terminate(event.call_id))
            return
        if not event.sdp:
            return
        if state.transition(local_id, "active", ("accepting",), answered_at=time.time()):
            runtime.answer_sdp = event.sdp
            runtime.answer_event.set()
        return

    # --- Llamada entrante ---------------------------------------------------
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
    call_id = _local_id_for(event.call_id) or event.call_id
    ended = state.transition(
        call_id, "ended", ("claimed", "accepting", "active", "ending"),
        ended_at=time.time(),
    )
    if not ended:
        return  # reenvío: la llamada ya estaba cerrada (limpieza no-op)
    session = _sessions.get(call_id)
    if session is not None:
        session.closed.set()
    runtime = _outbound.get(call_id)
    if runtime is not None and not runtime.answer_event.is_set():
        runtime.answer_sdp = ""
        runtime.answer_event.set()  # colgaron antes de descolgar: la tarea despierta
    logger.info("LLAMADA %s: la otra parte colgó.", call_id)


def _on_permission_reply(event: CallEvent) -> None:
    from src.calls import permissions

    # Tarea con dueño (misma frontera M2): el timbre no espera a Meta.
    _register_task(
        f"perm:{event.caller}:{event.wamid or int(time.time())}",
        permissions.handle_reply(event),
    )


# ---------------------------------------------------------------------------
# Candados
# ---------------------------------------------------------------------------


def _gate_reason(caller: str, busy: int, check_allowlist: bool = True) -> str | None:
    """La cadena de candados, del más barato al más caro. None = pasa.

    `busy` = cuántas OTRAS llamadas ocupan línea ahora mismo."""
    if not config.calls_enabled():
        return "FEATURE_CALLS está apagado"
    if check_allowlist:
        allowed = config.call_allowed_numbers()
        if allowed and numbers.canonical(caller) not in {numbers.canonical(n) for n in allowed}:
            return "número fuera de la lista de permitidos"
    if busy >= config.CALL_MAX_CONCURRENT:
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


# ---------------------------------------------------------------------------
# Llamada entrante
# ---------------------------------------------------------------------------


async def _run_inbound(event: CallEvent) -> None:
    """La vida completa de una llamada entrante, con frontera total de fallo."""
    from src.calls import graph, relay, voice_loop, webrtc

    call_id = event.call_id
    session = None
    granted = 0
    answered_at = None
    try:
        reason = _gate_reason(event.caller, busy=active_calls() - 1)
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
# Llamada saliente (la ordena permissions.py con permiso vigente)
# ---------------------------------------------------------------------------


async def start_outbound(to: str) -> str | None:
    """Admite una saliente: reclama, aparta el cupo y agenda su ÚNICA tarea.

    Todo lo de aquí es local e inmediato (sin red): cuando devuelve None, la
    llamada ya ocupa línea y su cupo ya está apartado en la memoria — el
    connect a Meta lo hace la tarea después. Texto = motivo del no.
    """
    if not config.outbound_calls_enabled():
        return "FEATURE_OUTBOUND_CALLS está apagado"
    reason = _gate_reason(to, busy=active_calls(), check_allowlist=False)
    if reason is not None:
        return reason
    if not state.permission_valid(to):
        return "sin permiso vigente de esa persona"

    local_id = f"out:{numbers.canonical(to)}:{uuid.uuid4().hex[:12]}"
    if not state.claim(local_id, to, "out"):
        return "identificador de llamada repetido"
    granted = _granted_seconds()
    if not state.reserve_seconds(local_id, granted):
        state.transition(local_id, "rejected", ("claimed",),
                         last_error="cupo diario agotado", ended_at=time.time())
        return "cupo diario agotado"
    _outbound[local_id] = _Outbound()
    if not _register_task(local_id, _run_outbound(local_id, to, granted)):
        state.refund_full_reserve(local_id)
        state.transition(local_id, "failed", ("claimed",),
                         last_error="tarea duplicada", ended_at=time.time())
        return "identificador de llamada repetido"
    return None


async def _run_outbound(local_id: str, to: str, granted: int) -> None:
    """La vida completa de una saliente: connect → descuelgue → conversación."""
    from src.calls import graph, permissions, relay, voice_loop, webrtc

    runtime = _outbound[local_id]
    session = None
    remote_id = ""
    answered_at = None

    async def tell_owner(text: str) -> None:
        await asyncio.to_thread(permissions._notify_owner, text)

    try:
        state.transition(local_id, "accepting", ("claimed",))
        ttl = granted + config.TURN_TTL_MARGIN_SECONDS
        relay_result = await asyncio.to_thread(relay.fetch, ttl)
        if not relay_result.ok:
            state.refund_full_reserve(local_id)
            state.transition(local_id, "failed", ("accepting",),
                             last_error=relay_result.reason, ended_at=time.time())
            await tell_owner(f"No pude llamar a +{numbers.canonical(to)}: {relay_result.reason}.")
            return
        session = webrtc.MediaSession()
        _sessions[local_id] = session
        offer_sdp = await session.offer_outbound(relay_result)
        result = await asyncio.to_thread(graph.connect, to, offer_sdp)
        if not result.ok:
            if result.ambiguous:
                # Meta pudo haber recibido la orden: la reserva SE CONSERVA.
                logger.error("LLAMADA %s: connect AMBIGUO (%s); conservo la reserva.",
                             local_id, result.reason)
                state.transition(local_id, "failed", ("accepting",),
                                 last_error=f"connect ambiguo: {result.reason}",
                                 ended_at=time.time())
                await tell_owner(f"No estoy seguro de que la llamada a +{numbers.canonical(to)} "
                                 f"haya salido ({result.reason}); no la repito sola.")
            else:
                state.refund_full_reserve(local_id)
                state.transition(local_id, "failed", ("accepting",),
                                 last_error=f"connect rechazado: {result.reason}",
                                 ended_at=time.time())
                await tell_owner(f"Meta rechazó la llamada a +{numbers.canonical(to)} ({result.reason}).")
            return
        remote_id = result.call_id
        if not remote_id:
            # Sin identificador no hay forma de correlacionar ni colgar: se
            # cierra con motivo y la reserva se conserva (Meta pudo marcar).
            logger.error("LLAMADA %s: Meta no devolvió identificador de llamada.", local_id)
            state.transition(local_id, "failed", ("accepting",),
                             last_error="connect sin identificador", ended_at=time.time())
            await tell_owner(f"Marqué a +{numbers.canonical(to)} pero Meta no confirmó la llamada.")
            return
        state.set_remote_id(local_id, remote_id)
        _remote_to_local[remote_id] = local_id
        logger.info("LLAMADA saliente a %s en camino (%s / %s).", to, local_id, remote_id)

        # Esperar el descuelgue (o el colgado, o el tope de timbre).
        try:
            await asyncio.wait_for(runtime.answer_event.wait(), RING_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.info("LLAMADA %s: nadie descolgó; la cancelo.", local_id)
            state.transition(local_id, "failed", ("accepting",),
                             last_error="sin respuesta", ended_at=time.time())
            await asyncio.to_thread(graph.terminate, remote_id)
            state.refund_full_reserve(local_id)  # jamás se conectó audio
            await tell_owner(f"+{numbers.canonical(to)} no contestó la llamada.")
            return
        if not runtime.answer_sdp:
            # Colgaron antes de descolgar (terminate): nada se conectó.
            state.refund_full_reserve(local_id)
            await tell_owner(f"+{numbers.canonical(to)} colgó antes de contestar.")
            return
        if not await session.complete_outbound(runtime.answer_sdp):
            raise RuntimeError("no pude conectar la respuesta de audio")
        row = state.get(local_id)
        answered_at = (row["answered_at"] if row and row["answered_at"] else time.time())
        logger.info("LLAMADA %s: contestada (concedidos %d s).", local_id, granted)
        await tell_owner(f"Llamada conectada con +{numbers.canonical(to)}.")
        end_reason = await voice_loop.converse(
            session, to, local_id, answered_at + granted, STOP_EVENT
        )
        logger.info("LLAMADA %s: terminó (%s).", local_id, end_reason)
    except Exception as exc:
        logger.exception("LLAMADA %s: fallo inesperado; la cierro.", local_id)
        state.transition(local_id, "failed",
                         ("claimed", "accepting", "active", "ending"),
                         last_error=f"{exc.__class__.__name__}: {exc}",
                         ended_at=time.time())
        if remote_id:
            await asyncio.to_thread(graph.terminate, remote_id)
    finally:
        if answered_at is not None:
            end_at = time.time()
            state.transition(local_id, "ended", ("active", "ending"), ended_at=end_at)
            state.reconcile_seconds(local_id, int(end_at - answered_at))
        if session is not None:
            try:
                await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("LLAMADA %s: el cierre del audio no terminó a tiempo.", local_id)
        if answered_at is not None and remote_id:
            await asyncio.to_thread(graph.terminate, remote_id)


async def _best_effort_reject(call_id: str) -> None:
    from src.calls import graph

    await asyncio.to_thread(graph.reject, call_id)


async def _best_effort_terminate(remote_id: str) -> None:
    from src.calls import graph

    await asyncio.to_thread(graph.terminate, remote_id)


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
        meta_id = row["remote_id"] or call_id
        if row["direction"] == "out":
            if row["remote_id"]:
                graph.terminate(meta_id)
        elif row["state"] == "active":
            graph.terminate(meta_id)
        else:
            graph.reject(meta_id)


async def shutdown(timeout: float = 20.0) -> None:
    """Despide y cuelga las llamadas vivas; espera a que sus tareas terminen."""
    STOP_EVENT.set()
    for runtime in _outbound.values():
        if not runtime.answer_event.is_set():
            runtime.answer_event.set()  # las salientes sin descolgar se cancelan
    tasks = list(_tasks.values())
    if not tasks:
        return
    logger.info("Apagando el teléfono: cuelgo %d llamada(s) en curso.", len(tasks))
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
    except asyncio.TimeoutError:
        logger.warning("Alguna llamada no terminó a tiempo durante el apagado.")
