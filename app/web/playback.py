"""Учёт того, сколько аудио уже отправлено провайдеру телефонии, но ещё не прозвучало.

Зачем: провайдер играет полученные кадры подряд. Когда клиент перебивает агента, модель замолкает,
но всё, что мы уже успели отправить, доиграет в трубку, и агент говорит поверх клиента. Значит при
перебивании очередь надо сбрасывать (у Twilio это сообщение clear).

Сколько именно осталось, знает только отправляющая сторона, поэтому считаем здесь: байты в секунды
по частоте кодека, вычитая уже прошедшее время.
"""

import time

# Байт на секунду звука по кодекам, которые ходят в телефонии и в тестовой странице.
BYTES_PER_SECOND = {
    "audio/pcmu": 8000,  # G.711 mu-law 8 кГц, один байт на сэмпл
    "audio/pcma": 8000,
    "audio/pcm": 48000,  # PCM16 24 кГц (значение уточняется по rate)
}


def bytes_per_second(audio_format: dict | None) -> int:
    fmt = (audio_format or {}).get("type", "audio/pcmu")
    rate = int((audio_format or {}).get("rate") or 8000)
    if fmt == "audio/pcm":
        return rate * 2
    return BYTES_PER_SECOND.get(fmt, 8000)


class PlaybackQueue:
    """Оценка незаигранного хвоста. Не хранит само аудио, только его длительность."""

    def __init__(self, audio_format: dict | None = None, now=time.monotonic) -> None:
        self._rate = bytes_per_second(audio_format)
        self._now = now
        self._plays_until: float = 0.0

    def sent(self, audio_bytes: int) -> None:
        moment = self._now()
        start = max(self._plays_until, moment)
        self._plays_until = start + audio_bytes / self._rate

    @property
    def outstanding_seconds(self) -> float:
        return max(0.0, self._plays_until - self._now())

    def reset(self) -> None:
        self._plays_until = 0.0


def should_clear(event_type: str, outstanding_seconds: float, threshold: float) -> bool:
    """Нужно ли сбросить очередь воспроизведения.

    Явного события о перебивании у GPT-Live нет (проверено по документации 19.09.2026), поэтому
    сигналом служит речь клиента: её транскрипт приходит в ходе разговора. Короткие поддакивания
    не должны рвать фразу агента, поэтому сбрасываем только когда в очереди осталось заметно много.
    У Яндекса есть прямое событие interrupted, ему верим сразу.
    """
    if event_type == "interrupted":
        return True
    return event_type == "input_transcript" and outstanding_seconds > threshold
