"""El relevo de audio (TURN de Cloudflare).

Un servicio en la nube no recibe el audio directo de una llamada: hace
falta un relevo intermedio. Cloudflare presta ese relevo con credenciales
EFÍMERAS que se piden aquí, una por llamada, con una vida un poco más larga
que la propia llamada (la credencial jamás caduca a mitad de conversación).

Las direcciones del relevo son fijas y públicas; lo que se pide cada vez es
solo la credencial (usuario y clave temporales). Se usa el transporte TLS
por el puerto 443 — el que atraviesa cualquier red.

Regla de seguridad: SIN relevo vigente NO hay llamada. Si esta petición
falla, la llamada se rechaza con un motivo claro — nunca se intenta el
audio "a pelo" (fallaría mudo, que es peor que un rechazo honesto).
Los registros jamás incluyen la credencial.
"""

import logging
from dataclasses import dataclass

import httpx

from src import config

logger = logging.getLogger("agente")

CLOUDFLARE_TURN_URL = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
)
TURN_URL = "turns:turn.cloudflare.com:443?transport=tcp"  # TLS 443: pasa en cualquier red
STUN_URL = "stun:stun.cloudflare.com:3478"
TIMEOUT_SECONDS = 10.0


@dataclass
class RelayResult:
    ok: bool
    username: str = ""
    credential: str = ""
    reason: str = ""


def fetch(ttl_seconds: int) -> RelayResult:
    """Pide una credencial efímera de relevo para UNA llamada."""
    key_id = config.turn_key_id()
    token = config.turn_api_token()
    if not key_id or not token:
        return RelayResult(
            ok=False,
            reason="faltan CLOUDFLARE_TURN_KEY_ID / CLOUDFLARE_TURN_API_TOKEN",
        )
    try:
        response = httpx.post(
            CLOUDFLARE_TURN_URL.format(key_id=key_id),
            headers={"Authorization": f"Bearer {token}"},
            json={"ttl": int(ttl_seconds)},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return RelayResult(ok=False, reason=f"red hacia el relevo: {exc.__class__.__name__}")
    if response.status_code != 201:
        return RelayResult(
            ok=False,
            reason=f"el relevo rechazó la credencial ({response.status_code}): revisa las llaves TURN",
        )
    try:
        ice = response.json().get("iceServers")
    except ValueError:
        return RelayResult(ok=False, reason="respuesta del relevo con forma inesperada")
    # El esquema oficial devuelve un objeto o una lista; solo importan las
    # credenciales (las direcciones del relevo son las fijas de arriba).
    if isinstance(ice, dict):
        ice = [ice]
    username = credential = ""
    for entry in ice if isinstance(ice, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("username") and entry.get("credential"):
            username, credential = str(entry["username"]), str(entry["credential"])
            break
    if not username:
        return RelayResult(ok=False, reason="credencial de relevo incompleta")
    logger.info("LLAMADA: relevo de audio listo (TLS 443, vida %d s).", int(ttl_seconds))
    return RelayResult(ok=True, username=username, credential=credential)
