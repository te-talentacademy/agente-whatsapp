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
    if config.rag_enabled():
        # La libreta se rearma en cada arranque desde conocimiento/: agregar un
        # documento es subirlo a tu copia y dejar que Railway redespliegue.
        from src.rag import index

        try:
            index.rebuild()
        except Exception:
            logger.exception("No pude armar la libreta; el agente sigue sin ella.")
    task = asyncio.create_task(worker.run_forever())
    logger.info("Agente listo. Esperando el primer mensaje.")
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
