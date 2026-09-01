"""El conector de audio de la llamada (WebRTC con aiortc).

Este módulo habla el idioma técnico de la llamada: recibe la propuesta de
conexión (SDP), responde con la nuestra, y mueve el audio en los dos
sentidos a través del relevo (ver relay.py).

Formatos internos, fijos y simples:
- Audio que LLEGA: se convierte a PCM 16 kHz, mono, 16 bits — lo que los
  oídos (VAD + transcripción) necesitan.
- Audio que SALE: la voz entrega PCM 24 kHz; aquí se convierte a 48 kHz
  antes de ponerlo en el aire (el códec de la llamada trabaja a 48 kHz).

El conector usa UN solo relevo por llamada (la dirección TLS fija de
relay.py): el motor de WebRTC solo aprovecha el primer relevo de la lista,
así que mejor uno bien elegido que una lista que no se usa.
"""

import asyncio
import logging
import time
from fractions import Fraction

from src.calls.relay import STUN_URL, TURN_URL, RelayResult

logger = logging.getLogger("agente")

HEAR_RATE = 16000          # lo que oyen el VAD y la transcripción
SPEAK_RATE = 48000         # lo que sale al aire
FRAME_MS = 20
SPEAK_SAMPLES = SPEAK_RATE * FRAME_MS // 1000
HEAR_QUEUE_FRAMES = 500    # ~10 s de audio entrante en espera, máximo


class MediaSession:
    """Una conexión de audio viva. Crear -> answer/offer -> conversar -> close."""

    def __init__(self) -> None:
        self.pc = None
        self.hear_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=HEAR_QUEUE_FRAMES)
        self.speaker = None
        self._reader_task: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()

    # -- armado ------------------------------------------------------------

    def _build_pc(self, relay: RelayResult):
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

        pc = RTCPeerConnection(
            RTCConfiguration(iceServers=[
                RTCIceServer(urls=[STUN_URL]),
                RTCIceServer(urls=[TURN_URL], username=relay.username,
                             credential=relay.credential),
            ])
        )

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            state = pc.connectionState
            logger.info("LLAMADA: conexión de audio en estado '%s'.", state)
            if state == "connected":
                self.connected.set()
            if state in ("failed", "closed"):
                self.closed.set()

        @pc.on("track")
        def _on_track(track) -> None:
            if track.kind == "audio" and self._reader_task is None:
                self._reader_task = asyncio.ensure_future(self._read_incoming(track))

        self.pc = pc
        return pc

    async def _read_incoming(self, track) -> None:
        """Convierte lo que llega a PCM de oídos y lo deja en la fila.

        Si la fila se llena (nadie escucha), se descarta lo más viejo: en una
        conversación en vivo, el audio atrasado ya no sirve.
        """
        import av

        resampler = av.AudioResampler(format="s16", layout="mono", rate=HEAR_RATE)
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    data = bytes(out.planes[0])[: out.samples * 2]
                    if self.hear_queue.full():
                        try:
                            self.hear_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    self.hear_queue.put_nowait(data)
        except Exception:
            # La pista terminó (colgaron) o la conexión murió: fin natural.
            self.closed.set()

    # -- señalización ------------------------------------------------------

    async def answer_inbound(self, offer_sdp: str, relay: RelayResult) -> str | None:
        """Recibe la propuesta de Meta y produce nuestra respuesta SDP."""
        from aiortc import RTCSessionDescription

        pc = self._build_pc(relay)
        self.speaker = _SpeakerTrack()
        pc.addTrack(self.speaker)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)  # aquí se recolectan los candidatos
        return pc.localDescription.sdp

    async def offer_outbound(self, relay: RelayResult) -> str | None:
        """Arma nuestra propuesta SDP para una llamada saliente."""
        pc = self._build_pc(relay)
        self.speaker = _SpeakerTrack()
        pc.addTrack(self.speaker)
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        return pc.localDescription.sdp

    async def complete_outbound(self, answer_sdp: str) -> bool:
        """La persona descolgó: conecta su respuesta SDP a nuestra propuesta."""
        from aiortc import RTCSessionDescription

        if self.pc is None:
            return False
        try:
            await self.pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            return True
        except Exception as exc:
            logger.warning("LLAMADA: no pude conectar la respuesta de audio (%s).", exc)
            return False

    # -- voz saliente ------------------------------------------------------

    def speak(self, pcm_48k: bytes) -> None:
        """Encola audio (PCM 48 kHz mono s16) para decirlo por la llamada."""
        if self.speaker is not None:
            self.speaker.feed(pcm_48k)

    def speaking_seconds_left(self) -> float:
        return self.speaker.seconds_left() if self.speaker is not None else 0.0

    # -- cierre ------------------------------------------------------------

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
        self.closed.set()


def resample_to_speak(pcm: bytes, source_rate: int) -> bytes:
    """Convierte el PCM de la voz (24 kHz) al del aire (48 kHz)."""
    import av

    if source_rate == SPEAK_RATE or not pcm:
        return pcm
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SPEAK_RATE)
    samples = len(pcm) // 2
    frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
    frame.planes[0].update(pcm + b"\x00" * (frame.planes[0].buffer_size - len(pcm)))
    frame.sample_rate = source_rate
    frame.pts = 0
    out = b""
    for piece in resampler.resample(frame):
        out += bytes(piece.planes[0])[: piece.samples * 2]
    for piece in resampler.resample(None):  # vaciar el resto
        out += bytes(piece.planes[0])[: piece.samples * 2]
    return out


class _SpeakerTrack:
    """La bocina: entrega el audio encolado en marcos de 20 ms, a ritmo real.

    Cuando no hay nada que decir, entrega silencio — la llamada sigue viva.
    Hereda de la pista de aiortc en tiempo de carga (import perezoso).
    """

    kind = "audio"

    def __new__(cls):
        from aiortc.mediastreams import MediaStreamTrack

        # La clase real se construye una sola vez, mezclada con aiortc.
        real = type("_SpeakerTrackReal", (MediaStreamTrack,), dict(cls.__dict__))
        real.kind = "audio"
        instance = object.__new__(real)
        MediaStreamTrack.__init__(instance)
        instance._buffer = bytearray()
        instance._lock = None  # el hilo de la voz y el lazo comparten buffer
        instance._start = None
        instance._sent_samples = 0
        instance._pts = 0
        return instance

    def feed(self, pcm_48k: bytes) -> None:
        self._buffer.extend(pcm_48k)

    def seconds_left(self) -> float:
        return len(self._buffer) / 2 / SPEAK_RATE

    async def recv(self):
        import av

        if self._start is None:
            self._start = time.time()
        # Ritmo real: un marco cada 20 ms, ni antes ni después.
        self._sent_samples += SPEAK_SAMPLES
        wait = self._start + self._sent_samples / SPEAK_RATE - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        need = SPEAK_SAMPLES * 2
        chunk = bytes(self._buffer[:need])
        del self._buffer[:need]
        if len(chunk) < need:
            chunk += b"\x00" * (need - len(chunk))  # silencio
        frame = av.AudioFrame(format="s16", layout="mono", samples=SPEAK_SAMPLES)
        frame.planes[0].update(chunk)
        frame.sample_rate = SPEAK_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SPEAK_RATE)
        self._pts += SPEAK_SAMPLES
        return frame
