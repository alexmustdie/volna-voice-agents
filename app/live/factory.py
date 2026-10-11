"""Выбор голосового движка: openai (GPT-Live-1) | yandex (Yandex AI Studio Realtime) | мок.

Движок принадлежит агенту, а не приложению: голос существует только внутри своего движка, поэтому
движок определяется голосом (engine_for_voice). VOICE_ENGINE из настроек это только значение по
умолчанию для новых агентов и для мест, где агента нет.
"""

from app.config import get_settings
from app.live.base import LiveSession
from app.live.fake import FakeLiveSession
from app.models import LIVE_VOICES

ENGINE_LABELS = {"openai": "OpenAI GPT-Live-1", "yandex": "Яндекс Realtime"}


def _voices_by_engine() -> dict[str, list[str]]:
    from app.live.yandex_live import YANDEX_VOICES

    return {"openai": list(LIVE_VOICES), "yandex": list(YANDEX_VOICES)}


def engine_voices(engine: str | None = None) -> list[str]:
    engine = engine or get_settings().voice_engine
    return _voices_by_engine().get(engine, list(LIVE_VOICES))


def voices_by_engine() -> dict[str, list[str]]:
    return _voices_by_engine()


def engine_for_voice(voice: str | None) -> str | None:
    """Движок, которому принадлежит голос. None, если голос неизвестен."""
    for engine, voices in _voices_by_engine().items():
        if voice in voices:
            return engine
    return None


def engine_has_credentials(engine: str | None = None) -> bool:
    s = get_settings()
    engine = engine or s.voice_engine
    if engine == "yandex":
        return bool(s.yandex_api_key and s.yandex_folder_id)
    return bool(s.openai_api_key)


def create_live_session(
    engine: str | None = None, contact_name: str | None = None, allow_fake: bool = True
) -> LiveSession:
    """Реальный движок, если есть ключи; иначе мок (если разрешён)."""
    engine = engine or get_settings().voice_engine
    if engine_has_credentials(engine):
        if engine == "yandex":
            from app.live.yandex_live import YandexLiveSession

            return YandexLiveSession()
        from app.live.openai_live import OpenAILiveSession

        return OpenAILiveSession()
    if allow_fake:
        return FakeLiveSession(contact_name=contact_name)
    raise RuntimeError(f"нет учётных данных для движка {engine}")
