"""Сброс очереди воспроизведения при перебивании и показ времени в рабочей таймзоне."""

from datetime import UTC, datetime

from app.config import get_settings
from app.web.playback import PlaybackQueue, bytes_per_second, should_clear
from app.web.templating import fmt_dt


def test_bytes_per_second():
    assert bytes_per_second({"type": "audio/pcmu", "rate": 8000}) == 8000
    assert bytes_per_second({"type": "audio/pcma", "rate": 8000}) == 8000
    assert bytes_per_second({"type": "audio/pcm", "rate": 24000}) == 48000
    assert bytes_per_second(None) == 8000


def test_playback_queue_counts_unplayed_tail():
    clock = {"now": 100.0}
    queue = PlaybackQueue({"type": "audio/pcmu", "rate": 8000}, now=lambda: clock["now"])
    assert queue.outstanding_seconds == 0

    queue.sent(8000)  # секунда звука
    assert queue.outstanding_seconds == 1.0
    queue.sent(4000)  # ещё полсекунды в очередь
    assert queue.outstanding_seconds == 1.5

    clock["now"] += 1.0  # секунда проиграла
    assert round(queue.outstanding_seconds, 3) == 0.5
    clock["now"] += 5.0  # всё доиграло, в минус не уходим
    assert queue.outstanding_seconds == 0

    queue.sent(8000)
    queue.reset()  # сброс очереди у провайдера
    assert queue.outstanding_seconds == 0


def test_should_clear_rules():
    # прямое событие модели: сбрасываем всегда
    assert should_clear("interrupted", 0.0, 1.2)
    # клиент заговорил, а в трубке ещё три секунды речи агента
    assert should_clear("input_transcript", 3.0, 1.2)
    # короткое поддакивание в конце фразы очередь не рвёт
    assert not should_clear("input_transcript", 0.4, 1.2)
    # речь самого агента и прочие события ничего не сбрасывают
    assert not should_clear("output_transcript", 5.0, 1.2)
    assert not should_clear("audio", 5.0, 1.2)


def test_panel_shows_moscow_time(monkeypatch):
    """Встреча, о которой агент договорился на 12:00 МСК, лежит в базе как 09:00 UTC."""
    monkeypatch.setattr(get_settings(), "display_timezone", "Europe/Moscow")
    stored = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    assert fmt_dt(stored) == "20.09.2026 12:00"
    # SQLite отдаёт время без таймзоны: считаем его UTC, а не локальным
    assert fmt_dt(datetime(2026, 9, 20, 9, 0)) == "20.09.2026 12:00"
    assert fmt_dt(None) == "–"

    monkeypatch.setattr(get_settings(), "display_timezone", "Europe/Kaliningrad")
    assert fmt_dt(stored) == "20.09.2026 11:00"
    monkeypatch.setattr(get_settings(), "display_timezone", "неизвестная/зона")
    assert fmt_dt(stored) == "20.09.2026 09:00"  # ломаная настройка не должна ронять панель
