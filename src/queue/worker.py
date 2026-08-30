"""El trabajador de fondo: drena la fila de espera.

Corre dentro del mismo servicio, en un bucle tranquilo: pregunta si hay una
ficha lista, la atiende y vuelve a preguntar. Si no hay nada, descansa un
momento. Nada de lo que pase aquí puede tumbar el servicio.
"""

import asyncio
import logging
import time

from src import agent
from src.queue import store

logger = logging.getLogger("agente")

IDLE_SLEEP = 0.5
_last_beat: float = 0.0


def last_beat() -> float:
    """Momento de la última vuelta del bucle (para la comprobación de salud)."""
    return _last_beat


async def run_forever() -> None:
    global _last_beat
    logger.info("Trabajador de fondo en marcha.")
    while True:
        _last_beat = time.time()
        try:
            job = await asyncio.to_thread(store.claim_ready_job)
            if job is None:
                await asyncio.sleep(IDLE_SLEEP)
                continue
            await asyncio.to_thread(agent.handle_job, job)
        except asyncio.CancelledError:
            logger.info("Trabajador de fondo detenido.")
            raise
        except Exception:
            # Red de seguridad del bucle: registrar y seguir vivos.
            logger.exception("Fallo inesperado en el trabajador de fondo")
            await asyncio.sleep(IDLE_SLEEP)
