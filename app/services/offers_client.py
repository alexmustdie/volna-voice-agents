"""Клиент внешнего сервиса офферов: УТП по новостройкам для голосового агента.

Сервис читает только свой кэш и не ходит на сайты внутри запроса, поэтому отвечает быстро.
Контракт: GET /utp/match?budget=&rooms=&district=&metro=&motive= -> {best, blocks, collections}.

Из ответа берём только `best` и ровно шесть полей (контракт UtpOffer):
jk_name, title, price_or_threshold, condition, theme, distance_hint. Поля source, url, fetched_at,
score отбрасываем здесь же, чтобы они физически не могли попасть в промпт.

Два уровня оффера:
  1. конкретный ЖК: jk_name + price_or_threshold + condition, иногда distance_hint, title пустой;
  2. подборка из каталога: title + theme + condition, иногда price_or_threshold, jk_name пустой,
     конкретный ЖК называть нельзя.

best: null приходит, когда лид назвал район или метро, а подходящего по гео ЖК нет. Это честный
ответ, а не ошибка: агент переходит на общий оффер и ничего не придумывает. Так же обрабатываем
таймаут и любую ошибку сети: нет оффера, разговор идёт дальше.
"""

import logging
import re
from dataclasses import asdict, dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Ровно эти поля волна забирает из ответа и только их агенту разрешено произносить.
OFFER_FIELDS = ("jk_name", "title", "price_or_threshold", "condition", "theme", "distance_hint")

_LAYOUTS = {
    "0": "студия",
    "1": "однокомнатная квартира",
    "2": "двухкомнатная квартира",
    "3": "трёхкомнатная квартира",
    "4": "четырёхкомнатная квартира",
    "5": "пятикомнатная квартира",
    "6": "шестикомнатная квартира",
    "7": "семикомнатная квартира",
}
# Евроформат: спальни плюс кухня-гостиная. "3Е-к.кв" это две спальни и кухня-гостиная.
_EURO_LAYOUTS = {
    "2": "евродвушка, спальня и кухня-гостиная",
    "3": "евротрёшка, две спальни и кухня-гостиная",
    "4": "еврочетырёшка, три спальни и кухня-гостиная",
    "5": "европятёрка, четыре спальни и кухня-гостиная",
}


def _millions_to_speech(match: re.Match) -> str:
    """'8,2 млн' -> '8 миллионов 200 тысяч рублей': числа модель склоняет сама, сокращения читает
    плохо.
    """
    whole, frac = match.group(1), match.group(2)
    text = f"{whole} миллионов"
    if frac:
        thousands = int(frac.ljust(1, "0")[0]) * 100
        if thousands:
            text += f" {thousands} тысяч"
    return text + " рублей"


def to_speech(text: str | None) -> str:
    """Разворачивает сокращения сервиса офферов в то, что можно произнести вслух."""
    if not text:
        return ""
    out = re.sub(r"(\d+)(?:[.,](\d))?\s*млн", _millions_to_speech, text)
    out = re.sub(
        r"(\d)Е-к\.кв", lambda m: _EURO_LAYOUTS.get(m.group(1), f"евро{m.group(1)}-комнатная"), out
    )
    out = re.sub(
        r"(\d)-к\.кв", lambda m: _LAYOUTS.get(m.group(1), f"{m.group(1)}-комнатная квартира"), out
    )
    out = re.sub(r"\bПВ\s*(\d+)\s*%", r"первоначальный взнос \1 процентов", out)
    out = re.sub(r"\bот\s*(\d+(?:[.,]\d+)?)\s*%", r"ставка от \1 процента", out)
    out = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", r"\1 процентов", out)
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class UtpOffer:
    jk_name: str | None = None
    title: str | None = None
    price_or_threshold: str | None = None
    condition: str | None = None
    theme: str | None = None
    distance_hint: str | None = None

    @classmethod
    def from_api(cls, best: dict | None) -> "UtpOffer | None":
        if not best:
            return None
        offer = cls(**{f: (best.get(f) or None) for f in OFFER_FIELDS})
        return offer if any(asdict(offer).values()) else None

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in OFFER_FIELDS}

    def spoken_dict(self) -> dict:
        """Что можно показать голосовой модели: всё, кроме названия ЖК.

        Название клиенту не произносится никогда: узнав его, он идёт к застройщику напрямую.
        Модель не может назвать то, чего не видела. В базе звонка название остаётся (to_dict)
        для коллеги, который будет связываться с клиентом.
        """
        return {f: v for f, v in self.to_dict().items() if f != "jk_name"}

    @property
    def is_collection(self) -> bool:
        return not self.jk_name and bool(self.title)

    def speech_lines(self) -> list[str]:
        """Оффер словами для промпта: только шесть полей, сокращения развёрнуты."""
        lines: list[str] = []
        if self.jk_name:
            # само название в промпт не попадает, см. spoken_dict
            lines.append(
                "Это конкретный жилой комплекс, но его название и застройщика клиенту не называй, "
                "даже если он просит: скажи, что название сообщит коллега."
            )
        if self.title:
            lines.append(
                f"Подборка: {self.title}. Конкретные жилые комплексы из неё называть нельзя."
            )
        if self.theme:
            lines.append(f"Тема подборки: {to_speech(self.theme)}.")
        if self.price_or_threshold:
            lines.append(f"Цена: {to_speech(self.price_or_threshold)}.")
        if self.condition:
            lines.append(f"Условие: {to_speech(self.condition)}.")
        if self.distance_hint:
            lines.append(f"Расположение: {to_speech(self.distance_hint)}.")
        return lines


async def fetch_offer(
    budget: int | None = None,
    rooms: int | None = None,
    district: str | None = None,
    metro: str | None = None,
    motive: str | None = None,
    timeout: float | None = None,
) -> UtpOffer | None:
    """Оффер из сервиса офферов.

    None при пустом best, таймауте и любой ошибке: агент идёт дальше без УТП.
    """
    settings = get_settings()
    if not settings.offers_url:
        return None
    params = {
        k: v
        for k, v in {
            "budget": budget,
            "rooms": rooms,
            "district": district,
            "metro": metro,
            "motive": motive,
        }.items()
        if v not in (None, "")
    }
    timeout = timeout if timeout is not None else settings.offers_timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{settings.offers_url.rstrip('/')}/utp/match", params=params
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # УТП необязательно, разговор важнее
        log.warning("сервис офферов недоступен (%s): идём без УТП", exc)
        return None
    if not isinstance(data, dict):
        log.warning("сервис офферов вернул не объект: %r", type(data))
        return None
    return UtpOffer.from_api(data.get("best"))
