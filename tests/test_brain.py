"""El cerebro y la libreta, sin salir a internet."""

import os

from src import agent, config
from src.llm import brain, client, prompt
from src.queue.store import Job
from src.rag import index


def job(text="¿cuánto cuesta la hogaza?", sender="521"):
    return Job(sender=sender, attempts=1, message_ids=[1], texts=[text], kinds=["text"])


def test_sin_cerebro_responde_acuse(monkeypatch):
    enviados = []
    monkeypatch.setattr(agent, "send_text", lambda to, t: enviados.append((to, t)) or client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job())
    assert enviados and enviados[0][1] == config.auto_reply_text()


def test_cerebro_encendido_sin_llave_cae_al_acuse(monkeypatch):
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    llamadas = []
    monkeypatch.setattr(client, "complete", lambda *a, **k: llamadas.append(1) or client.LlmResult(ok=True, text="no debería"))
    enviados = []
    monkeypatch.setattr(agent, "send_text", lambda to, t: enviados.append(t) or client.LlmResult(ok=True))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    agent.handle_job(job())
    assert llamadas == [] and enviados == [config.auto_reply_text()]


def test_cerebro_responde_y_recuerda(monkeypatch):
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    monkeypatch.setenv("AGENT_NAME", "Espiga")
    capturado = {}

    def fake_complete(messages, model, api_key, title=""):
        capturado["messages"] = messages
        capturado["model"] = model
        return client.LlmResult(ok=True, text="La hogaza campesina cuesta $95.")

    monkeypatch.setattr(client, "complete", fake_complete)
    t1 = brain.think("521", "¿cuánto cuesta la hogaza?")
    assert t1.text == "La hogaza campesina cuesta $95."
    assert capturado["messages"][0]["role"] == "system" and "Espiga" in capturado["messages"][0]["content"]
    assert capturado["model"] == config.DEFAULT_MODEL
    brain.think("521", "¿y la integral?")
    roles = [m["role"] for m in capturado["messages"]]
    assert roles == ["system", "user", "assistant", "user"]  # el historial viaja en orden


def test_tope_diario_corta_sin_llamar_al_modelo(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT", "1")
    llamadas = []
    monkeypatch.setattr(client, "complete", lambda *a, **k: llamadas.append(1) or client.LlmResult(ok=True, text="ok"))
    assert brain.think("521", "hola").text == "ok"
    assert brain.think("521", "otra").text == brain.LIMIT_REACHED_TEXT
    assert llamadas == [1]


def test_tope_diario_cero_es_sin_limite(monkeypatch):
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT", "0")
    assert config.daily_message_limit() is None
    monkeypatch.setenv("DAILY_MESSAGE_LIMIT", "")
    assert config.daily_message_limit() == config.DEFAULT_DAILY_MESSAGE_LIMIT


def test_fallo_pasajero_pospone_y_fatal_cae_al_acuse(monkeypatch):
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    resultados = {"r": client.LlmResult(ok=False, retryable=True, reason="saturado")}
    monkeypatch.setattr(client, "complete", lambda *a, **k: resultados["r"])
    pospuestos, enviados = [], []
    monkeypatch.setattr(agent, "fail", lambda j, r: pospuestos.append(r))
    monkeypatch.setattr(agent, "complete", lambda j: None)
    monkeypatch.setattr(agent, "send_text", lambda to, t: enviados.append(t) or client.LlmResult(ok=True))
    agent.handle_job(job())
    assert pospuestos and "saturado" in pospuestos[0] and enviados == []
    resultados["r"] = client.LlmResult(ok=False, retryable=False, reason="llave rechazada")
    agent.handle_job(job())
    assert enviados == [config.auto_reply_text()]


def test_libreta_indexa_y_encuentra(tmp_path, monkeypatch):
    carpeta = tmp_path / "conocimiento"
    carpeta.mkdir()
    (carpeta / "precios.md").write_text("# Precios\n\nLa hogaza campesina cuesta $95.\n\nEl croissant cuesta $45.\n", encoding="utf-8")
    (carpeta / "README.md").write_text("# no se indexa\n", encoding="utf-8")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(carpeta))
    assert index.rebuild() == 1
    notas = index.search("¿Cuánto cuesta la hogaza?")
    assert notas and "hogaza" in notas[0]["chunk"].lower() and notas[0]["title"] == "Precios"
    assert index.search("hola") == []  # solo palabras vacías: no busca


def test_libreta_entra_en_el_prompt_solo_si_esta_encendida(tmp_path, monkeypatch):
    carpeta = tmp_path / "conocimiento"
    carpeta.mkdir()
    (carpeta / "horario.md").write_text("# Horario\n\nAbrimos de lunes a viernes de 7 a 20 horas.\n", encoding="utf-8")
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(carpeta))
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    capturado = {}
    monkeypatch.setattr(client, "complete", lambda messages, *a, **k: capturado.update(sys=messages[0]["content"]) or client.LlmResult(ok=True, text="ok"))
    brain.think("521", "¿qué horario tienen?")
    assert "lunes a viernes" not in capturado["sys"]  # apagada: no se consulta
    monkeypatch.setenv("FEATURE_RAG", "on")
    index.rebuild()
    brain.think("521", "¿qué horario tienen?")
    assert "lunes a viernes" in capturado["sys"]


def test_reintento_de_envio_no_vuelve_a_pensar(monkeypatch):
    monkeypatch.setenv("FEATURE_BRAIN", "on")
    monkeypatch.setenv("OPENROUTER_API_KEY", "llave-falsa")
    from src.queue import store
    from src.webhook.parse import InboundMessage

    store.enqueue(InboundMessage(wamid="w1", sender="521", kind="text", body="hola", timestamp="1"))
    llamadas = []
    monkeypatch.setattr(client, "complete", lambda *a, **k: llamadas.append(1) or client.LlmResult(ok=True, text="pensado una vez"))
    envios = {"n": 0}

    def envio_falla_luego_ok(to, t):
        envios["n"] += 1
        return client.LlmResult(ok=envios["n"] > 1, retryable=True, reason="saturado")

    monkeypatch.setattr(agent, "send_text", envio_falla_luego_ok)
    import time

    real = time.time
    monkeypatch.setattr(store.time, "time", lambda: real() + 3)
    primero = store.claim_ready_job()
    agent.handle_job(primero)              # piensa + falla el envío -> reintento con pausa
    monkeypatch.setattr(store.time, "time", lambda: real() + 400)
    segundo = store.claim_ready_job()
    assert segundo is not None and segundo.reply == "pensado una vez"
    agent.handle_job(segundo)              # reutiliza: no vuelve a llamar al modelo
    assert llamadas == [1] and envios["n"] == 2
    assert store.claim_ready_job() is None  # cerrada


def test_sin_notas_el_prompt_prohibe_inventar(monkeypatch):
    texto = prompt.build_system_prompt([])
    assert "NO tienes notas" in texto
    con_notas = prompt.build_system_prompt([{"title": "Precios", "source": "p.md", "chunk": "Hogaza $95"}])
    assert "Hogaza $95" in con_notas and "NO tienes notas" not in con_notas


def test_prompt_usa_personalidad_del_archivo(tmp_path, monkeypatch):
    archivo = tmp_path / "personalidad.md"
    archivo.write_text("Eres {nombre}, vendes flores.", encoding="utf-8")
    monkeypatch.setattr(config, "PERSONALITY_FILE", str(archivo))
    monkeypatch.setenv("AGENT_NAME", "Flora")
    texto = prompt.build_system_prompt([])
    assert texto.startswith("Eres Flora, vendes flores.") and "Reglas de la casa" in texto
    monkeypatch.setattr(config, "PERSONALITY_FILE", str(tmp_path / "no-existe.md"))
    assert prompt.DEFAULT_PERSONALITY.split("{nombre}")[0] in prompt.build_system_prompt([])
    assert not os.path.exists(str(tmp_path / "no-existe.md"))
