"""La fila de espera: dedupe, espera corta, reintentos y fichas agotadas."""

import time

from src.queue import store
from src.webhook.parse import InboundMessage


def msg(wamid: str, sender: str = "521", body: str = "hola") -> InboundMessage:
    return InboundMessage(wamid=wamid, sender=sender, kind="text", body=body, timestamp="1")


def test_enqueue_dedupe_por_wamid():
    assert store.enqueue(msg("a")) is True
    assert store.enqueue(msg("a")) is False


def test_claim_respeta_la_espera_corta(monkeypatch):
    store.enqueue(msg("a"))
    assert store.claim_ready_job() is None  # aún dentro del debounce
    real = time.time
    monkeypatch.setattr(store.time, "time", lambda: real() + store.DEBOUNCE_SECONDS + 0.1)
    job = store.claim_ready_job()
    assert job is not None and job.sender == "521" and job.texts == ["hola"] and job.attempts == 1
    assert store.claim_ready_job() is None  # ya está reclamada


def test_complete_cierra_o_reencola_si_llego_algo_nuevo(monkeypatch):
    real = time.time
    store.enqueue(msg("a"))
    monkeypatch.setattr(store.time, "time", lambda: real() + 3)
    job = store.claim_ready_job()
    store.enqueue(msg("b"))  # llega mientras se atiende
    store.complete(job)
    monkeypatch.setattr(store.time, "time", lambda: real() + 6)
    again = store.claim_ready_job()
    assert again is not None and again.texts == ["hola"] and len(again.message_ids) == 1
    store.complete(again)
    monkeypatch.setattr(store.time, "time", lambda: real() + 9)
    assert store.claim_ready_job() is None


def test_fail_reintenta_con_pausa_y_agota(monkeypatch):
    real = time.time
    offset = 3.0
    monkeypatch.setattr(store.time, "time", lambda: real() + offset)
    store.enqueue(msg("a"))
    for intento in range(1, store.MAX_ATTEMPTS + 1):
        offset += 400  # más que el techo de la pausa
        job = store.claim_ready_job()
        assert job is not None and job.attempts == intento
        store.fail(job, "falla de prueba")
    offset += 400
    assert store.claim_ready_job() is None  # agotada: no se reclama más
    from src import db

    row = db.connect().execute("SELECT status, last_error FROM jobs WHERE sender='521'").fetchone()
    assert row["status"] == "dead" and row["last_error"] == "falla de prueba"
    # Un mensaje nuevo revive la ficha desde cero.
    assert store.enqueue(msg("nuevo")) is True
    offset += 400
    job = store.claim_ready_job()
    assert job is not None and job.attempts == 1
