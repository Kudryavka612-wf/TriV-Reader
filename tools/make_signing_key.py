"""更新の署名に使う鍵の対（Ed25519）を作る。最初の 1 回だけ、配布元（オーナー）が使う。

    python tools\\make_signing_key.py <秘密鍵を書き出すファイル>

- 秘密鍵（32 バイトの seed を base64 にした 1 行）は、指定したファイルにだけ書く。
  画面には出さない。すでにあるファイルには上書きしない（鍵を取り違えないため）。
- 画面には、viewer/version.py にそのまま貼る公開鍵の 1 行と、GitHub の Secrets
  （UPDATE_SIGNING_KEY）への登録のしかたを出す。
- 追加の部品は要らない（標準ライブラリと viewer/ed25519.py だけ）。乱数は secrets
  （OS の暗号用の乱数）から取る。
"""
from __future__ import annotations

import base64
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from viewer import ed25519  # noqa: E402

SECRET_NAME = "UPDATE_SIGNING_KEY"


def generate(path: str) -> str:
    """鍵の対を作り、秘密鍵を path に書いて、公開鍵（base64）を返す。"""
    seed = secrets.token_bytes(ed25519.SEED_BYTES)
    pub = ed25519.public_key(seed)
    # 作った対で署名して確かめてから書く
    probe = b"TriV-Reader update signing key check"
    if not ed25519.verify(pub, probe, ed25519.sign(seed, probe)):
        raise RuntimeError("作った鍵を確かめられませんでした")
    # O_EXCL: すでにあるファイルには書かない
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64encode(seed) + b"\n")
    return base64.b64encode(pub).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0].startswith("-"):
        print(__doc__)
        return 2
    path = os.path.abspath(argv[0])
    if os.path.exists(path):
        print(f"すでにファイルがあります（上書きしません）: {path}", file=sys.stderr)
        return 2
    try:
        pub = generate(path)
    except OSError as exc:
        print(f"秘密鍵を書けません: {exc}", file=sys.stderr)
        return 2
    print(f"""秘密鍵を書きました（画面には出しません）: {path}

1) viewer/version.py の UPDATE_PUBLIC_KEY を、次の 1 行に置き換えてください:

UPDATE_PUBLIC_KEY = "{pub}"

2) 秘密鍵を GitHub に登録します（リポジトリのオーナーが行う）:
   リポジトリの Settings > Secrets and variables > Actions > New repository secret
     Name  : {SECRET_NAME}
     Secret: 上のファイルの中身（44 文字の 1 行）をそのまま貼る
   gh で入れるときは（PowerShell。パイプ | で渡すと BOM が混ざることがあるので --body で）:
     gh secret set {SECRET_NAME} --repo <オーナー/リポジトリ> --body (Get-Content -Raw "{path}")

3) 登録したら、秘密鍵のファイルは USB メモリなど PC の外に控えを 1 つ残し、この PC
   からは消してください（GitHub の Secrets は後から読み出せません。控えを失うと、
   配った版はそれ以後の更新を入れられなくなり、インストーラーで入れ直すことになります）。
   秘密鍵はメール・チャット・共有フォルダに置かないでください。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
