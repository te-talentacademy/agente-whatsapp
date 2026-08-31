"""El timbre: verificación, firma, tope de tamaño y sobres raros."""

import time

from fastapi.testclient import TestClient

from src.main import app
from src.queue import worker
from src.webhook.parse import extract_messages
from src.webhook.signature import verify_signature
from tests.conftest import PNID, SECRET, VERIFY, meta_payload, sign


def client():
    return TestClient(app)


def test_verify_devuelve_challenge_crudo():
    r = client().get("/", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "4242"})
    assert r.status_code == 200
    assert r.content == b"4242"
    assert r.headers["content-type"].startswith("text/plain")


def test_verify_rechaza_token_distinto_y_sin_challenge():
    c = client()
    assert c.get("/", params={"hub.mode": "subscribe", "hub.verify_token": "otra", "hub.challenge": "1"}).status_code == 403
    assert c.get("/", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY}).status_code == 403


def test_verify_sin_variable_es_503(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "")
    r = client().get("/", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"})
    assert r.status_code == 503


def test_salud_exige_trabajador_vivo(monkeypatch):
    monkeypatch.setattr(worker, "_last_beat", 0.0)
    assert client().get("/").status_code == 503
    monkeypatch.setattr(worker, "_last_beat", time.time())
    assert client().get("/").status_code == 200


def test_post_firmado_encola_y_duplicado_no():
    body = meta_payload()
    c = client()
    r1 = c.post("/", content=body, headers={"X-Hub-Signature-256": sign(body)})
    r2 = c.post("/", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r1.status_code == 200 and r1.json() == {"received": 1}
    assert r2.status_code == 200 and r2.json() == {"received": 0}


def test_post_sin_firma_o_firma_mala_es_401():
    body = meta_payload()
    c = client()
    assert c.post("/", content=body).status_code == 401
    assert c.post("/", content=body, headers={"X-Hub-Signature-256": "sha256=" + "f" * 64}).status_code == 401
    assert c.post("/", content=body, headers={"X-Hub-Signature-256": sign(body, "otro-secreto")}).status_code == 401


def test_post_sin_app_secret_es_503(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "")
    body = meta_payload()
    assert client().post("/", content=body, headers={"X-Hub-Signature-256": sign(body)}).status_code == 503


def test_post_demasiado_grande_es_413():
    grande = b"x" * (1_048_576 + 1)
    assert client().post("/", content=grande, headers={"X-Hub-Signature-256": sign(grande)}).status_code == 413


def test_post_malformado_firmado_se_ignora_con_200():
    body = b"{esto no es json"
    r = client().post("/", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200 and r.json() == {"ignored": True}


def test_post_hostil_no_tumba_nada():
    body = ('{"object":"whatsapp_business_account","entry":[null,{"changes":[null,{"value":{"metadata":'
            '{"phone_number_id":"%s"},"messages":[null,{"id":42},{"from":"x"},{"id":"wamid.H","from":"521","type":"text"}]}}]}]}' % PNID).encode()
    r = client().post("/", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 200 and r.json() == {"received": 1}


def test_firma_valida_formato():
    assert verify_signature(b"abc", sign(b"abc"), SECRET)
    assert not verify_signature(b"abc", None, SECRET)
    assert not verify_signature(b"abc", "sha256=zz", SECRET)
    assert not verify_signature(b"abc", "md5=" + "0" * 64, SECRET)


def test_extract_filtra_otro_numero_y_statuses():
    import json

    otro = json.loads(meta_payload(pnid="999"))
    assert extract_messages(otro, PNID) == []
    statuses = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": PNID}, "statuses": [{"id": "x", "status": "delivered"}]}}]}]}
    assert extract_messages(statuses, PNID) == []
    propio = extract_messages(json.loads(meta_payload("hola", "wamid.1")), PNID)
    assert len(propio) == 1 and propio[0].body == "hola" and propio[0].kind == "text"
