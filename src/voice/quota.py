"""Cinturón de seguridad de la voz: cupo diario de solicitudes a Cartesia.

Misma regla que el cerebro: se aparta el cupo ANTES de cada solicitud (un
intento cortado por la red pudo cobrarse) y solo se devuelve si el proveedor
la rechazó sin procesarla.
"""

from datetime import datetime, timezone

from src import config, db


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reserve() -> bool:
    limit = config.daily_voice_limit()
    with db.transaction() as conn:
        row = conn.execute("SELECT voice FROM usage WHERE day = ?", (_today(),)).fetchone()
        used = row["voice"] if row else 0
        if limit is not None and used >= limit:
            return False
        conn.execute(
            "INSERT INTO usage (day, replies, voice) VALUES (?, 0, 1)"
            " ON CONFLICT(day) DO UPDATE SET voice = voice + 1",
            (_today(),),
        )
    return True


def release() -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE usage SET voice = MAX(voice - 1, 0) WHERE day = ?", (_today(),))


def available() -> bool:
    """Solo mira (no aparta): ¿queda cupo de voz hoy? Sirve de candado barato
    antes de aceptar una llamada, que consumirá varias solicitudes."""
    limit = config.daily_voice_limit()
    if limit is None:
        return True
    with db.transaction() as conn:
        row = conn.execute("SELECT voice FROM usage WHERE day = ?", (_today(),)).fetchone()
    used = row["voice"] if row else 0
    return used < limit
