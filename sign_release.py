"""配布物（TriVReader.exe / Setup.exe）にコード署名する。証明書が無ければ何もしない。

署名は任意の工程。リリースの CI（.github/workflows/release.yml）は、リポジトリの Secrets に
証明書が入っているときだけ署名する。入っていなければ、これまでどおり署名なしで配る。

    python sign_release.py --optional  <ファイル>...      # 証明書があれば署名、無ければ何もしない
    python sign_release.py --pfx 証明書.pfx  <ファイル>...   # 手元で署名（パスワードは環境変数）

証明書は次のどちらかで渡す（PFX 形式）。パスワードは CODESIGN_PFX_PASSWORD（互換のため
TRIVREADER_PFX_PW も読む）。
  --pfx <パス>            手元の PFX ファイル
  CODESIGN_PFX_BASE64     PFX を base64 にした文字列（CI の Secrets 向け）

署名には signtool（Windows SDK。GitHub の Windows ランナーには入っている）を使う。見つか
らないときは PowerShell の Set-AuthenticodeSignature で署名する（SDK が無い PC でも動く）。
署名したあと、ファイルに証明書本人の署名が付いたことを確かめる。確かめられなければ失敗にする
（署名したつもりで署名なしのものを配らない）。

自己署名の証明書でも署名はできるが、SmartScreen の警告は、証明書を入れていない PC では消えない
（正式には有料のコード署名証明書が要る。BUILD.md の「コード署名」を参照）。
"""
from __future__ import annotations

import argparse
import base64
import glob
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"

# 署名したファイルの署名者を調べる（トラスト判定ではなく「証明書本人の署名があるか」）。
# 自己署名の証明書は信頼されない扱い（UnknownError）になるが、署名自体は付いている。
_PS_VERIFY = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
    $env:TRV_PFX, $env:TRV_PW)
$bad = 0
foreach ($f in ($env:TRV_FILES -split "`n")) {
    if (-not $f) { continue }
    $s = Get-AuthenticodeSignature -LiteralPath $f
    $ok = ($s.SignerCertificate -ne $null) -and ($s.SignerCertificate.Thumbprint -eq $cert.Thumbprint)
    Write-Output ("{0}: {1} ({2})" -f $(if ($ok) { "署名あり" } else { "署名なし" }), $f, $s.Status)
    if (-not $ok) { $bad++ }
}
if ($bad -gt 0) { exit 1 }
"""

_PS_SIGN = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
    $env:TRV_PFX, $env:TRV_PW)
foreach ($f in ($env:TRV_FILES -split "`n")) {
    if (-not $f) { continue }
    if ($env:TRV_TS) {
        $r = Set-AuthenticodeSignature -LiteralPath $f -Certificate $cert -HashAlgorithm SHA256 `
             -TimestampServer $env:TRV_TS
    } else {
        $r = Set-AuthenticodeSignature -LiteralPath $f -Certificate $cert -HashAlgorithm SHA256
    }
    if (-not $r.SignerCertificate) { Write-Error "署名できません: $f ($($r.StatusMessage))"; exit 1 }
}
"""


def find_signtool() -> str | None:
    """signtool.exe を探す（PATH → Windows SDK の新しい版から順に）。"""
    found = shutil.which("signtool") or shutil.which("signtool.exe")
    if found:
        return found
    cands: list[str] = []
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if root:
            cands += glob.glob(os.path.join(root, "Windows Kits", "10", "bin", "*", "x64",
                                            "signtool.exe"))

    def version_key(path: str) -> list[int]:
        ver = path.split(os.sep)[-3]   # ...\bin\10.0.22621.0\x64\signtool.exe
        return [int(p) if p.isdigit() else 0 for p in ver.split(".")]

    cands.sort(key=version_key, reverse=True)
    return cands[0] if cands else None


def _powershell(script: str, env: dict) -> subprocess.CompletedProcess:
    exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    with tempfile.TemporaryDirectory(prefix="trv_sign_") as d:
        path = os.path.join(d, "run.ps1")
        with open(path, "w", encoding="utf-8-sig") as f:   # BOM つき: 日本語を文字化けさせない
            f.write(script)
        return subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **env})


def sign(files: list[str], pfx: str, password: str, timestamp: str = DEFAULT_TIMESTAMP,
         method: str = "auto", verify: bool = True) -> None:
    """files を pfx で署名する。失敗したら RuntimeError。

    method: "auto"（signtool があれば signtool、無ければ PowerShell）/ "signtool" / "powershell"
    """
    for f in files:
        if not os.path.isfile(f):
            raise RuntimeError(f"署名するファイルがありません: {f}")
    signtool = find_signtool() if method in ("auto", "signtool") else None
    if method == "signtool" and not signtool:
        raise RuntimeError("signtool が見つかりません（Windows SDK を入れてください）")
    env = {"TRV_PFX": pfx, "TRV_PW": password, "TRV_TS": timestamp,
           "TRV_FILES": "\n".join(os.path.abspath(f) for f in files)}
    if signtool:
        cmd = [signtool, "sign", "/fd", "SHA256", "/f", pfx]
        if password:
            cmd += ["/p", password]
        if timestamp:
            cmd += ["/tr", timestamp, "/td", "SHA256"]
        cmd += [os.path.abspath(f) for f in files]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            detail = proc.stdout + proc.stderr
            if password:
                detail = detail.replace(password, "***")   # 秘密を表示に出さない
            raise RuntimeError("signtool が失敗しました:\n" + detail)
    else:
        proc = _powershell(_PS_SIGN, env)
        if proc.returncode != 0:
            raise RuntimeError("PowerShell での署名に失敗しました:\n" + proc.stdout + proc.stderr)
    if verify:
        proc = _powershell(_PS_VERIFY, env)
        print(proc.stdout.strip())
        if proc.returncode != 0:
            raise RuntimeError("署名したはずのファイルに、証明書の署名が付いていません:\n"
                               + proc.stdout + proc.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TriV-Reader の配布物にコード署名する")
    ap.add_argument("files", nargs="+", help="署名するファイル（exe など）")
    ap.add_argument("--pfx", help="PFX 証明書のパス（無ければ環境変数 CODESIGN_PFX_BASE64）")
    ap.add_argument("--timestamp", default=os.environ.get("CODESIGN_TIMESTAMP_URL",
                                                          DEFAULT_TIMESTAMP),
                    help="タイムスタンプサーバー（空文字で付けない）")
    ap.add_argument("--optional", action="store_true",
                    help="証明書が無ければ何もせず成功で終わる（CI 用）")
    ap.add_argument("--method", choices=("auto", "signtool", "powershell"), default="auto")
    ap.add_argument("--no-verify", action="store_true", help="署名後の確認を省く（試験用）")
    args = ap.parse_args(argv)

    password = os.environ.get("CODESIGN_PFX_PASSWORD") or os.environ.get("TRIVREADER_PFX_PW", "")
    pfx = args.pfx
    tmp_dir = None
    try:
        if not pfx and os.environ.get("CODESIGN_PFX_BASE64", "").strip():
            tmp_dir = tempfile.mkdtemp(prefix="trv_cert_")
            pfx = os.path.join(tmp_dir, "cert.pfx")
            with open(pfx, "wb") as f:
                f.write(base64.b64decode(os.environ["CODESIGN_PFX_BASE64"]))
        if not pfx:
            msg = "コード署名の証明書が設定されていないので、署名しません"
            if args.optional:
                print(msg)
                return 0
            print(msg + "（--pfx か CODESIGN_PFX_BASE64 が要ります）", file=sys.stderr)
            return 2
        if not os.path.isfile(pfx):
            print(f"証明書が見つかりません: {pfx}", file=sys.stderr)
            return 2
        try:
            sign(args.files, pfx, password, args.timestamp, args.method, not args.no_verify)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("コード署名しました:", ", ".join(args.files))
        return 0
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)   # 証明書の複製を残さない


if __name__ == "__main__":
    raise SystemExit(main())
