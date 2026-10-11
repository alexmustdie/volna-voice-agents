import phonenumbers
from phonenumbers import NumberParseException


def normalize_phone(raw: str | None, default_region: str = "RU") -> str | None:
    """Приводит номер к E.164. Возвращает None, если номер невалиден."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = phonenumbers.parse(text, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
