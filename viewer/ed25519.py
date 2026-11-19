"""Ed25519 の署名の確かめ（RFC 8032。標準ライブラリだけで動く）。

自動更新が update.json を信じてよいかを、配布元の秘密鍵で付けた署名で確かめる
ために使う（updater.verify_manifest）。凍結した配布版に部品を足さないよう、
外部のライブラリは使わない。計算は RFC 8032 の 6 章（参考実装）に沿っている。

- verify() は公開されている値（公開鍵・署名・本文）しか扱わないので、処理時間が
  入力で変わっても問題ない。
- sign() / public_key() は試験と鍵づくり（tools/make_signing_key.py）用。秘密鍵を
  扱うのに一定時間の計算になっていないので、リリースの署名（CI）には使わない
  （CI は cryptography で署名する。tools/sign_update.py）。

試験は ed25519_test.py（RFC 8032 7.1 の試験ベクトル）。
"""
from __future__ import annotations

import hashlib

# 曲線のパラメータ（RFC 8032 5.1）
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493   # 基点の位数（q / L）
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
SEED_BYTES = 32


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _sha512_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


# 点は拡張座標 (X, Y, Z, T)。x = X/Z, y = Y/Z, x*y = T/Z
def _add(p1: tuple, p2: tuple) -> tuple:
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % _P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % _P
    c = 2 * p1[3] * p2[3] * _D % _P
    d = 2 * p1[2] * p2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, pt: tuple) -> tuple:
    q = (0, 1, 1, 0)   # 単位元
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(p1: tuple, p2: tuple) -> bool:
    # x1/z1 == x2/z2 かつ y1/z1 == y2/z2
    if (p1[0] * p2[2] - p2[0] * p1[2]) % _P != 0:
        return False
    return (p1[1] * p2[2] - p2[1] * p1[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None   # 正規の符号化ではない（y は p 未満でなければならない）
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None   # 曲線の上の点ではない
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt: tuple) -> bytes:
    zinv = _inv(pt[2])
    x = pt[0] * zinv % _P
    y = pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes) -> tuple | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """署名が public_key の持ち主のものとして message に合っていれば True。

    長さ違い・曲線上に無い点・正規でない S（S >= L。署名の書き換えで同じ本文に
    別の署名を作れてしまう）は、例外にせず False を返す。
    """
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != PUBLIC_KEY_BYTES:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_BYTES:
        return False
    public_key, signature, message = bytes(public_key), bytes(signature), bytes(message)
    a = _decompress(public_key)
    if a is None:
        return False
    r_bytes = signature[:32]
    r = _decompress(r_bytes)
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = _sha512_int(r_bytes + public_key + message) % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# --- 以下は試験と鍵づくり用（秘密鍵を扱う。一定時間の計算ではない） -----------
def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != SEED_BYTES:
        raise ValueError("秘密鍵（seed）は 32 バイトです")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    """秘密鍵（32 バイトの seed）から公開鍵（32 バイト）を作る。"""
    a, _prefix = _expand(seed)
    return _compress(_mul(a, _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """試験用の署名（64 バイト）。リリースの署名には使わない（冒頭の説明を参照）。"""
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _sha512_int(prefix + message) % _L
    r_bytes = _compress(_mul(r, _G))
    h = _sha512_int(r_bytes + pub + message) % _L
    s = (r + h * a) % _L
    return r_bytes + int.to_bytes(s, 32, "little")
