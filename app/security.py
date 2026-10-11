"""Токены для WebSocket-эндпоинтов звонка.

Медиа-сокет Twilio и сокет браузерного стенда открываются без cookie панели, поэтому подлинность
соединения подтверждает токен: HMAC-SHA256 от предмета токена на SESSION_SECRET. Twilio получает
его в TwiML (<Parameter name="token">) и возвращает в событии start, браузерный стенд берёт его со
страницы и передаёт в query.

Предмет токена: для медиа-сокета Twilio это идентификатор звонка ("42"), для браузерного стенда
"browser:{agent_id}"; пространства не пересекаются.
"""

import hashlib
import hmac

from app.config import get_settings


def media_token(subject: int | str) -> str:
    key = get_settings().session_secret.encode()
    return hmac.new(key, str(subject).encode(), hashlib.sha256).hexdigest()


def verify_media_token(subject: int | str, token: object) -> bool:
    if not isinstance(token, str) or not token:
        return False
    return hmac.compare_digest(media_token(subject), token)
