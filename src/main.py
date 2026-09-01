"""Arranque del servicio.

Enciende tres cosas y nada más: los registros (logs), la memoria (base de
datos) y el trabajador de fondo. Las credenciales de fases futuras no se
tocan aquí: el servicio arranca completo aunque solo existan las variables
de la puesta en marcha.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src import config, db
from src.queue import worker
from src.webhook.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agente")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    warning = config.storage_warning()
    if warning:
        logger.warning(warning)
    rebuild_task = None  # referencia FUERTE: la tarea vive aquí hasta el cierre
    if config.rag_enabled():
        # La libreta se rearma en cada arranque desde conocimiento/: agregar un
        # documento es subirlo a tu copia y dejar que Railway redespliegue.
        # El rearme corre en segundo plano (leer un PDF escaneado puede tomar
        # minutos) y mientras tanto sigue respondiendo el índice anterior, que
        # vive en tu volumen. La primera vez, la libreta aparece al terminar.
        from src.rag import index

        index.STOP_EVENT.clear()

        async def _rebuild_background() -> None:
            try:
                await asyncio.to_thread(index.rebuild)
            except Exception:
                logger.exception("No pude armar la libreta; el índice anterior sigue en pie.")

        rebuild_task = asyncio.create_task(_rebuild_background())
    task = asyncio.create_task(worker.run_forever())
    logger.info("Agente listo. Esperando el primer mensaje.")
    try:
        yield
    finally:
        task.cancel()
        if rebuild_task is not None:
            # Apagado cooperativo: la señal corta el rearme entre archivos y
            # páginas (un hilo no se interrumpe por la fuerza) y se espera a
            # que termine limpio antes de cerrar.
            from src.rag import index

            index.STOP_EVENT.set()
            rebuild_task.cancel()
            try:
                await rebuild_task
            except asyncio.CancelledError:
                pass


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
