"""Пол голоса: измеренный у OpenAI, документированный у Яндекса, и предупреждение о расхождении."""

from app.live.voices import LIVE_VOICE_PITCH_HZ, mismatch, voice_gender, voice_note
from app.models import LIVE_VOICES


def test_every_openai_voice_measured():
    assert set(LIVE_VOICE_PITCH_HZ) == set(LIVE_VOICES)
    # ни один голос не попал в промежуток между мужским и женским диапазоном
    assert all(hz <= 160 or hz >= 175 for hz in LIVE_VOICE_PITCH_HZ.values())


def test_gender_by_voice():
    assert voice_gender("bossa") == "female"  # 216 Гц
    assert voice_gender("ermil") == "male"  # документация Яндекса
    assert voice_gender("meridian") == "male"  # 100 Гц
    assert voice_gender("alena") == "female"
    assert voice_gender("нет такого") is None and voice_gender(None) is None


def test_note_and_mismatch():
    assert voice_note("bossa") == "женский, 216 Гц"
    assert voice_note("ermil") == "мужской"  # у Яндекса тон не мерили
    assert voice_note("нет такого") == "пол неизвестен"
    # агент-мужчина с женским голосом: панель должна это показать
    assert mismatch("male", "bossa") and not mismatch("female", "bossa")
    assert mismatch("female", "ermil") and not mismatch("male", "ermil")
    assert not mismatch("male", "нет такого")  # неизвестный голос не считаем ошибкой
    assert not mismatch(None, "bossa")
