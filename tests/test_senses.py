"""Oídos, ojos y voz — sin salir a internet."""

import json
import time

from src import agent, config
from src.llm import client
from src.queue.store import Job
from src.voice import cartesia, notes, quota
from src.webhook import parse
from src.whatsapp import media, send
from tests.conftest import PNID


def job(texts=None, attachments=None, sender="521"):
    return Job(sender=sender, attempts=1, message_ids=[1], texts=texts or [],
               kinds=[], attachments=attachments or [], oldest_at=time.time())


def brain_on(monkeypatch):
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")


# --- el sobre trae el identificador del adjunto ---------------------------

def test_parse_extrae_media_id():
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": PNID},
        "messages": [
            {"from": "521", "id": "w1", "type": "audio", "audio": {"id": "MEDIA-AUDIO", "mime_type": "audio/ogg"}},
            {"from": "521", "id": "w2", "type": "image", "image": {"id": "MEDIA-FOTO", "caption": "mi ticket"}},
        ]}}]}]}
    out = parse.extract_messages(payload, PNID)
    assert [(m.kind, m.media_id, m.body) for m in out] == [
        ("audio", "MEDIA-AUDIO", ""), ("image", "MEDIA-FOTO", "mi ticket")]


# --- descargas: tipo real por bytes y topes -------------------------------

def test_sniff_reconoce_por_bytes_no_por_etiqueta():
    assert media._sniff(b"OggS....", media.AUDIO_MAGIC) == "audio/ogg"
    assert media._sniff(b"\xff\xd8\xff\xe0..", media.IMAGE_MAGIC) == "image/jpeg"
    assert media._sniff(b"\x00\x00\x00\x18ftypM4A ", media.AUDIO_MAGIC) == "audio/mp4"
    assert media._sniff(b"<html>hola", media.AUDIO_MAGIC) is None
    assert media._sniff(b"MZ\x90\x00", media.IMAGE_MAGIC) is None


# --- Cartesia: fallback, clasificación y cupo -----------------------------

class _Resp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload else ""

    def json(self):
        return self._payload


def test_stt_usa_plan_b_solo_si_el_primario_no_existe(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    modelos = []

    def fake_post(url, headers=None, files=None, data=None, timeout=None, json=None):
        modelos.append(data["model"])
        if data["model"] == cartesia.STT_MODEL:
            return _Resp(400, {"error": "Unsupported model: Model not supported"})
        return _Resp(200, {"text": "hola desde el audio"})

    monkeypatch.setattr(cartesia.httpx, "post", fake_post)
    r = cartesia.transcribe(b"OggS...", "audio/ogg")
    assert r.ok and r.text == "hola desde el audio"
    assert modelos == [cartesia.STT_MODEL, cartesia.STT_FALLBACK_MODEL]


def test_cartesia_401_es_refundable_y_5xx_no(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    monkeypatch.setattr(cartesia.httpx, "post", lambda *a, **k: _Resp(401))
    r = cartesia.transcribe(b"OggS", "audio/ogg")
    assert not r.ok and r.refundable
    monkeypatch.setattr(cartesia.httpx, "post", lambda *a, **k: _Resp(500))
    r = cartesia.transcribe(b"OggS", "audio/ogg")
    assert not r.ok and not r.refundable
    monkeypatch.delenv("CARTESIA_VOICE_ID", raising=False)
    r = cartesia.synthesize("hola")
    assert not r.ok and r.refundable and "CARTESIA_VOICE_ID" in r.reason


def test_cupo_de_voz_reserva_y_devuelve(monkeypatch):
    monkeypatch.setenv("DAILY_VOICE_LIMIT", "2")
    assert quota.reserve() and quota.reserve()
    assert not quota.reserve()
    quota.release()
    assert quota.reserve()
    monkeypatch.setenv("DAILY_VOICE_LIMIT", "0")
    assert config.daily_voice_limit() is None


# --- oídos: la transcripción entra como texto de la persona ---------------

def test_nota_de_voz_entra_como_texto_del_turno(monkeypatch):
    brain_on(monkeypatch)
    monkeypatch.setenv("FEATURE_VOICE_IN", "on")
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    monkeypatch.setattr(media, "download", lambda mid, kind: (b"OggS", "audio/ogg"))
    monkeypatch.setattr(cartesia, "transcribe", lambda data, mime: cartesia.VoiceResult(ok=True, text="quiero dos hogazas"))
    capturado = {}
    monkeypatch.setattr(client, "complete", lambda messages, model, *a, **k: capturado.update(user=messages[-1]["content"], model=model) or client.LlmResult(ok=True, text="claro"))
    monkeypatch.setattr(agent, "send_text", lambda to, t: client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job(texts=["hola"], attachments=[("audio", "M1")]))
    assert capturado["user"] == "hola\n[Nota de voz] quiero dos hogazas"
    assert capturado["model"] == config.DEFAULT_MODEL  # sin foto: motor de texto


def test_sin_feature_voice_in_no_se_llama_a_cartesia(monkeypatch):
    brain_on(monkeypatch)
    llamadas = []
    monkeypatch.setattr(cartesia, "transcribe", lambda *a, **k: llamadas.append(1))
    monkeypatch.setattr(client, "complete", lambda *a, **k: client.LlmResult(ok=True, text="ok"))
    monkeypatch.setattr(agent, "send_text", lambda to, t: client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job(texts=["hola"], attachments=[("audio", "M1")]))
    assert llamadas == []


# --- ojos: gate por adjunto ------------------------------------------------

def test_con_foto_viaja_al_motor_con_vision_y_sin_foto_no(monkeypatch):
    brain_on(monkeypatch)
    monkeypatch.setenv("FEATURE_VISION", "on")
    monkeypatch.setattr(media, "download", lambda mid, kind: (b"\xff\xd8\xff\xe0", "image/jpeg"))
    capturado = {}
    monkeypatch.setattr(client, "complete", lambda messages, model, *a, **k: capturado.update(model=model, content=messages[-1]["content"]) or client.LlmResult(ok=True, text="veo pan"))
    monkeypatch.setattr(agent, "send_text", lambda to, t: client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job(texts=["que es esto?"], attachments=[("image", "F1")]))
    assert capturado["model"] == config.DEFAULT_VISION_MODEL
    assert isinstance(capturado["content"], list)
    assert capturado["content"][0] == {"type": "text", "text": "que es esto?"}
    assert capturado["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    agent.handle_job(job(texts=["y sin foto?"], sender="522"))
    assert capturado["model"] == config.DEFAULT_MODEL and isinstance(capturado["content"], str)


def test_vision_apagada_ignora_la_foto(monkeypatch):
    brain_on(monkeypatch)
    descargas = []
    monkeypatch.setattr(media, "download", lambda *a, **k: descargas.append(1))
    monkeypatch.setattr(client, "complete", lambda messages, model, *a, **k: client.LlmResult(ok=True, text="ok"))
    monkeypatch.setattr(agent, "send_text", lambda to, t: client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job(texts=["mira"], attachments=[("image", "F1")]))
    assert descargas == []


# --- voz saliente: best-effort y jamás para textos fijos ------------------

def test_la_voz_sale_para_el_cerebro_pero_no_para_el_acuse(monkeypatch):
    brain_on(monkeypatch)
    monkeypatch.setenv("FEATURE_VOICE_OUT", "on")
    hablado = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target, self._args = target, args

        def start(self):
            hablado.append(self._args[1])

    monkeypatch.setattr(agent.threading, "Thread", FakeThread)
    monkeypatch.setattr(client, "complete", lambda *a, **k: client.LlmResult(ok=True, text="respuesta pensada"))
    monkeypatch.setattr(agent, "send_text", lambda to, t: client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job(texts=["hola"]))
    assert hablado == ["respuesta pensada"]
    # El acuse (texto fijo) no se locuta.
    monkeypatch.delenv("FEATURE_BRAIN", raising=False)
    agent.handle_job(job(texts=["hola"], sender="522"))
    assert hablado == ["respuesta pensada"]


def test_speak_reply_respeta_caducidad_largo_y_cupo(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    llamadas = []
    monkeypatch.setattr(cartesia, "synthesize", lambda text: llamadas.append(text) or cartesia.VoiceResult(ok=True, data=b"ID3mp3"))
    subidas, enviados = [], []
    monkeypatch.setattr(send, "upload_media", lambda data, mime, fn: subidas.append(mime) or "MEDIA-9")
    monkeypatch.setattr(send, "send_audio", lambda to, mid: enviados.append(mid) or send.SendResult(ok=True))
    monkeypatch.setattr(notes.shutil, "which", lambda name: None)  # sin ffmpeg: MP3
    notes.speak_reply("521", "hola", time.time() - 9999)          # turno viejo
    notes.speak_reply("521", "x" * (config.VOICE_MAX_CHARS + 1), time.time())  # muy largo
    assert llamadas == []
    monkeypatch.setenv("DAILY_VOICE_LIMIT", "1")
    notes.speak_reply("521", "hola", time.time())
    assert llamadas == ["hola"] and subidas == ["audio/mpeg"] and enviados == ["MEDIA-9"]
    notes.speak_reply("521", "otra", time.time())                 # cupo agotado
    assert llamadas == ["hola"]


def test_speak_reply_jamas_lanza(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "llave-falsa")
    monkeypatch.setattr(cartesia, "synthesize", lambda text: (_ for _ in ()).throw(RuntimeError("boom")))
    notes.speak_reply("521", "hola", time.time())  # no debe propagar
