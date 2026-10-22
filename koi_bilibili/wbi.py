"""WBI 签名。img_key、sub_key 来自 nav 的 wbi_img。"""

import hashlib
from urllib.parse import urlencode

# fmt: off
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
# fmt: on

_STRIP = set("!'()*")


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    if len(raw) < 64:
        raise ValueError("wbi 密钥长度不够")
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(params: dict[str, object], mixin: str, wts: int) -> dict[str, str]:
    signed = {key: _filter(value) for key, value in params.items()}
    signed["wts"] = str(wts)
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


def _filter(value: object) -> str:
    return "".join(ch for ch in str(value) if ch not in _STRIP)
