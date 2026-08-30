"""El timbre del agente: la puerta que Meta toca para avisar de cada mensaje.

Vive en la raíz del servicio, así la dirección que genera Railway se pega tal
cual en el panel de Meta, sin agregarle nada.

- GET  con parámetros hub.* -> verificación del timbre (contraseña compartida).
- GET  sin parámetros       -> página de salud ("tu agente está vivo").
- POST                      -> aviso de mensaje: confirmar rápido, trabajar detrás.
"""

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from src import config, db
from src.queue import store, worker
from src.webhook.parse import extract_messages
from src.webhook.signature import verify_signature

logger = logging.getLogger("agente")
router = APIRouter()

MAX_BODY_BYTES = 1_048_576  # 1 MiB: nadie necesita tocar el timbre con más
WORKER_STALE_SECONDS = 30.0

HEALTH_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Agente</title></head>
<body style="font-family: sans-serif; text-align: center; padding-top: 4rem;">
<h1>Tu agente está vivo</h1>
<p>El timbre está conectado y escuchando.</p>
</body></html>"""


@router.get("/")
async def root(request: Request) -> Response:
    params = request.query_params
    if "hub.mode" in params or "hub.verify_token" in params:
        return _verify(params)
    # Página de salud: comprueba sin tocar nada (ni consume ni reclama fichas).
    if not db.healthy():
        return PlainTextResponse("memoria no disponible", status_code=503)
    beat = worker.last_beat()
    if beat == 0.0 or time.time() - beat > WORKER_STALE_SECONDS:
        return PlainTextResponse("trabajador de fondo detenido", status_code=503)
    return HTMLResponse(HEALTH_PAGE)


def _verify(params) -> Response:
    expected = config.verify_token()
    if not expected:
        # Sin contraseña configurada no hay verificación posible: error ruidoso.
        return PlainTextResponse("falta WHATSAPP_VERIFY_TOKEN", status_code=503)
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == expected and challenge:
        # Meta espera recibir el desafío tal cual, en texto plano.
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("verificacion rechazada", status_code=403)


async def _read_body_capped(request: Request) -> bytes | None:
    """Lee el cuerpo con tope de tamaño. None = excedido."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/")
async def inbound(request: Request):
    # 1) Tope de tamaño ANTES de cualquier otro trabajo.
    raw_body = await _read_body_capped(request)
    if raw_body is None:
        return PlainTextResponse("demasiado grande", status_code=413)

    # 2) Configuración mínima presente. Sin App Secret no se acepta nada:
    #    la firma es lo único que garantiza que el aviso viene de Meta.
    secret = config.app_secret()
    own_number = config.phone_number_id()
    if not secret or not own_number:
        logger.error(
            "Falta META_APP_SECRET o WHATSAPP_PHONE_NUMBER_ID: "
            "el timbre no puede aceptar avisos hasta configurarlas."
        )
        return PlainTextResponse("configuracion incompleta", status_code=503)

    # 3) Firma sobre el cuerpo crudo, antes de leer el contenido.
    header = request.headers.get("x-hub-signature-256")
    if not verify_signature(raw_body, header, secret):
        return PlainTextResponse("firma invalida", status_code=401)

    # 4) Contenido. Un sobre firmado pero ilegible se ignora con 200:
    #    devolver error haría que Meta lo reenviara una y otra vez sin remedio.
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return {"ignored": True}

    # 5) Extraer los mensajes dirigidos a TU número y guardarlos.
    messages = extract_messages(payload, own_number)
    stored = 0
    for msg in messages:
        if store.enqueue(msg):
            stored += 1
            if config.log_message_text():
                logger.info(
                    'MENSAJE RECIBIDO de %s (%s): "%s"',
                    msg.sender,
                    msg.kind,
                    msg.body or "(sin texto)",
                )
            else:
                logger.info("MENSAJE RECIBIDO de %s (%s)", msg.sender, msg.kind)

    # 6) Confirmar rápido; la respuesta al mensaje corre por dentro.
    return {"received": stored}
