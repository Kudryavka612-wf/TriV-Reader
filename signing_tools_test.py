"""更新の署名の道具（tools/make_signing_key.py・tools/sign_update.py）の回帰テスト。

- 鍵づくり: 秘密鍵はファイルにだけ書き、画面には出さない。公開鍵は version.py に
  そのまま貼れる 1 行で出す。すでにあるファイルは上書きしない。
- 署名: 秘密鍵が無ければ失敗し、update.json.sig を作らない（CI はこれでリリースを
  止める）。作った署名はアプリと同じ検証（updater.verify_manifest）で通る。
  version.py の公開鍵と対でない秘密鍵では失敗する（配ると全 PC が更新を入れられなく
  なるため）。sha256 の無い update.json には署名しない。
cryptography が無い環境（このリポジトリの .venv・CI のテストの工程）では、署名の
部分は純 Python の署名に差し替えて流れを確かめる（CI の署名の工程は cryptography を
入れて本物で行う）。鍵は一時フォルダに作る使い捨て。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

TMP = tempfile.mkdtemp(prefix="trv_signtools_")

from viewer import ed25519, updater  # noqa: E402
from viewer import version as ver  # noqa: E402

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> bool:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
    return bool(ok)


def have_cryptography() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def pure_signer(seed: bytes, data: bytes) -> tuple[bytes, bytes]:
    return ed25519.sign(seed, data), ed25519.public_key(seed)


def run_tool(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
    return subprocess.run([sys.executable, os.path.join(ROOT, "tools", script), *args],
                          capture_output=True, text=True, encoding="utf-8", env=e, timeout=120)


def write_manifest(path: str, **over) -> None:
    m = {"version": "99.0.0",
         "url": f"https://github.com/{ver.GITHUB_REPO}/releases/download/v99.0.0/TriVReader-99.0.0.zip",
         "sha256": hashlib.sha256(b"zip").hexdigest(), "notes": "テスト"}
    m.update(over)
    m = {k: v for k, v in m.items() if v is not None}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def test_make_signing_key() -> tuple[str, str]:
    key_path = os.path.join(TMP, "key.txt")
    r = run_tool("make_signing_key.py", key_path)
    check("the key tool succeeds", r.returncode == 0, r.stderr[-200:])
    secret = open(key_path, encoding="ascii").read().strip() if os.path.exists(key_path) else ""
    seed = base64.b64decode(secret) if secret else b""
    check("…writes a 32-byte private key (base64) to the file", len(seed) == 32)
    check("…never prints the private key", secret and secret not in r.stdout + r.stderr)
    m = re.search(r'^UPDATE_PUBLIC_KEY = "([A-Za-z0-9+/=]{44})"$', r.stdout, re.M)
    check("…prints the public key as a line to paste into version.py", m)
    pub = m.group(1) if m else ""
    check("…and the printed public key belongs to the private key",
          seed and pub == base64.b64encode(ed25519.public_key(seed)).decode())
    check("…and tells how to register the UPDATE_SIGNING_KEY secret",
          "UPDATE_SIGNING_KEY" in r.stdout and "Secrets" in r.stdout)
    before = open(key_path, "rb").read()
    r2 = run_tool("make_signing_key.py", key_path)
    check("the key tool refuses to overwrite an existing key file",
          r2.returncode != 0 and open(key_path, "rb").read() == before)
    return secret, pub


def test_sign_update(secret: str, pub: str) -> None:
    import sign_update

    manifest = os.path.join(TMP, "update.json")
    sig_path = manifest + ".sig"
    write_manifest(manifest)

    # 秘密鍵が無い（Secrets 未登録）→ 失敗し、.sig を作らない
    with open(sig_path, "w") as f:
        f.write("stale")
    env = {k: v for k, v in os.environ.items() if k != "UPDATE_SIGNING_KEY"}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sign_update.py"), manifest],
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env=dict(env, PYTHONIOENCODING="utf-8"))
    check("signing without UPDATE_SIGNING_KEY fails (the release stops)",
          r.returncode != 0 and "UPDATE_SIGNING_KEY" in r.stderr, r.stderr[-200:])
    check("…and leaves no update.json.sig (not even an old one)", not os.path.exists(sig_path))

    r = run_tool("sign_update.py", manifest, env={"UPDATE_SIGNING_KEY": "not a key"})
    check("a malformed key fails without printing it",
          r.returncode != 0 and "not a key" not in r.stdout + r.stderr and not os.path.exists(sig_path))

    signer = sign_update.sign_bytes if have_cryptography() else pure_signer
    if not have_cryptography():
        print("（cryptography が無いので、署名は純 Python に差し替えて流れを確かめます）")
    saved = ver.UPDATE_PUBLIC_KEY
    try:
        ver.UPDATE_PUBLIC_KEY = pub
        out = io.StringIO()
        with redirect_stdout(out):
            path = sign_update.sign_manifest(manifest, secret, signer=signer)
        raw = open(manifest, "rb").read()
        check("a signed update.json verifies the way the app checks it",
              updater.verify_manifest(raw, open(path, "rb").read()))
        check("…and the private key is not printed", secret not in out.getvalue())

        # version.py の公開鍵と対でない秘密鍵 → 失敗（配ると全 PC が更新を入れられない）
        other = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
        err = ""
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                sign_update.sign_manifest(manifest, other, signer=signer)
        except sign_update.SignError as exc:
            err = str(exc)
        check("a private key that does not match UPDATE_PUBLIC_KEY is refused",
              "UPDATE_PUBLIC_KEY" in err and not os.path.exists(sig_path), err[:80])

        write_manifest(manifest, sha256=None)
        err = ""
        try:
            with redirect_stdout(io.StringIO()):
                sign_update.sign_manifest(manifest, secret, signer=signer)
        except sign_update.SignError as exc:
            err = str(exc)
        check("an update.json without sha256 is not signed", err and not os.path.exists(sig_path),
              err[:80])
    finally:
        ver.UPDATE_PUBLIC_KEY = saved

    if have_cryptography():
        # 本物（CI と同じ）: 環境変数の秘密鍵で、コマンドとして
        write_manifest(manifest)
        r = run_tool("sign_update.py", manifest, env={"UPDATE_SIGNING_KEY": secret})
        ok = r.returncode == 0 and os.path.exists(sig_path)
        sig = base64.b64decode(open(sig_path, "rb").read().strip()) if ok else b""
        check("[cryptography] the command signs with the key from UPDATE_SIGNING_KEY",
              ok and ed25519.verify(base64.b64decode(pub), open(manifest, "rb").read(), sig),
              r.stderr[-200:])
        check("[cryptography] …without printing the private key", secret not in r.stdout + r.stderr)


def main() -> int:
    try:
        secret, pub = "", ""
        for t in (test_make_signing_key,):
            try:
                secret, pub = t()
            except Exception as exc:  # noqa: BLE001
                check(f"{t.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")
        for t in (lambda: test_sign_update(secret, pub),):
            try:
                t()
            except Exception as exc:  # noqa: BLE001
                check("a test ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    ok = sum(results)
    print(f"\n==== {ok}/{len(results)} checks passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
