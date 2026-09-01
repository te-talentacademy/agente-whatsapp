"""Números de teléfono en una sola forma canónica.

WhatsApp escribe el mismo número de varias maneras (+52 55..., 5255...,
con espacios o guiones). Antes de comparar o guardar un número, todo pasa
por aquí: solo dígitos, sin signo, sin separadores.
"""


def canonical(number: str) -> str:
    """Deja solo los dígitos: '+52 55-1234' y '525512 34' quedan iguales."""
    if not isinstance(number, str):
        return ""
    return "".join(ch for ch in number if ch.isdigit())


def same(a: str, b: str) -> bool:
    ca, cb = canonical(a), canonical(b)
    return bool(ca) and ca == cb
