"""La libreta: índice de búsqueda sobre tus documentos.

Cómo funciona, en corto:
1. Al arrancar (con FEATURE_RAG=on) lee los documentos de `conocimiento/` —
   `.md`, `.txt`, `.pdf` (aunque sea escaneado), `.docx` y `.xlsx` —, parte
   cada uno en fragmentos de unos pocos párrafos y los guarda en un índice de
   búsqueda por palabras dentro de la propia memoria del servicio (SQLite FTS5).
   La conversión de PDF/Word/Excel ocurre aquí, en tu servicio (src/rag/extract.py).
2. Ante cada pregunta, busca los fragmentos con más palabras en común y se los
   pasa al cerebro como "tu libreta". La búsqueda ocurre dentro de tu
   servicio; los fragmentos elegidos viajan luego al modelo junto con el
   mensaje (ver docs/runbook-operacion.md, "A dónde viajan los datos").

Si agregas o cambias documentos, Railway redespliega y el índice se rearma solo
(en segundo plano: mientras tanto sigue respondiendo el índice anterior, que
vive en tu volumen). Regla de seguridad del rearme: un archivo que NO se pudo
leer con confianza conserva su versión anterior en el índice — solo borrar el
archivo de `conocimiento/` borra su contenido de la libreta.
"""

import logging
import os
import re
import sqlite3
import threading

from src import config, db

logger = logging.getLogger("agente")

CHUNK_TARGET = 700     # caracteres aproximados por fragmento
TOP_K = 4
SUPPORTED_EXTENSIONS = (".md", ".txt", ".pdf", ".docx", ".xlsx")
_available: bool | None = None

# Señal de apagado cooperativo: el servidor la activa al cerrar y el rearme la
# consulta entre archivos y entre páginas (un hilo no se puede interrumpir por
# la fuerza a mitad de una conversión).
STOP_EVENT = threading.Event()

STOPWORDS = {
    "a", "al", "ante", "como", "con", "cual", "cuales", "cuando", "cuanto",
    "cuanta", "cuantos", "de", "del", "donde", "el", "ella", "ellos", "en",
    "entre", "es", "esa", "ese", "eso", "esta", "este", "esto", "estan", "hay",
    "hola", "la", "las", "le", "les", "lo", "los", "me", "mi", "mis", "muy",
    "nos", "o", "para", "pero", "por", "porque", "que", "quien", "se", "ser",
    "si", "sin", "sobre", "son", "su", "sus", "te", "tiene", "tienen", "tu",
    "tus", "un", "una", "unos", "unas", "y", "ya", "yo", "the", "and", "for",
}


def _ensure_table() -> bool:
    """Crea el índice si el motor lo soporta. Si no, la libreta se apaga sola."""
    global _available
    if _available is not None:
        return _available
    try:
        with db.transaction() as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5("
                "source, title, chunk, tokenize='unicode61 remove_diacritics 2')"
            )
        _available = True
    except sqlite3.OperationalError as exc:
        logger.error("La libreta no esta disponible en este entorno (%s): sigo sin ella.", exc)
        _available = False
    return _available


def _chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) > CHUNK_TARGET and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def rebuild() -> int:
    """Rearma el índice desde la carpeta de conocimiento. Devuelve fragmentos totales.

    Reemplazo POR ARCHIVO, en una sola transacción corta al final:
    - archivo leído con confianza -> sus fragmentos nuevos sustituyen a los viejos;
    - archivo que falló (dañado, fallo pasajero, presupuesto, visión apagada)
      -> se CONSERVA su versión anterior, con aviso en el registro;
    - archivo que ya no existe en la carpeta -> sus fragmentos se borran.
    La lectura (incluida la visión, que cuesta tiempo y dinero) ocurre ANTES y
    FUERA de la transacción: el timbre jamás espera por un rearme.
    """
    if not _ensure_table():
        return 0
    from src.rag import extract as extractor  # carga perezosa

    folder = config.KNOWLEDGE_DIR
    stats = extractor.RebuildStats()
    names: list[str] = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(SUPPORTED_EXTENSIONS) or name.lower() == "readme.md":
                continue
            names.append(name)

    results: dict[str, "extractor.ExtractResult"] = {}
    for name in names:
        if STOP_EVENT.is_set():
            logger.info("Rearme de la libreta interrumpido por apagado; el índice anterior queda en pie.")
            return 0
        results[name] = extractor.extract(os.path.join(folder, name), stats, STOP_EVENT)

    if STOP_EVENT.is_set():
        logger.info("Rearme de la libreta interrumpido por apagado; el índice anterior queda en pie.")
        return 0

    preserved: list[tuple[str, str]] = []
    published = 0
    with db.transaction() as conn:
        existing = {r["source"] for r in conn.execute("SELECT DISTINCT source FROM knowledge")}
        for gone in sorted(existing - set(names)):
            conn.execute("DELETE FROM knowledge WHERE source = ?", (gone,))
        for name, result in results.items():
            if not result.ok:
                preserved.append((name, result.reason))
                continue
            conn.execute("DELETE FROM knowledge WHERE source = ?", (name,))
            title = _title_of(result.text, name)
            for chunk in _chunks(result.text):
                conn.execute(
                    "INSERT INTO knowledge (source, title, chunk) VALUES (?, ?, ?)",
                    (name, title, chunk),
                )
            published += 1
            if result.skipped_pages:
                logger.warning(
                    "Libreta: %s publicado con %d página(s) saltada(s) por tope.",
                    name,
                    result.skipped_pages,
                )
        total = conn.execute("SELECT COUNT(*) AS c FROM knowledge").fetchone()["c"]

    for name, reason in preserved:
        logger.warning("Libreta: conservo la versión anterior de %s (%s).", name, reason)
    for name, pages in sorted(stats.vision_pages.items()):
        logger.info("Libreta: %s — %d página(s) leídas por visión en este rearme.", name, pages)
    kind = "completo" if not preserved and stats.pending_pages == 0 else "parcial"
    logger.info(
        "Libreta lista (rearme %s): %d fragmentos; %d archivo(s) publicados, %d conservado(s); "
        "visión: %d solicitud(es), %d acierto(s) de caché, %d página(s) pendientes.",
        kind,
        total,
        published,
        len(preserved),
        stats.http_calls,
        stats.cache_hits,
        stats.pending_pages,
    )
    return total


def _terms(question: str) -> list[str]:
    words = re.findall(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}", question.lower())
    seen: list[str] = []
    for w in words:
        if w not in STOPWORDS and w not in seen:
            seen.append(w)
    return seen[:12]


def search(question: str, k: int = TOP_K) -> list[dict]:
    """Devuelve los fragmentos más parecidos a la pregunta (título, fuente, texto)."""
    if not _ensure_table():
        return []
    terms = _terms(question)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        with db.transaction() as conn:
            rows = conn.execute(
                "SELECT source, title, chunk FROM knowledge"
                " WHERE knowledge MATCH ? ORDER BY bm25(knowledge) LIMIT ?",
                (match, k),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("Busqueda en la libreta fallo (%s): respondo sin ella.", exc)
        return []
    return [{"source": r["source"], "title": r["title"], "chunk": r["chunk"]} for r in rows]
