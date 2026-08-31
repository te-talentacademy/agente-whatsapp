"""Preparación común de las pruebas: variables de entorno y memoria temporal.

Las pruebas no tocan internet: OpenRouter y WhatsApp se sustituyen por dobles.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SECRET = "secreto-de-pruebas"
VERIFY = "miagente2026"
PNID = "111222333444555"

os.environ.update({
    "WHATSAPP_TOKEN": "token-falso",
    "WHATSAPP_PHONE_NUMBER_ID": PNID,
    "WHATSAPP_VERIFY_TOKEN": VERIFY,
    "META_APP_SECRET": SECRET,
    "AUTO_REPLY": "on",
    "LOG_MESSAGE_TEXT": "on",
})


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Cada prueba arranca con un archivo de memoria nuevo y vacío."""
    from src import db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "prueba.db"))
    for flag in ("FEATURE_BRAIN", "FEATURE_RAG", "OPENROUTER_API_KEY", "DAILY_MESSAGE_LIMIT"):
        monkeypatch.delenv(flag, raising=False)
    db._conn = None
    from src.rag import index

    index._available = None
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def sign(body: bytes, secret: str = SECRET) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def meta_payload(text: str = "hola", wamid: str = "wamid.PRUEBA", sender: str = "5215587654321", pnid: str = PNID) -> bytes:
    import json

    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": pnid},
            "messages": [{"from": sender, "id": wamid, "timestamp": "1", "type": "text", "text": {"body": text}}],
        }, "field": "messages"}]}],
    }).encode()
