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
# Oídos, ojos y voz (fase del curso: FEATURE_VOICE_IN / FEATURE_VISION /
# FEATURE_VOICE_OUT)
# ---------------------------------------------------------------------------

DEFAULT_VISION_MODEL = "qwen/qwen3.8-flash"  # segundo motor: solo despierta con adjuntos
DEFAULT_DAILY_VOICE_LIMIT = 100              # solicitudes de voz (oídos + voz) por día
AUDIO_NOTES_PER_TURN = 2
IMAGES_PER_TURN = 3
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
VOICE_MAX_AGE_SECONDS = 600.0   # un audio viejo no se envía tarde
VOICE_MAX_CHARS = 800           # respuestas muy largas van solo en texto


def voice_in_enabled() -> bool:
    return flag("FEATURE_VOICE_IN")


def voice_out_enabled() -> bool:
    return flag("FEATURE_VOICE_OUT")


def vision_enabled() -> bool:
    return flag("FEATURE_VISION")


def cartesia_api_key() -> str:
    return os.environ.get("CARTESIA_API_KEY", "").strip()


def cartesia_voice_id() -> str:
    return os.environ.get("CARTESIA_VOICE_ID", "").strip()


def openrouter_vision_model() -> str:
    return os.environ.get("OPENROUTER_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL


def daily_voice_limit() -> int | None:
    return limit("DAILY_VOICE_LIMIT", DEFAULT_DAILY_VOICE_LIMIT)


# ---------------------------------------------------------------------------
# Llamadas (fase del curso: FEATURE_CALLS / FEATURE_OUTBOUND_CALLS)
# ---------------------------------------------------------------------------
# Las llamadas son el ramal más caro (cada minuto gasta oídos, cerebro y voz),
# así que llegan con cinturones propios: minutos por llamada, minutos por día
# y una lista opcional de números permitidos.

DEFAULT_CALL_MAX_MINUTES = 5
DEFAULT_CALL_DAILY_MINUTES = 30
TURN_TTL_MARGIN_SECONDS = 120   # la credencial debe vivir MÁS que la llamada
DEFAULT_CALL_GREETING = "¡Hola! Soy {name}. ¿En qué te ayudo?"
DEFAULT_CALL_GOODBYE = "¡Gracias por llamar! Hasta pronto."

# El teléfono atiende UNA llamada a la vez: el servicio corre en un solo
# proceso y más líneas simultáneas multiplicarían el gasto sin control.
# El valor es un techo fijo de esta versión, no una variable.
CALL_MAX_CONCURRENT = 1
CALL_CLAIM_LEASE_SECONDS = 60.0   # una llamada reclamada sin sesión viva más
                                  # de esto se considera huérfana (ver runbook)
CALL_IDLE_TIMEOUT_SECONDS = 30.0  # silencio total -> despedida y colgar
MAX_UTTERANCE_SECONDS = 60.0      # una intervención no acumula audio sin fin
DEFAULT_GRAPH_BASE_URL = "https://graph.facebook.com/v23.0"


def calls_enabled() -> bool:
    return flag("FEATURE_CALLS")


def outbound_calls_enabled() -> bool:
    return flag("FEATURE_OUTBOUND_CALLS")


def turn_key_id() -> str:
    return os.environ.get("CLOUDFLARE_TURN_KEY_ID", "").strip()


def turn_api_token() -> str:
    return os.environ.get("CLOUDFLARE_TURN_API_TOKEN", "").strip()


def call_max_minutes() -> int:
    # A diferencia de los cupos diarios, el tope por llamada nunca es "sin
    # límite": un 0 aquí no apaga el cinturón, lo devuelve al recomendado.
    value = limit("CALL_MAX_MINUTES", DEFAULT_CALL_MAX_MINUTES)
    return value if value else DEFAULT_CALL_MAX_MINUTES


def call_daily_minutes_limit() -> int | None:
    return limit("CALL_DAILY_MINUTES_LIMIT", DEFAULT_CALL_DAILY_MINUTES)


def call_allowed_numbers() -> set[str]:
    """Lista opcional de números que pueden llamar (vacía = cualquiera)."""
    raw = os.environ.get("CALL_ALLOWED_NUMBERS", "").strip()
    if not raw:
        return set()
    return {part.strip().lstrip("+") for part in raw.split(",") if part.strip()}


def call_greeting_text() -> str:
    custom = os.environ.get("CALL_GREETING_TEXT", "").strip()
    return custom or DEFAULT_CALL_GREETING.format(name=agent_name())


def call_goodbye_text() -> str:
    custom = os.environ.get("CALL_GOODBYE_TEXT", "").strip()
    return custom or DEFAULT_CALL_GOODBYE


def call_owner_number() -> str:
    """El número del dueño: el único que puede ordenar llamadas salientes."""
    return os.environ.get("CALL_OWNER_NUMBER", "").strip()


def graph_base_url() -> str:
    """Dirección base de la API de Meta. Variable avanzada: no la toques
    salvo que Meta retire la versión anclada (ver docs/variables.md)."""
    return os.environ.get("GRAPH_BASE_URL", "").strip() or DEFAULT_GRAPH_BASE_URL


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
