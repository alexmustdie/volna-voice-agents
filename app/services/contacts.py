"""Импорт CSV.

Обязательна одна колонка с номером, остальное необязательно:
    phone, name, district, metro, budget, rooms
Без заголовка файл читается как "phone, name" (так были первые базы).
district, metro, budget, rooms нужны для подбора УТП через внешний сервис офферов до звонка: если их
нет,
подбор идёт только по тому, что агент выяснит в разговоре.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, ContactList
from app.phone import normalize_phone

# Синонимы колонок: базы выгружают и по-русски, и по-английски.
COLUMN_ALIASES: dict[str, str] = {
    "phone": "phone",
    "телефон": "phone",
    "номер": "phone",
    "name": "name",
    "имя": "name",
    "фио": "name",
    "district": "district",
    "район": "district",
    "metro": "metro",
    "метро": "metro",
    "станция": "metro",
    "budget": "budget",
    "бюджет": "budget",
    "rooms": "rooms",
    "комнат": "rooms",
    "комнаты": "rooms",
    "комнатность": "rooms",
}

LeadRow = dict[str, str | int | None]


@dataclass
class ImportResult:
    added: int = 0
    duplicates: int = 0
    invalid: list[str] = field(default_factory=list)
    with_profile: int = 0  # сколько контактов пришли с гео, бюджетом или комнатностью


def parse_budget(raw: str | None) -> int | None:
    """'8 200 000', '8,2 млн', '8.2m', '12000000 руб' -> рубли. Мусор -> None."""
    if not raw:
        return None
    text = str(raw).lower().replace("\xa0", " ").strip()
    match = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    if re.search(r"млн|млн\.|m\b|кк\b", text):
        value *= 1_000_000
    elif re.search(r"тыс|k\b", text):
        value *= 1_000
    elif value < 1000:  # "8,2" без единиц это миллионы: цены в рублях так не пишут
        value *= 1_000_000
    return round(value) if value > 0 else None  # 8.2 * 1e6 в double это 8199999.99


def parse_rooms(raw: str | None) -> int | None:
    """'2', '2к', 'двушка', 'студия', '3Е' -> 0..7. Мусор -> None."""
    if not raw:
        return None
    text = str(raw).lower().strip()
    if "студи" in text or text in ("0", "0к"):
        return 0
    words = {
        "однушк": 1,
        "одноком": 1,
        "двушк": 2,
        "двухком": 2,
        "трёшк": 3,
        "трешк": 3,
        "трёхком": 3,
        "трехком": 3,
        "четырёшк": 4,
        "четырешк": 4,
        "четырёхком": 4,
        "четырехком": 4,
    }
    for stem, value in words.items():
        if stem in text:
            return value
    match = re.search(r"\d+", text)
    if not match:
        return None
    rooms = int(match.group())
    return rooms if 0 <= rooms <= 7 else None


def parse_csv(content: bytes) -> list[LeadRow]:
    """Строки файла как словари с ключами phone, name, district, metro, budget, rooms."""
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in r)]
    if not rows:
        return []

    header = [COLUMN_ALIASES.get(c.strip().lower().rstrip("ы"), "") for c in rows[0]]
    if "phone" in header:
        columns = {name: i for i, name in enumerate(header) if name}
        rows = rows[1:]
    else:
        columns = {"phone": 0, "name": 1}  # файл без заголовка: как в первых базах

    def cell(row: list[str], name: str) -> str | None:
        i = columns.get(name)
        if i is None or i >= len(row):
            return None
        return row[i].strip() or None

    result: list[LeadRow] = []
    for row in rows:
        result.append(
            {
                "phone": cell(row, "phone") or "",
                "name": cell(row, "name"),
                "district": cell(row, "district"),
                "metro": cell(row, "metro"),
                "budget": parse_budget(cell(row, "budget")),
                "rooms": parse_rooms(cell(row, "rooms")),
            }
        )
    return result


async def import_contacts(
    session: AsyncSession, contact_list: ContactList, content: bytes
) -> ImportResult:
    result = ImportResult()
    existing = set(
        (await session.execute(select(Contact.phone).where(Contact.list_id == contact_list.id)))
        .scalars()
        .all()
    )
    seen: set[str] = set()
    for row in parse_csv(content):
        phone = normalize_phone(str(row["phone"]))
        if phone is None:
            result.invalid.append(str(row["phone"]))
            continue
        if phone in existing or phone in seen:
            result.duplicates += 1
            continue
        seen.add(phone)
        session.add(
            Contact(
                list_id=contact_list.id,
                phone=phone,
                name=row["name"],
                district=row["district"],
                metro=row["metro"],
                budget=row["budget"],
                rooms=row["rooms"],
            )
        )
        result.added += 1
        if any(row[k] is not None for k in ("district", "metro", "budget", "rooms")):
            result.with_profile += 1
    await session.commit()
    return result
