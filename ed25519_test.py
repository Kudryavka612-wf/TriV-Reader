"""Ed25519 の検証（viewer/ed25519.py）を RFC 8032 の試験ベクトルで確かめる。

自動更新は、この検証が通った update.json しか信じない（updater.verify_manifest）。
検証が甘いと偽の更新を通し、厳しすぎると正しい更新まで入らなくなるので、
RFC 8032 7.1 の TEST 1 / 2 / 3 / SHA(abc) の公開鍵・署名を、作れること・通ること、
1 ビットでも違えば通らないこと、正規でない署名（S >= L）を通さないことを見る。
cryptography が入っていれば（手元・CI の署名の工程）、別の実装とも突き合わせる。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from viewer import ed25519  # noqa: E402

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


# RFC 8032 7.1（秘密鍵, 公開鍵, 本文, 署名）
VECTORS = [
    ("TEST 1",
     "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("TEST 2",
     "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("TEST 3",
     "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("TEST SHA(abc)",
     "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
     "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
     "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def flip(data: bytes, bit: int) -> bytes:
    b = bytearray(data)
    b[bit // 8] ^= 1 << (bit % 8)
    return bytes(b)


def main() -> int:
    for name, sk, pk, msg, sig in VECTORS:
        sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
        check(f"{name}: the public key is derived from the secret key",
              ed25519.public_key(sk) == pk)
        check(f"{name}: the signature matches the RFC", ed25519.sign(sk, msg) == sig)
        check(f"{name}: the RFC signature verifies", ed25519.verify(pk, msg, sig))
        check(f"{name}: a changed message does not verify",
              not ed25519.verify(pk, msg + b"\x00", sig)
              and (not msg or not ed25519.verify(pk, flip(msg, 0), sig)))
        bad_r = all(not ed25519.verify(pk, msg, flip(sig, i)) for i in (0, 77, 255))
        bad_s = all(not ed25519.verify(pk, msg, flip(sig, i)) for i in (256, 300, 500))
        check(f"{name}: a flipped bit in R or S does not verify", bad_r and bad_s)
        check(f"{name}: another key does not verify",
              not ed25519.verify(flip(pk, 3), msg, sig))

    # 正規でない S（S + L）: 同じ本文に別の署名を作れてしまうので通さない
    _name, sk, pk, msg, sig = (bytes.fromhex(x) if i else x for i, x in enumerate(VECTORS[0]))
    s = int.from_bytes(sig[32:], "little") + ed25519._L
    malleated = sig[:32] + s.to_bytes(32, "little")
    check("a non-canonical S (S + L) is refused", not ed25519.verify(pk, msg, malleated))
    # 長さ違い・型違いは例外にせず False
    check("wrong lengths are refused without an exception",
          not ed25519.verify(pk[:31], msg, sig) and not ed25519.verify(pk, msg, sig[:63])
          and not ed25519.verify(pk, msg, sig + b"\x00") and not ed25519.verify(None, msg, sig))
    # 曲線の上に無い公開鍵（y >= p）
    check("a public key that is not a point is refused",
          not ed25519.verify(b"\xff" * 32, msg, sig))

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        print("（cryptography が無いので、別の実装との突き合わせは省きます）")
    else:
        same = True
        for i in range(20):
            seed = os.urandom(32)
            m = os.urandom(i * 13)
            key = Ed25519PrivateKey.from_private_bytes(seed)
            pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                                serialization.PublicFormat.Raw)
            sig2 = key.sign(m)
            same &= (ed25519.public_key(seed) == pub and ed25519.sign(seed, m) == sig2
                     and ed25519.verify(pub, m, sig2))
        check("random keys agree with cryptography (another implementation)", same)

    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
