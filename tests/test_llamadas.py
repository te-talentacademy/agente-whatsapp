"""El teléfono (F4): parser, máquina de llamadas, cupos, permisos y lazo de voz.

Los contratos que estas pruebas vigilan, en el idioma de la auditoría:
- Autoridad exacta por número propio en cada aviso (M3).
- Idempotencia con reclamo atómico: connect/terminate/permiso duplicados y
  timeouts ambiguos jamás repiten side effects (M1).
- Recuperación tras interrupción: nada no-terminal se descarta en silencio;
  barrido y reaviso compiten por la misma transición (M1 ronda 2).
- Tareas con dueño y frontera total de fallo (M2).
- Reserva del cupo diario CONFIRMADA antes del accept; ambiguo conserva,
  solo el rechazo inequívoco devuelve (M4 + condiciones ronda 3).
"""

import asyncio
import json
import math
import struct
import time

from src import config, db
from src.calls import manager, numbers, permissions, state, voice_loop, webrtc
from src.webhook.parse import extract_call_events, extract_messages
from tests.conftest import PNID, SECRET, sign

OFFER_SDP = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"


def calls_payload(event="connect", call_id="wacid.PRUEBA-1", caller="5215587654321",
                  pnid=PNID, sdp=OFFER_SDP):
    call = {"id": call_id, "from": caller, "event": event, "timestamp": "1"}
    if event == "connect":
        call["session"] = {"sdp_type": "offer", "sdp": sdp}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": pnid},
            "calls": [call],
        }, "field": "calls"}]}],
    }


def permission_reply_payload(sender="5215587654321", response="accept",
                             wamid="wamid.PERM-1", pnid=PNID, permanent=False,
                             expiration=None):
    reply = {"response": response, "is_permanent": permanent}
    if expiration is not None:
        reply["expiration_timestamp"] = expiration
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": pnid},
            "messages": [{"from": sender, "id": wamid, "timestamp": "1",
                          "type": "interactive",
                          "interactive": {"type": "call_permission_reply",
                                          "call_permission_reply": reply}}],
        }, "field": "messages"}]}]}


def run(coro):
    return asyncio.run(coro)


async def _drain_tasks():
    while manager._tasks:
        await asyncio.gather(*list(manager._tasks.values()), return_exceptions=True)


def _reset_phone(monkeypatch=None):
    manager._tasks.clear()
    manager._sessions.clear()
    manager._outbound.clear()
    manager._remote_to_local.clear()
    manager.STOP_EVENT = asyncio.Event()


# ---------------------------------------------------------------------------
# Parser: autoridad, formas y hostilidad
# ---------------------------------------------------------------------------


def test_parser_connect_y_terminate():
    events = extract_call_events(calls_payload("connect"), PNID)
    assert len(events) == 1
    assert events[0].kind == "connect"
    assert events[0].call_id == "wacid.PRUEBA-1"
    assert events[0].caller == "5215587654321"
    assert events[0].sdp == OFFER_SDP
    events = extract_call_events(calls_payload("terminate"), PNID)
    assert events[0].kind == "terminate"


def test_parser_permission_reply():
    events = extract_call_events(
        permission_reply_payload(permanent=True, expiration="1767225600"), PNID
    )
    assert len(events) == 1
    event = events[0]
    assert event.kind == "permission_reply"
    assert event.response == "accept"
    assert event.is_permanent is True
    assert event.expiration_timestamp == "1767225600"
    assert event.wamid == "wamid.PERM-1"


def test_parser_autoridad_exacta_por_numero_propio():
    """M3: un sobre que mezcla número propio y ajeno solo entrega lo propio."""
    payload = calls_payload()
    ajena = calls_payload(call_id="wacid.AJENA", pnid="999888777")["entry"][0]
    payload["entry"].append(ajena)
    events = extract_call_events(payload, PNID)
    assert [e.call_id for e in events] == ["wacid.PRUEBA-1"]


def test_parser_sobre_hostil_no_lanza():
    hostiles = [
        None, [], {"object": "otra_cosa"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "calls": [None, 42, {"id": None}, {"id": "x", "event": "??"},
                      {"event": "connect"}, {"id": "y", "event": "connect",
                       "session": "no-un-dict"}],
            "messages": [{"interactive": {"call_permission_reply": {"response": "accept"}}}],
        }}]}]},
    ]
    for payload in hostiles:
        events = extract_call_events(payload, PNID)
        # El único aceptable del último sobre: connect "y" con sdp vacío.
        assert all(e.call_id or e.kind == "permission_reply" for e in events)


# ---------------------------------------------------------------------------
# Números canónicos
# ---------------------------------------------------------------------------


def test_numeros_canonicos():
    assert numbers.canonical("+52 55 1234-5678") == "525512345678"
    assert numbers.same("+52 (55) 1234 5678", "525512345678")
    assert not numbers.same("", "")
    assert not numbers.same("5255", "5256")


# ---------------------------------------------------------------------------
# Máquina de llamadas (M1): reclamo, transiciones, recuperación
# ---------------------------------------------------------------------------


def test_reclamo_gana_una_sola_vez():
    db.connect()
    assert state.claim("wacid.A", "521", "in") is True
    assert state.claim("wacid.A", "521", "in") is False  # reenvío de Meta


def test_transicion_guardada_es_idempotente():
    db.connect()
    state.claim("wacid.B", "521", "in")
    assert state.transition("wacid.B", "ended", state.ACTIVE_STATES, ended_at=1.0)
    # Terminate duplicado: limpieza no-op.
    assert not state.transition("wacid.B", "ended", state.ACTIVE_STATES, ended_at=2.0)


def test_recuperacion_compite_por_la_misma_transicion():
    """Barrido y reaviso: solo UNO gana el cierre de una huérfana."""
    db.connect()
    state.claim("wacid.C", "521", "in")
    first = state.recover("wacid.C", "reinicio", require_lease=False)
    second = state.recover("wacid.C", "lease-vencido", require_lease=False)
    assert first is True and second is False
    row = state.get("wacid.C")
    assert row["state"] == "failed"
    assert "reinicio" in row["last_error"]


def test_lease_protege_reclamos_recientes(monkeypatch):
    db.connect()
    state.claim("wacid.D", "521", "in")
    # Recién reclamada: el lease aún la protege.
    assert state.recover("wacid.D", "lease-vencido", require_lease=True) is False
    real = time.time
    monkeypatch.setattr(state.time, "time", lambda: real() + config.CALL_CLAIM_LEASE_SECONDS + 5)
    assert state.recover("wacid.D", "lease-vencido", require_lease=True) is True


# ---------------------------------------------------------------------------
# Cupo diario de minutos (M4)
# ---------------------------------------------------------------------------


def test_reserva_confirma_antes_y_concilia_despues(monkeypatch):
    db.connect()
    monkeypatch.setenv("CALL_DAILY_MINUTES_LIMIT", "10")
    state.claim("wacid.E", "521", "in")
    assert state.daily_seconds_remaining() == 600
    assert state.reserve_seconds("wacid.E", 300) is True
    assert state.daily_seconds_remaining() == 300  # confirmada en la memoria
    state.reconcile_seconds("wacid.E", 60)         # habló 1 minuto
    assert state.daily_seconds_remaining() == 540
    assert state.get("wacid.E")["seconds"] == 60


def test_muerte_abrupta_no_regala_cupo(monkeypatch):
    """Sin conciliación (proceso muerto), lo apartado queda contado."""
    db.connect()
    monkeypatch.setenv("CALL_DAILY_MINUTES_LIMIT", "5")
    state.claim("wacid.F", "521", "in")
    assert state.reserve_seconds("wacid.F", 300) is True
    # El proceso muere aquí: nada se concilia. El día quedó consumido.
    assert state.daily_seconds_remaining() == 0
    state.claim("wacid.G", "521", "in")
    assert state.reserve_seconds("wacid.G", 60) is False


def test_devolucion_completa_solo_rechazo_inequivoco(monkeypatch):
    db.connect()
    monkeypatch.setenv("CALL_DAILY_MINUTES_LIMIT", "5")
    state.claim("wacid.H", "521", "in")
    state.reserve_seconds("wacid.H", 300)
    state.refund_full_reserve("wacid.H")
    assert state.daily_seconds_remaining() == 300


# ---------------------------------------------------------------------------
# Permisos de saliente
# ---------------------------------------------------------------------------


def test_limites_locales_de_solicitudes(monkeypatch):
    db.connect()
    ok, _ = state.reserve_request("52155511111")
    assert ok
    ok, reason = state.reserve_request("52155511111")
    assert not ok and "hoy" in reason
    # Pasan 25 h: el límite diario libera, el semanal aún permite la segunda.
    real = time.time
    monkeypatch.setattr(state.time, "time", lambda: real() + 25 * 3600)
    ok, _ = state.reserve_request("52155511111")
    assert ok
    monkeypatch.setattr(state.time, "time", lambda: real() + 50 * 3600)
    ok, reason = state.reserve_request("52155511111")
    assert not ok and "semana" in reason


def test_respuesta_de_permiso_transiciona_una_sola_vez():
    db.connect()
    state.reserve_request("52155522222")
    assert state.apply_permission_reply("52155522222", "accept", False,
                                        time.time() + 3600) == "approved"
    # Reenvío del mismo aviso: no dispara nada.
    assert state.apply_permission_reply("52155522222", "accept", False,
                                        time.time() + 3600) == "stale"
    assert state.permission_valid("52155522222") is True


def test_permiso_denegado_y_caducado():
    db.connect()
    state.reserve_request("52155533333")
    assert state.apply_permission_reply("52155533333", "reject", False, None) == "denied"
    assert state.permission_valid("52155533333") is False
    state.reserve_request("52155544444")
    state.apply_permission_reply("52155544444", "accept", False, time.time() - 10)
    assert state.permission_valid("52155544444") is False  # caducado
    state.reserve_request("52155555555")
    state.apply_permission_reply("52155555555", "accept", True, None)
    assert state.permission_valid("52155555555") is True   # permanente


# ---------------------------------------------------------------------------
# El encargado (manager): candados, orden reserva→accept, M2
# ---------------------------------------------------------------------------


def _fake_graph(monkeypatch, accept_result=None, on_accept=None):
    """Sustituye las acciones de Meta y registra cada una en orden."""
    from src.calls import graph

    log: list[str] = []

    def action(name, ok=True, ambiguous=False):
        def _do(*args, **kwargs):
            log.append(name)
            if name == "accept":
                if on_accept is not None:
                    on_accept()
                if accept_result is not None:
                    return accept_result
            return graph.CallActionResult(ok=ok, ambiguous=ambiguous)
        return _do

    monkeypatch.setattr(graph, "pre_accept", action("pre_accept"))
    monkeypatch.setattr(graph, "accept", action("accept"))
    monkeypatch.setattr(graph, "reject", action("reject"))
    monkeypatch.setattr(graph, "terminate", action("terminate"))
    return log


def _fake_relay(monkeypatch, ok=True):
    from src.calls import relay

    def fetch(ttl):
        return relay.RelayResult(ok=ok, username="u", credential="c",
                                 reason="" if ok else "el relevo rechazó la credencial (401): revisa las llaves TURN")
    monkeypatch.setattr(relay, "fetch", fetch)


def _fake_session(monkeypatch, end_reason="tope-llamada"):
    class FakeSession:
        def __init__(self):
            self.closed = asyncio.Event()
            self.hear_queue = asyncio.Queue()

        async def answer_inbound(self, sdp, relay_result):
            return "v=0\r\n(answer)"

        async def close(self):
            pass

    monkeypatch.setattr(webrtc, "MediaSession", FakeSession)

    async def fake_converse(session, caller, call_id, deadline_at, stop_event):
        return end_reason

    monkeypatch.setattr(voice_loop, "converse", fake_converse)
    return FakeSession


def _calls_env(monkeypatch, **extra):
    monkeypatch.setenv("FEATURE_CALLS", "on")
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voz-falsa")
    monkeypatch.setenv("CLOUDFLARE_TURN_KEY_ID", "kid")
    monkeypatch.setenv("CLOUDFLARE_TURN_API_TOKEN", "tok")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_candado_sin_llaves_turn_rechaza_sin_gastar(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    monkeypatch.delenv("CLOUDFLARE_TURN_KEY_ID", raising=False)
    log = _fake_graph(monkeypatch)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert log == ["reject"]
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "rejected"
    assert "sin-relay-vigente" in row["last_error"]
    assert row["reserved_seconds"] == 0  # el candado cerró antes de apartar nada


def test_candado_lista_de_permitidos(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_ALLOWED_NUMBERS="+52111, +52222")
    log = _fake_graph(monkeypatch)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert log == ["reject"]
    assert "permitidos" in state.get("wacid.PRUEBA-1")["last_error"]


def test_reserva_confirmada_antes_del_accept(monkeypatch):
    """Condición ronda 3: cuando Meta recibe el accept, el cupo YA está contado."""
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    seen = {}

    def on_accept():
        seen["remaining_at_accept"] = state.daily_seconds_remaining()

    log = _fake_graph(monkeypatch, on_accept=on_accept)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert "accept" in log
    # 10 min de cupo - 5 min concedidos = 300 s ya descontados al aceptar.
    assert seen["remaining_at_accept"] == 300
    assert state.get("wacid.PRUEBA-1")["state"] == "ended"


def test_accept_ambiguo_conserva_la_reserva(monkeypatch):
    from src.calls import graph

    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    _fake_graph(monkeypatch,
                accept_result=graph.CallActionResult(ok=False, ambiguous=True, reason="red: Timeout"))

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "failed" and "ambiguo" in row["last_error"]
    assert state.daily_seconds_remaining() == 300  # la reserva NO se devolvió


def test_accept_rechazado_inequivoco_devuelve_la_reserva(monkeypatch):
    from src.calls import graph

    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    _fake_graph(monkeypatch,
                accept_result=graph.CallActionResult(ok=False, reason="rechazado (401): token"))

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert state.get("wacid.PRUEBA-1")["state"] == "failed"
    assert state.daily_seconds_remaining() == 600  # devuelta completa


def test_relevo_caido_rechaza_y_devuelve(monkeypatch):
    """El fallo simulado del runbook: sin relevo vigente no hay llamada."""
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch, ok=False)
    log = _fake_graph(monkeypatch)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert log == ["reject"]
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "rejected" and "relevo" in row["last_error"]
    assert state.daily_seconds_remaining() == 600


def test_connect_duplicado_en_vuelo_no_repite_nada(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    log = _fake_graph(monkeypatch)

    async def scenario():
        events = extract_call_events(calls_payload(), PNID)
        await manager.dispatch_events(events)
        await manager.dispatch_events(events)  # reenvío inmediato de Meta
        await _drain_tasks()

    run(scenario())
    assert log.count("accept") == 1
    assert log.count("reject") == 0


def test_huerfana_por_reinicio_se_cierra_con_motivo(monkeypatch):
    """M1 ronda 2: reinicio durante claimed/accepting/active, uno por uno."""
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    log = _fake_graph(monkeypatch)
    state.claim("wacid.R1", "521", "in")                       # claimed
    state.claim("wacid.R2", "521", "in")
    state.transition("wacid.R2", "accepting", ("claimed",))    # accepting
    state.claim("wacid.R3", "521", "in")
    state.transition("wacid.R3", "accepting", ("claimed",))
    state.transition("wacid.R3", "active", ("accepting",), answered_at=time.time())

    manager.boot_sweep()
    for call_id in ("wacid.R1", "wacid.R2", "wacid.R3"):
        row = state.get(call_id)
        assert row["state"] == "failed"
        assert "reinicio" in row["last_error"]
    assert log.count("reject") == 2      # claimed + accepting
    assert log.count("terminate") == 1   # active
    # Segundo barrido: todo terminal, no-op.
    log.clear()
    manager.boot_sweep()
    assert log == []


def test_reaviso_sobre_huerfana_recupera_una_sola_vez(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    log = _fake_graph(monkeypatch)
    state.claim("wacid.PRUEBA-1", "5215587654321", "in")
    # Caída entre el reclamo y la tarea; el lease ya venció.
    real = time.time
    monkeypatch.setattr(state.time, "time", lambda: real() + config.CALL_CLAIM_LEASE_SECONDS + 5)

    async def scenario():
        events = extract_call_events(calls_payload(), PNID)
        await manager.dispatch_events(events)
        await manager.dispatch_events(events)  # segundo reaviso: ya terminal
        await _drain_tasks()

    run(scenario())
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "failed" and "lease-vencido" in row["last_error"]
    assert log.count("reject") == 1  # una sola acción terminal


def test_tarea_con_dueno_sobrevive_al_timbre_y_captura_excepciones(monkeypatch):
    """M2: la tarea vive tras responder el timbre; una excepción produce
    limpieza + estado terminal, jamás una llamada colgada en el aire."""
    from src.calls import relay

    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    log = _fake_graph(monkeypatch)

    def explota(ttl):
        raise RuntimeError("fallo inesperado del relevo")

    monkeypatch.setattr(relay, "fetch", explota)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        assert manager._tasks  # la tarea quedó registrada con dueño
        await _drain_tasks()

    run(scenario())
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "failed" and "RuntimeError" in row["last_error"]
    assert log == ["reject"]
    assert not manager._tasks and not manager._sessions  # limpieza total


def test_ventana_agotada_rechaza_fail_closed(monkeypatch):
    """La cadena hasta el accept debe caber en la ventana; si no, rechazo."""
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    log = _fake_graph(monkeypatch)
    real = time.time
    offset = {"value": 0.0}
    monkeypatch.setattr(manager.time, "time", lambda: real() + offset["value"])

    from src.calls import graph
    original_pre = graph.pre_accept

    def slow_pre_accept(call_id, sdp):
        offset["value"] = manager.ACCEPT_DEADLINE_SECONDS + 5  # el reloj vuela
        return original_pre(call_id, sdp)

    monkeypatch.setattr(graph, "pre_accept", slow_pre_accept)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    assert "accept" not in log and "reject" in log
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "failed" and "ventana" in row["last_error"]
    assert state.daily_seconds_remaining() == 600  # nada conectado: devuelta


def test_llamada_completa_concilia_el_consumo(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_session(monkeypatch)
    _fake_graph(monkeypatch)

    async def scenario():
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()

    run(scenario())
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "ended"
    # La llamada simulada duró ~0 s: casi todo el concedido volvió al cupo.
    assert state.daily_seconds_remaining() >= 595


def test_terminate_de_meta_cierra_y_es_idempotente(monkeypatch):
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch)
    state.claim("wacid.T", "521", "in")
    state.transition("wacid.T", "active", ("claimed",), answered_at=time.time())

    async def scenario():
        events = extract_call_events(calls_payload("terminate", call_id="wacid.T"), PNID)
        await manager.dispatch_events(events)
        await manager.dispatch_events(events)  # reenvío: no-op

    run(scenario())
    assert state.get("wacid.T")["state"] == "ended"


# ---------------------------------------------------------------------------
# Kill-switch: el teléfono apagado no toca el texto
# ---------------------------------------------------------------------------


def test_apagado_no_carga_el_telefono_ni_toca_el_texto(client_factory=None):
    from fastapi.testclient import TestClient

    from src.main import app

    web = TestClient(app)
    db.connect()
    body = json.dumps(calls_payload()).encode()
    response = web.post("/", content=body, headers={"x-hub-signature-256": sign(body)})
    assert response.status_code == 200
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 0

    # Y el texto de F1 sigue intacto en el mismo servicio.
    from tests.conftest import meta_payload

    body = meta_payload("hola con el teléfono apagado", wamid="wamid.KS-1")
    response = web.post("/", content=body, headers={"x-hub-signature-256": sign(body)})
    assert response.status_code == 200 and response.json() == {"received": 1}


def test_respuesta_de_permiso_no_se_encola_como_conversacion(monkeypatch):
    from fastapi.testclient import TestClient

    from src.main import app

    db.connect()
    _reset_phone()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    captured = {}

    async def fake_dispatch(events):
        captured["events"] = events

    monkeypatch.setattr(manager, "dispatch_events", fake_dispatch)
    web = TestClient(app)
    body = json.dumps(permission_reply_payload()).encode()
    response = web.post("/", content=body, headers={"x-hub-signature-256": sign(body)})
    assert response.status_code == 200
    assert response.json() == {"received": 0}  # no entró a la fila de mensajes
    assert captured["events"][0].kind == "permission_reply"
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Comando del dueño
# ---------------------------------------------------------------------------


def test_comando_solo_del_dueno_y_con_interruptor(monkeypatch):
    db.connect()
    # Apagado: ni el dueño puede.
    assert permissions.handle_text_command("52155500001", "llamar +52111") is None
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "+52 155 500 0001")
    # Otro número no es el dueño.
    assert permissions.handle_text_command("52155599999", "llamar +52111") is None
    # Texto que no es el comando literal: sigue al cerebro.
    assert permissions.handle_text_command("52155500001", "hola, ¿me llamas?") is None


def test_comando_pide_permiso_y_confirma_estados(monkeypatch):
    from src.calls import graph

    db.connect()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "5215550000010")
    sent = []
    monkeypatch.setattr(graph, "send_permission_request",
                        lambda to: (sent.append(to) or graph.CallActionResult(ok=True)))
    reply = permissions.handle_text_command("5215550000010", "Llamar al +52 155 512 3456")
    assert sent == ["521555123456"]
    assert "Solicitud de permiso enviada" in reply
    # Segunda orden el mismo día: el límite local la frena ANTES de Meta.
    reply = permissions.handle_text_command("5215550000010", "llamar +52 155 512 3456")
    assert sent == ["521555123456"]  # no salió otra
    assert "hoy" in reply


def test_solicitud_ambigua_conserva_la_reserva(monkeypatch):
    from src.calls import graph

    db.connect()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "5215550000011")
    monkeypatch.setattr(graph, "send_permission_request",
                        lambda to: graph.CallActionResult(ok=False, ambiguous=True, reason="red: Timeout"))
    reply = permissions.handle_text_command("5215550000011", "llamar +52155600 0000")
    assert "No estoy seguro" in reply
    # La reserva local sobrevivió al timeout: no se puede repetir hoy.
    ok, reason = state.reserve_request("521556000000")
    assert not ok and "hoy" in reason


def test_permiso_vigente_llama_directo(monkeypatch):
    db.connect()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "5215550000012")
    state.reserve_request("521557000000")
    state.apply_permission_reply("521557000000", "accept", True, None)
    monkeypatch.setattr(permissions, "_submit_call", lambda target: None)
    reply = permissions.handle_text_command("5215550000012", "llamar +521557000000")
    assert "llamando" in reply.lower()


def test_respuesta_aceptada_marca_y_avisa(monkeypatch):
    db.connect()
    _reset_phone()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "5215550000013")
    state.reserve_request("521558000000")
    notified = []
    monkeypatch.setattr(permissions, "_notify_owner", lambda text: notified.append(text))
    started = []

    async def fake_start(to):
        started.append(to)
        return None

    monkeypatch.setattr(manager, "start_outbound", fake_start)
    events = extract_call_events(permission_reply_payload(sender="521558000000"), PNID)

    async def scenario():
        await manager.dispatch_events(events)
        await _drain_tasks()
        # Reenvío del mismo aviso: transición ya hecha -> nada nuevo.
        await manager.dispatch_events(events)
        await _drain_tasks()

    run(scenario())
    assert started == ["521558000000"]  # UN solo connect por permiso (M1)
    assert any("Permiso recibido" in text for text in notified)


# ---------------------------------------------------------------------------
# Lazo de voz: VAD, topes y despedidas
# ---------------------------------------------------------------------------


class FakeCallSession:
    def __init__(self):
        self.hear_queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.spoken: list[bytes] = []

    def speaking_seconds_left(self) -> float:
        return 0.0

    def speak(self, pcm: bytes) -> None:
        self.spoken.append(pcm)


def _tone(seconds: float, amplitude: int = 8000) -> bytes:
    total = int(webrtc.HEAR_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / webrtc.HEAR_RATE)))
        for i in range(total)
    )


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(webrtc.HEAR_RATE * seconds)


def test_vad_detecta_una_intervencion():
    session = FakeCallSession()
    for chunk in (_silence(0.2), _tone(1.0), _silence(1.0)):
        session.hear_queue.put_nowait(chunk)

    async def scenario():
        return await voice_loop._collect_utterance(
            session, time.time() + 30, asyncio.Event()
        )

    utterance = run(scenario())
    # Se capturó aproximadamente el tono (1 s ± los marcos de arranque/cierre).
    assert 0.7 * len(_tone(1.0)) < len(utterance) < 2.2 * len(_tone(1.0))


def test_vad_ignora_ruidos_cortos_y_corta_al_tope(monkeypatch):
    monkeypatch.setattr(config, "MAX_UTTERANCE_SECONDS", 1.0)
    session = FakeCallSession()
    session.hear_queue.put_nowait(_tone(0.06))   # un golpe, no una frase
    session.hear_queue.put_nowait(_silence(1.0))
    session.hear_queue.put_nowait(_tone(5.0))    # discurso sin pausa

    async def scenario():
        return await voice_loop._collect_utterance(
            session, time.time() + 30, asyncio.Event()
        )

    utterance = run(scenario())
    assert len(utterance) <= len(_tone(1.3))  # cortada al tope, no 5 s


def test_tope_de_llamada_y_apagado_terminan_con_motivo():
    session = FakeCallSession()

    async def past_deadline():
        try:
            await voice_loop._collect_utterance(session, time.time() - 1, asyncio.Event())
        except voice_loop._Ended as ended:
            return ended.reason

    assert run(past_deadline()) == "tope-llamada"

    async def stopped():
        stop = asyncio.Event()
        stop.set()
        try:
            await voice_loop._collect_utterance(session, time.time() + 30, stop)
        except voice_loop._Ended as ended:
            return ended.reason

    assert run(stopped()) == "apagado"


def test_colgado_del_otro_lado_termina():
    session = FakeCallSession()
    session.closed.set()

    async def scenario():
        try:
            await voice_loop._collect_utterance(session, time.time() + 30, asyncio.Event())
        except voice_loop._Ended as ended:
            return ended.reason

    assert run(scenario()) == "colgado"


def test_conversacion_completa_saluda_responde_y_despide(monkeypatch):
    """El lazo entero con oídos/cerebro/voz sustituidos: saludo, un turno
    grounded y despedida por tope, en ese orden."""
    from src.llm import brain
    from src.voice import cartesia

    db.connect()
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave")
    said: list[str] = []
    monkeypatch.setattr(
        cartesia, "synthesize",
        lambda text, output_format=None: (said.append(text) or cartesia.VoiceResult(ok=True, data=b"\x00\x00" * 240)),
    )
    monkeypatch.setattr(
        cartesia, "transcribe",
        lambda data, mime: cartesia.VoiceResult(ok=True, text="¿cuánto cuesta el pan?"),
    )
    monkeypatch.setattr(
        brain, "think",
        lambda sender, text, image_parts=None, overlay="", max_tokens=None: brain.Thought(text="El pan cuesta $45."),
    )
    monkeypatch.setattr(webrtc, "resample_to_speak", lambda pcm, rate: pcm)

    session = FakeCallSession()
    session.hear_queue.put_nowait(_tone(0.8))
    session.hear_queue.put_nowait(_silence(1.0))

    async def scenario():
        deadline = time.time() + 4.0
        return await voice_loop.converse(session, "521", "wacid.V", deadline, asyncio.Event())

    reason = run(scenario())
    assert reason == "tope-llamada"
    assert said[0] == config.call_greeting_text()
    assert "El pan cuesta $45." in said
    assert said[-1] == config.call_goodbye_text()
    assert len(session.spoken) == len(said)


def test_regresion_f3_mp3_por_defecto_byte_identico(monkeypatch):
    """El param PCM no cambia NI UN BYTE el payload por defecto de las notas."""
    from src.voice import cartesia

    monkeypatch.setenv("CARTESIA_API_KEY", "llave")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voz-123")
    captured = {}

    class _Resp:
        status_code = 200
        content = b"mp3-bytes"
        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(cartesia.httpx, "post", fake_post)
    result = cartesia.synthesize("hola")
    assert result.ok
    assert captured["json"] == {
        "model_id": "sonic-3.6",
        "transcript": "hola",
        "voice": "voz-123",
        "language": "es",
        "output_format": {"container": "mp3", "encoding": "mp3", "sample_rate": 44100},
    }
    # Y con el formato de llamada, SOLO cambia la envoltura de audio.
    cartesia.synthesize("hola", cartesia.PCM_FORMAT)
    assert captured["json"]["output_format"] == {
        "container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000,
    }


def test_cupo_de_voz_agotado_corta_el_lazo(monkeypatch):
    from src.voice import quota

    db.connect()
    monkeypatch.setattr(quota, "reserve", lambda: False)
    session = FakeCallSession()

    async def scenario():
        try:
            await voice_loop._transcribe(b"\x00\x00" * 1600)
        except voice_loop._Ended as ended:
            return ended.reason

    assert run(scenario()) == "cupo-voz"


# ---------------------------------------------------------------------------
# El conector WebRTC: SDP real sin red
# ---------------------------------------------------------------------------


def test_answer_sdp_real_sin_red(monkeypatch):
    """aiortc de verdad: una oferta real produce una respuesta con audio.
    Sin red: el conector se arma sin servidores de relevo para la prueba."""
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    from src.calls import relay

    async def scenario():
        caller = RTCPeerConnection()
        caller.addTrack(AudioStreamTrack())
        offer = await caller.createOffer()
        await caller.setLocalDescription(offer)

        session = webrtc.MediaSession()

        def bare_pc(relay_result):
            pc = RTCPeerConnection()
            pc.on("track", lambda track: None)
            session.pc = pc
            return pc

        monkeypatch.setattr(session, "_build_pc", bare_pc)
        answer = await session.answer_inbound(
            caller.localDescription.sdp,
            relay.RelayResult(ok=True, username="u", credential="c"),
        )
        await session.close()
        await caller.close()
        return answer

    answer = run(scenario())
    assert answer and "m=audio" in answer


def test_resample_de_la_voz_al_aire():
    pcm_24k = b"\x01\x00" * 2400  # 0.1 s a 24 kHz
    pcm_48k = webrtc.resample_to_speak(pcm_24k, 24000)
    # El doble de muestras (± bordes del filtro).
    assert abs(len(pcm_48k) - 2 * len(pcm_24k)) < 400


def test_cierre_colgado_no_bloquea_la_contabilidad(monkeypatch):
    """Si cerrar el audio se cuelga (el otro lado desapareció), la
    conciliación del cupo sale igual y la tarea termina."""
    db.connect()
    _reset_phone()
    _calls_env(monkeypatch, CALL_DAILY_MINUTES_LIMIT="10")
    _fake_relay(monkeypatch)
    _fake_graph(monkeypatch)
    monkeypatch.setattr(manager, "CLOSE_TIMEOUT_SECONDS", 0.2)

    class StuckSession:
        def __init__(self):
            self.closed = asyncio.Event()
            self.hear_queue = asyncio.Queue()

        async def answer_inbound(self, sdp, relay_result):
            return "v=0\r\n(answer)"

        async def close(self):
            await asyncio.sleep(60)  # jamás termina solo

    monkeypatch.setattr(webrtc, "MediaSession", StuckSession)

    async def fake_converse(session, caller, call_id, deadline_at, stop_event):
        return "colgado"

    monkeypatch.setattr(voice_loop, "converse", fake_converse)

    async def scenario():
        started = time.time()
        await manager.dispatch_events(extract_call_events(calls_payload(), PNID))
        await _drain_tasks()
        return time.time() - started

    elapsed = run(scenario())
    assert elapsed < 5.0  # el cierre colgado no retuvo la tarea
    row = state.get("wacid.PRUEBA-1")
    assert row["state"] == "ended"
    assert state.daily_seconds_remaining() >= 595  # conciliación hecha


# ---------------------------------------------------------------------------
# Saliente: admisión antes de Meta (M4), respuesta única (M1), integral
# ---------------------------------------------------------------------------


class _OutSession:
    """Sesión saliente sustituta: oferta fija, respuesta aceptada."""

    def __init__(self):
        self.closed = asyncio.Event()
        self.hear_queue = asyncio.Queue()
        self.completed = 0

    async def offer_outbound(self, relay_result):
        return "v=0\r\n(offer)"

    async def complete_outbound(self, answer_sdp):
        self.completed += 1
        return True

    async def close(self):
        pass


def _outbound_env(monkeypatch, **extra):
    _calls_env(monkeypatch, FEATURE_OUTBOUND_CALLS="on", CALL_OWNER_NUMBER="5215550000099",
               CALL_DAILY_MINUTES_LIMIT="10", **extra)
    _fake_relay(monkeypatch)
    monkeypatch.setattr(webrtc, "MediaSession", _OutSession)
    monkeypatch.setattr(permissions, "_notify_owner", lambda text: None)


def _grant_permission(wa_id):
    state.reserve_request(wa_id)
    state.apply_permission_reply(wa_id, "accept", True, None)


def _answer_payload(remote_id, sender, sdp="v=0\r\n(answer)"):
    payload = calls_payload(call_id=remote_id, caller=sender, sdp=sdp)
    payload["entry"][0]["changes"][0]["value"]["calls"][0]["session"]["sdp_type"] = "answer"
    return payload


def test_saliente_reserva_y_ocupa_linea_antes_del_connect(monkeypatch):
    """M4: cuando Meta recibe el connect, la fila, la reserva y la línea ya existen."""
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650001")
    seen = {}

    def fake_connect(to, sdp):
        with db.transaction() as conn:
            row = conn.execute("SELECT * FROM calls WHERE direction = 'out'").fetchone()
        seen["row_state"] = row["state"]
        seen["reserved"] = row["reserved_seconds"]
        seen["remaining"] = state.daily_seconds_remaining()
        seen["busy"] = manager.active_calls()
        return graph.CallActionResult(ok=True, call_id="wacid.META-OUT-1")

    monkeypatch.setattr(graph, "connect", fake_connect)
    monkeypatch.setattr(graph, "terminate", lambda cid: graph.CallActionResult(ok=True))
    monkeypatch.setattr(manager, "RING_TIMEOUT_SECONDS", 0.3)

    async def scenario():
        refusal = await manager.start_outbound("5215587650001")
        assert refusal is None
        await _drain_tasks()

    run(scenario())
    assert seen["row_state"] == "accepting"
    assert seen["reserved"] == 300           # 5 min concedidos ya apartados
    assert seen["remaining"] == 300          # y descontados del día
    assert seen["busy"] == 1                 # la línea ya está ocupada


def test_saliente_no_entra_con_otra_llamada_en_linea(monkeypatch):
    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650002")

    async def scenario():
        async def ocupa():
            await asyncio.sleep(0.5)
        manager._register_task("wacid.OCUPADA", ocupa())   # otra llamada viva
        refusal = await manager.start_outbound("5215587650002")
        await _drain_tasks()
        return refusal

    assert run(scenario()) == "ya hay una llamada en curso"
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 0


def test_respuesta_saliente_duplicada_no_crea_segunda_tarea(monkeypatch):
    """M1: dos respuestas SDP en el mismo ciclo → una tarea, un complete, una conciliación."""
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650003")
    log = []
    monkeypatch.setattr(graph, "connect", lambda to, sdp: (log.append("connect") or graph.CallActionResult(ok=True, call_id="wacid.META-OUT-3")))
    monkeypatch.setattr(graph, "terminate", lambda cid: (log.append("terminate") or graph.CallActionResult(ok=True)))
    converse_calls = []

    async def fake_converse(session, caller, call_id, deadline_at, stop_event):
        converse_calls.append(call_id)
        await asyncio.sleep(0.2)
        return "tope-llamada"

    monkeypatch.setattr(voice_loop, "converse", fake_converse)

    async def scenario():
        assert await manager.start_outbound("5215587650003") is None
        await asyncio.sleep(0.1)  # la tarea ya mandó el connect y espera
        events = extract_call_events(_answer_payload("wacid.META-OUT-3", "5215587650003"), PNID)
        await manager.dispatch_events(events)
        await manager.dispatch_events(events)  # reenvío en el mismo ciclo
        assert manager.active_calls() == 1
        await _drain_tasks()

    run(scenario())
    assert log.count("connect") == 1
    assert converse_calls and len(converse_calls) == 1
    row = state.by_remote_id("wacid.META-OUT-3")
    assert row["state"] == "ended" and row["reserved_seconds"] == 0
    assert state.daily_seconds_remaining() >= 595  # conciliada UNA vez


def test_saliente_integral_connect_respuesta_conversacion(monkeypatch):
    """Flujo real: start_outbound → connect → respuesta → conversación → cierre."""
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650004")
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=True, call_id="wacid.META-OUT-4"))
    ended = []
    monkeypatch.setattr(graph, "terminate", lambda cid: (ended.append(cid) or graph.CallActionResult(ok=True)))
    spoken_to = []

    async def fake_converse(session, caller, call_id, deadline_at, stop_event):
        spoken_to.append(caller)
        assert session.completed == 1
        return "colgado"

    monkeypatch.setattr(voice_loop, "converse", fake_converse)

    async def scenario():
        assert await manager.start_outbound("5215587650004") is None
        await asyncio.sleep(0.1)
        await manager.dispatch_events(
            extract_call_events(_answer_payload("wacid.META-OUT-4", "5215587650004"), PNID)
        )
        await _drain_tasks()

    run(scenario())
    assert spoken_to == ["5215587650004"]
    assert ended == ["wacid.META-OUT-4"]     # se cuelga con el id de Meta
    assert state.by_remote_id("wacid.META-OUT-4")["state"] == "ended"
    assert not manager._tasks and not manager._outbound and not manager._remote_to_local


def test_saliente_sin_respuesta_devuelve_la_reserva(monkeypatch):
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650005")
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=True, call_id="wacid.META-OUT-5"))
    monkeypatch.setattr(graph, "terminate", lambda cid: graph.CallActionResult(ok=True))
    monkeypatch.setattr(manager, "RING_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        assert await manager.start_outbound("5215587650005") is None
        await _drain_tasks()

    run(scenario())
    row = state.by_remote_id("wacid.META-OUT-5")
    assert row["state"] == "failed" and "sin respuesta" in row["last_error"]
    assert state.daily_seconds_remaining() == 600  # nunca hubo audio: devuelta


def test_connect_ambiguo_conserva_reserva_e_inequivoco_devuelve(monkeypatch):
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650006")
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=False, ambiguous=True, reason="red: Timeout"))

    async def scenario():
        assert await manager.start_outbound("5215587650006") is None
        await _drain_tasks()

    run(scenario())
    assert state.daily_seconds_remaining() == 300  # ambiguo: conservada
    _reset_phone()
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=False, reason="rechazado (400): permiso"))
    run(scenario())
    assert state.daily_seconds_remaining() == 300  # inequívoco: devuelta (no baja a 0)


def test_solicitud_rechazada_inequivoca_devuelve_la_reserva(monkeypatch):
    from src.calls import graph

    db.connect()
    monkeypatch.setenv("FEATURE_OUTBOUND_CALLS", "on")
    monkeypatch.setenv("CALL_OWNER_NUMBER", "5215550000014")
    monkeypatch.setattr(graph, "send_permission_request",
                        lambda to: graph.CallActionResult(ok=False, reason="rechazado (400): número inválido"))
    reply = permissions.handle_text_command("5215550000014", "llamar +521559000000")
    assert "No pude enviar" in reply
    # La reserva local se devolvió: se puede volver a pedir hoy mismo.
    ok, _ = state.reserve_request("521559000000")
    assert ok


def test_terminate_antes_de_descolgar_cancela_la_saliente(monkeypatch):
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650007")
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=True, call_id="wacid.META-OUT-7"))
    monkeypatch.setattr(graph, "terminate", lambda cid: graph.CallActionResult(ok=True))

    async def scenario():
        assert await manager.start_outbound("5215587650007") is None
        await asyncio.sleep(0.1)
        await manager.dispatch_events(
            extract_call_events(calls_payload("terminate", call_id="wacid.META-OUT-7"), PNID)
        )
        await _drain_tasks()

    run(scenario())
    assert state.by_remote_id("wacid.META-OUT-7")["state"] == "ended"
    assert state.daily_seconds_remaining() == 600


def test_respuesta_en_el_limite_del_timbre_no_se_corta(monkeypatch):
    """Si la respuesta gana la transición justo cuando vence el timbre, la
    llamada sigue: jamás se termina ni se devuelve una contestada."""
    from src.calls import graph

    db.connect()
    _reset_phone()
    _outbound_env(monkeypatch)
    _grant_permission("5215587650008")
    monkeypatch.setattr(graph, "connect", lambda to, sdp: graph.CallActionResult(ok=True, call_id="wacid.META-OUT-8"))
    ended = []
    monkeypatch.setattr(graph, "terminate", lambda cid: (ended.append(cid) or graph.CallActionResult(ok=True)))
    monkeypatch.setattr(manager, "RING_TIMEOUT_SECONDS", 0.15)
    spoke = []

    async def fake_converse(session, caller, call_id, deadline_at, stop_event):
        spoke.append(call_id)
        return "colgado"

    monkeypatch.setattr(voice_loop, "converse", fake_converse)

    async def scenario():
        assert await manager.start_outbound("5215587650008") is None
        await asyncio.sleep(0.05)
        # La respuesta llega por la vía normal (transición guardada)…
        local_id = manager._remote_to_local["wacid.META-OUT-8"]
        assert state.transition(local_id, "active", ("accepting",), answered_at=time.time())
        # …pero la tarea despierta por timeout, no por el evento.
        await asyncio.sleep(0.2)
        manager._outbound[local_id].answer_sdp = "v=0\r\n(answer)"
        manager._outbound[local_id].answer_event.set()
        await _drain_tasks()

    run(scenario())
    assert len(spoke) == 1                   # conversó: la respuesta ganó al timbre
    assert ended == ["wacid.META-OUT-8"]     # se colgó al final, no por timeout
    row = state.by_remote_id("wacid.META-OUT-8")
    assert row["state"] == "ended"           # contestada y cerrada, no "sin respuesta"
    assert state.daily_seconds_remaining() >= 595
