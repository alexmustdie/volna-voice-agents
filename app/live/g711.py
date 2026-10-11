"""G.711 mu-law <-> PCM16 без сторонних зависимостей (audioop удалён из Python 3.13).

Нужен для движков, которые принимают только PCM (Yandex Realtime), при телефонии с mu-law 8 кГц.
"""

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0
    if sample < 0:
        sample = -sample
        sign = 0x80
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _decode_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


_DECODE_TABLE = [_decode_sample(b) for b in range(256)]
_ENCODE_TABLE = [_encode_sample(s) for s in range(-32768, 32768)]


def ulaw_to_pcm16(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        s = _DECODE_TABLE[b]
        out[2 * i] = s & 0xFF
        out[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(out)


def pcm16_to_ulaw(data: bytes) -> bytes:
    n = len(data) // 2
    out = bytearray(n)
    for i in range(n):
        s = data[2 * i] | (data[2 * i + 1] << 8)
        if s >= 0x8000:
            s -= 0x10000
        out[i] = _ENCODE_TABLE[s + 32768]
    return bytes(out)
