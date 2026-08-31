"""Configuración del agente.

Todo se controla con variables de entorno (la "caja fuerte" de Railway).
Los nombres exactos y su explicación viven en docs/variables.md.
"""

import os

# ---------------------------------------------------------------------------
# Interruptores (flags)
# ---------------------------------------------------------------------------
# Un interruptor solo enciende con el valor literal "on". Cualquier otro valor,
# un valor vacío o una variable ausente cuentan como apagado: si algo cuesta
# dinero, la duda siempre apaga, nunca enciende.


def flag(name: str, default: str = "off") -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        raw = default
    return raw == "on"


# ---------------------------------------------------------------------------
# Límites numéricos
# ---------------------------------------------------------------------------
# Regla de lectura: variable ausente o vacía -> se usa el valor recomendado;
# un "0" escrito a propósito -> sin límite. Así, una variable en blanco jamás
# desactiva una protección por accidente.


def limit(name: str, default: int) -> int | None:
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value == 0:
        return None  # sin límite, decisión explícita del dueño
    return max(value, 0) or default


# ---------------------------------------------------------------------------
# Credenciales de WhatsApp (las 4 obligatorias de la puesta en marcha)
# ---------------------------------------------------------------------------


def whatsapp_token() -> str:
    return os.environ.get("WHATSAPP_TOKEN", "").strip()


def phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()


def verify_token() -> str:
    return os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()


def app_secret() -> str:
    return os.environ.get("META_APP_SECRET", "").strip()


# ---------------------------------------------------------------------------
# Comportamiento base
# ---------------------------------------------------------------------------

DEFAULT_AUTO_REPLY_TEXT = (
    "Recibí tu mensaje. Todavía estoy en construcción, "
    "pero ya escucho perfecto."
)


def auto_reply_enabled() -> bool:
    return flag("AUTO_REPLY", default="on")


def auto_reply_text() -> str:
    return os.environ.get("AUTO_REPLY_TEXT", "").strip() or DEFAULT_AUTO_REPLY_TEXT


def log_message_text() -> bool:
    # Encendido de fábrica para que veas tus primeros mensajes en los registros.
    # Cuando atiendas conversaciones reales de clientes puedes apagarlo con
    # LOG_MESSAGE_TEXT=off (ver docs/runbook-operacion.md).
    return flag("LOG_MESSAGE_TEXT", default="on")


# ---------------------------------------------------------------------------
# El cerebro (fase del curso: FEATURE_BRAIN) y la libreta (FEATURE_RAG)
# ---------------------------------------------------------------------------
# Un solo lugar para el modelo recomendado: si algún día cambia, se cambia
# aquí o, sin tocar código, con la variable OPENROUTER_MODEL.

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_AGENT_NAME = "Mi agente"
DEFAULT_DAILY_MESSAGE_LIMIT = 200
PERSONALITY_FILE = "personalidad.md"
KNOWLEDGE_DIR = "conocimiento"


def brain_enabled() -> bool:
    return flag("FEATURE_BRAIN")


def rag_enabled() -> bool:
    return flag("FEATURE_RAG")


def openrouter_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def openrouter_model() -> str:
    return os.environ.get("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL


def agent_name() -> str:
    return os.environ.get("AGENT_NAME", "").strip() or DEFAULT_AGENT_NAME


def daily_message_limit() -> int | None:
    return limit("DAILY_MESSAGE_LIMIT", DEFAULT_DAILY_MESSAGE_LIMIT)


# ---------------------------------------------------------------------------
# Dónde vive la memoria del servicio (archivo de base de datos)
# ---------------------------------------------------------------------------
# En Railway, el disco normal se borra con cada despliegue. Para que los
# mensajes pendientes y el registro de mensajes ya vistos sobrevivan, el
# servicio necesita un volumen montado en /data (ver docs/variables.md).
# Railway anuncia el volumen con la variable RAILWAY_VOLUME_MOUNT_PATH; nos
# apoyamos en ella y no en que la carpeta simplemente exista.

VOLUME_MOUNT = "/data"


def volume_mounted() -> bool:
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip() == VOLUME_MOUNT


def db_path() -> str:
    explicit = os.environ.get("DB_PATH", "").strip()
    if explicit:
        return explicit
    if volume_mounted():
        return os.path.join(VOLUME_MOUNT, "agente.db")
    return "./agente.db"


def storage_warning() -> str | None:
    """Aviso de arranque si la memoria está en piso efímero (sin volumen)."""
    if os.environ.get("DB_PATH", "").strip() or volume_mounted():
        return None
    return (
        "AVISO: la memoria del agente esta en disco temporal. En Railway, "
        "adjunta un volumen montado en /data para que los mensajes pendientes "
        "sobrevivan a cada despliegue (ver docs/variables.md)."
    )
