"""Verificación de la firma que Meta pone en cada aviso del timbre.

Meta firma el cuerpo crudo de cada POST con tu App Secret (encabezado
X-Hub-Signature-256). Comprobar esa firma es lo que garantiza que el aviso
viene de Meta y no de un desconocido imitándola.
"""

import hashlib
import hmac

PREFIX = "sha256="


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith(PREFIX):
        return False
    received = header[len(PREFIX):].strip().lower()
    # La firma es hex de 32 bytes; cualquier otra forma se rechaza sin calcular.
    if len(received) != 64 or any(c not in "0123456789abcdef" for c in received):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # Comparación en tiempo constante: no filtra pistas por velocidad.
    return hmac.compare_digest(received, expected)
