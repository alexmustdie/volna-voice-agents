"""Пол голосов: чтобы имя и род персоны не расходились с тем, что слышит собеседник.

У голосов GPT-Live OpenAI пол в документации не описан, поэтому он измерен: основной тон (F0)
образцов, записанных с модели, посчитан автокорреляцией по участкам с речью (скрипт разбора лежал
в `app/web/static/voices`, цифры получены 03.10.2026, 13 голосов, 44-141 окно на голос). Мужской
голос обычно 85-155 Гц, женский 165-255 Гц; ни один голос не попал в промежуток, поэтому деление
однозначное. Это измерение записей, а не заявление OpenAI.

У Яндекса пол указан в документации SpeechKit, измерять не нужно.
"""

# Голос OpenAI -> измеренный основной тон, Гц
LIVE_VOICE_PITCH_HZ: dict[str, int] = {
    "marin": 183,
    "quartz": 229,
    "ripple": 106,
    "vesper": 103,
    "willow": 190,
    "stone": 102,
    "gleam": 182,
    "meridian": 100,
    "bossa": 216,
    "tempo": 108,
    "beacon": 128,
    "delta": 192,
    "cinder": 131,
}

# Голос Яндекса -> пол по документации SpeechKit (docs/speechkit/tts/voices)
YANDEX_VOICE_GENDER: dict[str, str] = {
    "alena": "female",
    "dasha": "female",
    "jane": "female",
    "julia": "female",
    "lera": "female",
    "masha": "female",
    "marina": "female",
    "oksana": "female",
    "omazh": "female",
    "filipp": "male",
    "ermil": "male",
    "zahar": "male",
    "alexander": "male",
    "anton": "male",
    "kirill": "male",
    "madi_ru": "male",
}

MALE_MAX_HZ = 160  # выше этого мужской голос на записях не встречается
FEMALE_MIN_HZ = 175


def voice_gender(voice: str | None) -> str | None:
    """'male', 'female' или None, если голос неизвестен."""
    if not voice:
        return None
    if voice in YANDEX_VOICE_GENDER:
        return YANDEX_VOICE_GENDER[voice]
    pitch = LIVE_VOICE_PITCH_HZ.get(voice)
    if pitch is None:
        return None
    if pitch <= MALE_MAX_HZ:
        return "male"
    return "female" if pitch >= FEMALE_MIN_HZ else None


def voice_note(voice: str | None) -> str:
    """Короткая подпись для панели: пол и, для OpenAI, измеренный тон."""
    gender = voice_gender(voice)
    label = {"male": "мужской", "female": "женский"}.get(gender or "", "пол неизвестен")
    pitch = LIVE_VOICE_PITCH_HZ.get(voice or "")
    return f"{label}, {pitch} Гц" if pitch else label


def mismatch(agent_gender: str | None, voice: str | None) -> bool:
    """Расходится ли пол персоны с полом голоса. Неизвестный голос расхождением не считаем."""
    gender = voice_gender(voice)
    return bool(gender and agent_gender and gender != agent_gender)
