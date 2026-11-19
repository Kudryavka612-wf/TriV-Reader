"""update.json に配布元の署名を付ける（<update.json>.sig を作る）。

リリースの CI（.github/workflows/release.yml）が、make_release.py の後に呼ぶ。
手作業で公開するときも、これを通してから update.json.sig を一緒に添付する。

    python tools/sign_update.py <update.json> [--key-file <秘密鍵のファイル>]

秘密鍵: --key-file のファイル、無ければ環境変数 UPDATE_SIGNING_KEY（GitHub の
  リポジトリの Secrets）。中身は Ed25519 の秘密鍵 32 バイト（seed）を base64 にした
  1 行（tools/make_signing_key.py が作る形）。PKCS#8 の PEM
  （-----BEGIN PRIVATE KEY-----）も受け付ける。
署名: cryptography（pip で入れる。凍結した配布版には入れない）で、update.json の
  バイト列そのものに署名し、base64 の 1 行を <update.json>.sig に書く。
確かめ: 書いた署名を、アプリと同じ検証（viewer/ed25519.py・別の実装）で確かめる。
  viewer/version.py の UPDATE_PUBLIC_KEY が入っていれば、秘密鍵がそれと対になって
  いるかも確かめる（対になっていない鍵で配ると、どの PC も以後の更新を入れられなく
  なる）。update.json の中身（sha256・zip の URL）もアプリと同じ基準で確かめる。
  どれかが合わなければ .sig を書かずに失敗する（署名の無い・合わないものを配らない）。
秘密鍵の中身は、成功しても失敗しても表示しない。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from viewer import ed25519, updater  # noqa: E402
from viewer import version as _ver  # noqa: E402

SECRET_NAME = "UPDATE_SIGNING_KEY"


class SignError(Exception):
    """署名できない（理由は表示してよい文面。鍵の中身は含めない）。"""


def load_seed(text: str) -> bytes:
    """秘密鍵の文字列から 32 バイトの seed を取り出す。"""
    text = (text or "").replace("﻿", "").strip()
    if not text:
        raise SignError(f"秘密鍵がありません（{SECRET_NAME}）")
    if text.startswith("-----BEGIN"):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            raise SignError("cryptography が入っていません（pip install cryptography）") from exc
        try:
            key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        except Exception as exc:  # noqa: BLE001  中身は出さない
            raise SignError("秘密鍵（PEM）を読めません（中身は表示しません）") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SignError("秘密鍵が Ed25519 ではありません")
        return key.private_bytes(serialization.Encoding.Raw,
                                 serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    try:
        seed = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignError("秘密鍵の形が正しくありません（base64 ではない。中身は表示しません）") from exc
    if len(seed) != ed25519.SEED_BYTES:
        raise SignError("秘密鍵の長さが正しくありません（32 バイトの base64 のはず）")
    return seed


def sign_bytes(seed: bytes, data: bytes) -> tuple[bytes, bytes]:
    """cryptography で署名する。(署名 64 バイト, 公開鍵 32 バイト)。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise SignError("cryptography が入っていません（pip install cryptography）") from exc
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return key.sign(data), pub


def check_manifest(data: bytes) -> None:
    """update.json の中身が、アプリの受け付ける形か（updater.manifest_problem と同じ基準）。"""
    try:
        info = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SignError("update.json を JSON として読めません") from exc
    problem = updater.manifest_problem(info)
    if problem:
        raise SignError(f"update.json をアプリが受け付けません: {problem}")


def sign_manifest(manifest: str, key_text: str, signer=sign_bytes) -> str:
    """manifest に署名して .sig のパスを返す。合わなければ SignError（.sig は残さない）。"""
    sig_path = manifest + ".sig"
    try:
        os.remove(sig_path)   # 古い署名を残さない
    except OSError:
        pass
    seed = load_seed(key_text)
    with open(manifest, "rb") as f:
        data = f.read()
    check_manifest(data)
    sig, pub = signer(seed, data)
    # 別の実装（アプリと同じ検証）で確かめる
    if not ed25519.verify(pub, data, sig):
        raise SignError("作った署名を確かめられませんでした")
    pub_b64 = base64.b64encode(pub).decode("ascii")
    embedded = str(_ver.UPDATE_PUBLIC_KEY or "").strip()
    if embedded and embedded != pub_b64:
        raise SignError(
            f"{SECRET_NAME} の秘密鍵と viewer/version.py の UPDATE_PUBLIC_KEY が対になって"
            "いません。このまま配ると、どの PC も更新を入れられなくなります。"
            f"（この秘密鍵の公開鍵: {pub_b64}）")
    sig_text = base64.b64encode(sig).decode("ascii") + "\n"
    with open(sig_path, "w", encoding="ascii", newline="\n") as f:
        f.write(sig_text)
    # 書いたファイルを読み直して、もう一度確かめる
    with open(sig_path, "rb") as f:
        again = base64.b64decode(f.read().strip(), validate=True)
    if not ed25519.verify(pub, data, again):
        os.remove(sig_path)
        raise SignError("書いた署名を確かめられませんでした")
    if not embedded:
        print("::warning::viewer/version.py の UPDATE_PUBLIC_KEY が空です。この版のアプリは"
              f"署名を確かめません。公開鍵 {pub_b64} を貼ってください。")
    print(f"署名しました: {sig_path}（公開鍵 {pub_b64}）")
    return sig_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="update.json に配布元の署名を付ける")
    ap.add_argument("manifest", help="update.json のパス")
    ap.add_argument("--key-file", help=f"秘密鍵のファイル（無ければ環境変数 {SECRET_NAME}）")
    args = ap.parse_args(argv)
    try:
        os.remove(args.manifest + ".sig")   # 失敗したときに古い署名が残らないよう、先に消す
    except OSError:
        pass
    try:
        if args.key_file:
            with open(args.key_file, encoding="utf-8-sig") as f:
                key_text = f.read()
        else:
            key_text = os.environ.get(SECRET_NAME, "")
        if not key_text.strip():
            raise SignError(
                f"秘密鍵がありません。リポジトリの Settings > Secrets and variables > Actions に "
                f"{SECRET_NAME} を登録してください（BUILD.md「更新の署名」）。"
                "署名の無い更新は配りません。")
        sign_manifest(args.manifest, key_text)
    except SignError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"::error::ファイルを読み書きできません: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
