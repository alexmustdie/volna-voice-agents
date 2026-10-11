from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SESSION_SECRET_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # секреты
    # ключ подписи cookie-сессии панели и токенов медиа-сокетов (app.security); обязателен,
    # значения по умолчанию нет, в реальном режиме не короче MIN_SESSION_SECRET_LENGTH
    session_secret: str
    openai_api_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    database_url: str = "postgresql+asyncpg://volna:volna@localhost:5432/volna"

    # по умолчанию реальный режим: симуляция включается явно (FAKE_CALLS=true в .env.example)
    fake_calls: bool = False
    public_base_url: str = "http://localhost:8000"
    worker_enabled: bool = True

    # голосовой движок: openai (GPT-Live-1) или yandex (Yandex AI Studio Realtime, родной русский)
    voice_engine: str = "openai"
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_realtime_url: str = "wss://ai.api.cloud.yandex.net/v1/realtime"
    yandex_realtime_model: str = "speech-realtime-260528"

    # разбор звонка после разговора: встреча, итог, "не звонить" по транскрипту
    post_call_analysis: bool = True
    analysis_yandex_model: str = "yandexgpt/latest"

    live_model: str = "gpt-live-1"
    backend_model: str = "gpt-5.6-luna"
    openai_live_ws_url: str = "wss://api.openai.com/v1/live/sessions"

    # в какой таймзоне панель показывает время: в БД всё хранится в UTC
    display_timezone: str = "Europe/Moscow"
    # перебивание: если в буфере провайдера осталось больше этого, при речи клиента сбрасываем
    # очередь
    barge_in_clear_seconds: float = 1.2

    # внешний сервис офферов (HTTP API): УТП по новостройкам. Пусто -> волна работает без УТП.
    offers_url: str = ""
    offers_timeout_seconds: float = 0.4  # запрос во время разговора: пауза слышна
    offers_prefetch_timeout_seconds: float = 2.0  # запрос до набора номера: спешить некуда

    # Суточный лимит звонков с одного нашего номера: у площадок объявлений свои ограничения.
    # 0 = без лимита. Пауза между звонками с одного номера сглаживает всплески.
    daily_calls_per_number: int = 0
    min_seconds_between_calls: float = 0.0

    max_retries: int = 2
    retry_interval_seconds: int = 900
    call_timeout_seconds: int = 1800
    ringing_timeout_seconds: int = 120
    worker_poll_seconds: float = 5.0

    fake_answer_rate: float = 0.7
    fake_meeting_rate: float = 0.4
    fake_call_seconds: float = 6.0

    twilio_validate_signature: bool = True

    @model_validator(mode="after")
    def _require_secret_in_real_mode(self) -> "Settings":
        if not self.fake_calls and len(self.session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise ValueError(
                "SESSION_SECRET при FAKE_CALLS=false обязателен и не короче "
                f"{MIN_SESSION_SECRET_LENGTH} символов: им подписываются cookie панели и токены "
                'медиа-сокетов; сгенерировать: python -c "import secrets; '
                'print(secrets.token_hex(32))"'
            )
        return self

    @property
    def public_ws_base(self) -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :]
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :]
        return base


@lru_cache
def get_settings() -> Settings:
    return Settings()
