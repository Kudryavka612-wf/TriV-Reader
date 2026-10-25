"""コード署名の補助（sign_release.py）の確認。

署名は任意の工程で、証明書が無いときは黙って署名なしで配る。ここでは、
(1) 証明書が無ければ何も変えずに成功する／必須にすれば失敗する、(2) 自己署名の証明書で実際に
署名でき、署名者が証明書本人だと確かめられる（日本語のフォルダ名でも）、(3) 証明書の
パスワードが違えば失敗する、(4) signtool を使うときの引数、(5) base64 の証明書を渡しても
一時ファイルを残さない、を見る。実際の署名には PowerShell の経路を使う（signtool の実機での
署名は、Windows SDK の入った環境が要るので、ここでは引数だけを見る）。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import sign_release

results: list[bool] = []


def check(name: str, ok, extra: str = "") -> None:
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")


def make_pfx(path: str, password: str) -> None:
    """自己署名のコード署名証明書を作って PFX に書く（証明書ストアには触れない）。"""
    ps = r"""
$ErrorActionPreference = "Stop"
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest(
    "CN=TriV-Reader Test", $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$oids = New-Object System.Security.Cryptography.OidCollection
[void]$oids.Add((New-Object System.Security.Cryptography.Oid("1.3.6.1.5.5.7.3.3")))
$req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension($oids, $true)))
$req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509KeyUsageExtension(
    [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature, $true)))
$cert = $req.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddDays(2))
[System.IO.File]::WriteAllBytes($env:TRV_OUT, $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, $env:TRV_PW))
"""
    ps_path = path + ".ps1"
    with open(ps_path, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", ps_path], capture_output=True, text=True, errors="replace",
                       env={**os.environ, "TRV_OUT": path, "TRV_PW": password})
    os.remove(ps_path)
    if r.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError("試験用の証明書を作れません:\n" + r.stdout + r.stderr)


def has_signature(path: str) -> bool:
    exe = shutil.which("powershell") or "powershell"
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                        "(Get-AuthenticodeSignature -LiteralPath $env:TRV_F).SignerCertificate -ne $null"],
                       capture_output=True, text=True, env={**os.environ, "TRV_F": path})
    return r.stdout.strip() == "True"


def run_main(argv: list[str], env: dict | None = None) -> tuple[int, str]:
    """main() を、環境変数を差し替えて呼ぶ。"""
    saved = {k: os.environ.get(k) for k in ("CODESIGN_PFX_BASE64", "CODESIGN_PFX_PASSWORD",
                                            "TRIVREADER_PFX_PW", "CODESIGN_TIMESTAMP_URL")}
    for k in saved:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sign_release.main(argv)
        return rc, out.getvalue() + err.getvalue()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def sample_exe() -> str:
    """署名のない、署名してよい exe（pip が同梱している実行ファイルの雛形）。

    Windows 標準の exe（notepad など）は Microsoft の署名つきで、署名を付け替えられない。
    """
    import pip

    return os.path.join(os.path.dirname(pip.__file__), "_vendor", "distlib", "t64.exe")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trv_signtest_")
    try:
        # 日本語のフォルダ名の下に、署名してよい適当な exe を置く
        sub = os.path.join(tmp, "試験 フォルダ")
        os.makedirs(sub)
        exe = os.path.join(sub, "対象.exe")
        shutil.copy2(sample_exe(), exe)
        check("(前提) the sample exe starts out unsigned", not has_signature(exe))

        # --- 証明書が無い -------------------------------------------------------------
        rc, out = run_main(["--optional", exe])
        check("no certificate + --optional: succeeds and leaves the file alone",
              rc == 0 and "署名しません" in out and not has_signature(exe), out.strip())
        rc, out = run_main([exe])
        check("no certificate without --optional: fails (so a release cannot silently skip it)",
              rc == 2, f"(rc {rc})")

        # --- 自己署名の証明書で実際に署名 ------------------------------------------------
        pfx = os.path.join(tmp, "test.pfx")
        make_pfx(pfx, "pw-試験")
        rc, out = run_main(["--pfx", pfx, "--timestamp", "", "--method", "powershell", exe],
                           {"CODESIGN_PFX_PASSWORD": "pw-試験"})
        check("signs with a self-signed certificate (Japanese folder name too)",
              rc == 0 and has_signature(exe), out.strip()[-300:])
        check("...and reports that the file now carries the certificate's signature",
              "署名あり" in out, out.strip()[-200:])

        # --- 間違ったパスワード ------------------------------------------------------------
        other = os.path.join(sub, "別の対象.exe")
        shutil.copy2(exe, other)
        # 署名済みのコピーなので、署名の有無ではなく終了コードだけを見る
        rc, out = run_main(["--pfx", pfx, "--timestamp", "", "--method", "powershell", other],
                           {"CODESIGN_PFX_PASSWORD": "wrong"})
        check("a wrong certificate password fails (non-zero) instead of passing unsigned files on",
              rc == 1, f"(rc {rc})")

        # --- base64 の証明書（CI の Secrets の形）と、一時ファイルの掃除 ----------------------
        fresh = os.path.join(sub, "新しい対象.exe")
        shutil.copy2(sample_exe(), fresh)
        before = set(os.listdir(tempfile.gettempdir()))
        with open(pfx, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        rc, out = run_main(["--timestamp", "", "--method", "powershell", fresh],
                           {"CODESIGN_PFX_BASE64": b64, "CODESIGN_PFX_PASSWORD": "pw-試験"})
        leftover = [n for n in set(os.listdir(tempfile.gettempdir())) - before
                    if n.startswith(("trv_cert_", "trv_sign_"))]
        check("a base64 certificate (CI secret) signs the file", rc == 0 and has_signature(fresh),
              out.strip()[-200:])
        check("...and no copy of the certificate is left in the temp folder", not leftover,
              str(leftover))

        # --- 無い・壊れた指定 ----------------------------------------------------------------
        rc, _ = run_main(["--pfx", os.path.join(tmp, "nothing.pfx"), exe])
        check("a missing --pfx file is an error", rc == 2)
        rc, out = run_main(["--pfx", pfx, "--timestamp", "", "--method", "powershell",
                            os.path.join(sub, "ない.exe")], {"CODESIGN_PFX_PASSWORD": "pw-試験"})
        check("a missing file to sign is an error", rc == 1 and "ありません" in out, out.strip())

        # --- signtool を使うときの引数 -----------------------------------------------------------
        log = os.path.join(tmp, "signtool_args.txt")
        fake_dir = os.path.join(tmp, "fakebin")
        os.makedirs(fake_dir)
        with open(os.path.join(fake_dir, "signtool.cmd"), "w", encoding="ascii") as f:
            f.write('@echo off\r\necho %*> "%TRV_LOG%"\r\nexit /b 0\r\n')
        old_path = os.environ["PATH"]
        os.environ["PATH"] = fake_dir + os.pathsep + old_path
        os.environ["TRV_LOG"] = log
        try:
            tgt = os.path.join(tmp, "signtool_target.exe")
            shutil.copy2(exe, tgt)
            rc, out = run_main(["--pfx", pfx, "--method", "signtool", "--no-verify", tgt],
                               {"CODESIGN_PFX_PASSWORD": "pw-試験"})
            args = open(log, encoding="utf-8", errors="replace").read().strip() if os.path.exists(log) else ""
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("TRV_LOG", None)
        expect = ["sign", "/fd SHA256", f"/f {pfx}", "/p pw-", "/tr http://timestamp.digicert.com",
                  "/td SHA256", tgt]
        check("signtool is called as: sign /fd SHA256 /f <pfx> /p <pw> /tr <url> /td SHA256 <file>",
              rc == 0 and all(e in args for e in expect), args)
        check("the signtool lookup returns an exe path, or None when no SDK is installed",
              sign_release.find_signtool() is None or sign_release.find_signtool().lower().endswith("signtool.exe"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n==== {n}/{len(results)} checks passed ====")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
