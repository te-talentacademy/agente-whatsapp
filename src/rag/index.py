"""La libreta: índice de búsqueda sobre tus documentos.

Cómo funciona, en corto:
1. Al arrancar (con FEATURE_RAG=on) lee `conocimiento/*.md|*.txt`, parte cada
   documento en fragmentos de unos pocos párrafos y los guarda en un índice de
   búsqueda por palabras dentro de la propia memoria del servicio (SQLite FTS5).
2. Ante cada pregunta, busca los fragmentos con más palabras en común y se los
   pasa al cerebro como "tu libreta". La búsqueda ocurre dentro de tu
   servicio; los fragmentos elegidos viajan luego al modelo junto con el
   mensaje (ver docs/runbook-operacion.md, "A dónde viajan los datos").

Si agregas o cambias documentos, Railway redespliega y el índice se rearma solo.
"""

import logging
import os
import re
import sqlite3

from src import config, db

logger = logging.getLogger("agente")

CHUNK_TARGET = 700     # caracteres aproximados por fragmento
TOP_K = 4
_available: bool | None = None

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
    """Rearma el índice desde la carpeta de conocimiento. Devuelve fragmentos cargados."""
    if not _ensure_table():
        return 0
    folder = config.KNOWLEDGE_DIR
    rows: list[tuple[str, str, str]] = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".md", ".txt")) or name.lower() == "readme.md":
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            title = _title_of(text, name)
            for chunk in _chunks(text):
                rows.append((name, title, chunk))
    with db.transaction() as conn:
        conn.execute("DELETE FROM knowledge")
        conn.executemany("INSERT INTO knowledge (source, title, chunk) VALUES (?, ?, ?)", rows)
    logger.info("Libreta lista: %d fragmentos de %s/.", len(rows), folder)
    return len(rows)


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
