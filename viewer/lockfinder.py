"""フォルダを「使用中」にしているアプリを調べる（Windows）。

エクスプローラーで「ファイルは使用中です」と言われてフォルダを移動・削除できない
とき、何がつかんでいるかを探す。調べるのは 2 通り。

1. フォルダの中のファイルを開いているアプリ。Windows の Restart Manager
   （インストーラーが「このアプリを閉じてください」と出すときと同じ仕組み）に
   ファイルの一覧を渡して聞く。
2. フォルダを「作業フォルダ（カレントディレクトリ）」にしているアプリ。ファイル
   を開いていなくても、これだけでフォルダは移動できなくなる（コマンドの画面・
   そのフォルダから起動したアプリなど）。各アプリのメモリから作業フォルダを読む。

管理者として動いているアプリ（ウイルス対策・システムのサービス等）の作業フォルダは
読めない。フォルダを監視しているだけの同期ソフトなどは、どちらにも出ないことがある。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

MAX_FILES = 5000

# よく出てくるアプリの説明と、解放のしかた（実行ファイル名の小文字 → 文）
HINTS = {
    "prevhost.exe": "エクスプローラーのプレビュー（右側の表示欄）で表示中です。"
                    "エクスプローラーの［表示］→［プレビュー ウィンドウ］で閉じるか、"
                    "別のファイルを選ぶと解放されます。",
    "msedge.exe": "Microsoft Edge です（エクスプローラーの PDF のプレビューもこれを使います）。"
                  "Edge で開いている PDF を閉じるか、エクスプローラーのプレビューを閉じてください。",
    "msedgewebview2.exe": "エクスプローラーの PDF のプレビュー（Microsoft Edge の部品）です。"
                          "プレビュー ウィンドウを閉じるか、別のファイルを選んでください。",
    "explorer.exe": "エクスプローラーです（縮小表示の作成中など）。少し待つか、"
                    "そのフォルダを表示しているウィンドウを閉じてから試してください。",
    "searchprotocolhost.exe": "Windows の検索の索引作成です。少し待つと解放されます。",
    "searchindexer.exe": "Windows の検索の索引作成です。少し待つと解放されます。",
    "searchfilterhost.exe": "Windows の検索の索引作成です。少し待つと解放されます。",
    "msmpeng.exe": "Windows セキュリティ（ウイルスの検査）です。少し待つと解放されます。",
    "onedrive.exe": "OneDrive の同期です。同期が終わると解放されます。",
    "cmd.exe": "コマンド プロンプトがこのフォルダを作業フォルダにしています。"
               "その画面を閉じるか、別のフォルダへ移ってください（cd）。",
    "powershell.exe": "PowerShell がこのフォルダを作業フォルダにしています。"
                      "その画面を閉じるか、別のフォルダへ移ってください（cd）。",
    "pwsh.exe": "PowerShell がこのフォルダを作業フォルダにしています。"
                "その画面を閉じるか、別のフォルダへ移ってください（cd）。",
    "pdfxedit.exe": "PDF-XChange Editor で開いています。そのファイルを閉じると解放されます。",
    "acrobat.exe": "Adobe Acrobat で開いています。そのファイルを閉じると解放されます。",
    "acrord32.exe": "Adobe Acrobat Reader で開いています。そのファイルを閉じると解放されます。",
    "trivreader.exe": "TriV-Reader です。そのファイルのタブを閉じると解放されます。",
}
DEFAULT_HINT = "このアプリで開いているファイルを閉じるか、アプリを終了すると解放されます。"
CWD_HINT = "このアプリがフォルダを作業フォルダにしています。アプリを終了すると解放されます。"


def hint_for(exe: str) -> str:
    return HINTS.get((exe or "").lower(), DEFAULT_HINT)


def list_files(folder: str, limit: int = MAX_FILES) -> tuple[list[str], bool]:
    """フォルダの中のファイル（サブフォルダも）。(一覧, 多すぎて途中までか)。"""
    out: list[str] = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            out.append(os.path.join(root, name))
            if len(out) >= limit:
                return out, True
    return out, False


# --- Windows のプロセス一覧 ------------------------------------------------
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def processes() -> dict[int, str]:
    """プロセス ID → 実行ファイル名。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    out: dict[int, str] = {}
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = e.szExeFile
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


# --- 1. ファイルを開いているアプリ（Restart Manager）---------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", _FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", _RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64),
                ("ApplicationType", wintypes.DWORD), ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD), ("bRestartable", wintypes.BOOL)]


def _rm_query(files: list[str]) -> dict[int, str]:
    """files のどれかを開いているプロセス（ID → アプリの表示名）。"""
    rm = ctypes.WinDLL("rstrtmgr")
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return {}
    try:
        arr = (wintypes.LPCWSTR * len(files))(*files)
        if rm.RmRegisterResources(session, len(files), arr, 0, None, 0, None) != 0:
            return {}
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD()
        res = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                           ctypes.byref(reasons))
        if res == 0 or needed.value == 0:
            return {}
        if res != 234:   # ERROR_MORE_DATA 以外
            return {}
        info = (_RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), info,
                        ctypes.byref(reasons)) != 0:
            return {}
        return {info[i].Process.dwProcessId: info[i].strAppName for i in range(count.value)}
    finally:
        rm.RmEndSession(session)


CHUNK = 50   # Restart Manager に一度に聞くファイルの数


def file_users(files: list[str], progress=None, cancelled=None) -> dict[int, dict]:
    """ファイルを開いているプロセス → {"app": 表示名, "files": [開いているファイル]}。

    Restart Manager はファイル 1 つにつき十数ミリ秒かかる（フォルダが大きいと数十秒）。
    一度に聞くのは CHUNK 個ずつにして、誰かが開いている塊の中だけを 1 つずつ聞き直し、
    どのファイルかを突き止める。progress(済んだ数, 全部の数)、cancelled() が真なら途中で止める。
    """
    found: dict[int, dict] = {}
    total = len(files)
    for i in range(0, total, CHUNK):
        if cancelled and cancelled():
            break
        chunk = files[i:i + CHUNK]
        holders = _rm_query(chunk)
        for pid, app in holders.items():
            found.setdefault(pid, {"app": app, "files": []})
        if holders and len(chunk) == 1:
            for pid in holders:
                found[pid]["files"].append(chunk[0])
        elif holders:
            for path in chunk:
                if cancelled and cancelled():
                    break
                for pid in _rm_query([path]):
                    found.setdefault(pid, {"app": "", "files": []})["files"].append(path)
        if progress:
            progress(min(i + CHUNK, total), total)
    return found


# --- 2. 作業フォルダにしているアプリ -------------------------------------
class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


def _read(k32, handle, address: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buf, size,
                                 ctypes.byref(got)) or got.value != size:
        return None
    return buf.raw


def process_cwd(pid: int) -> str | None:
    """プロセスの作業フォルダ（読めなければ None）。64bit／32bit のアプリどちらも。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)   # QUERY_INFORMATION | VM_READ
    if not h:
        return None
    try:
        wow_peb = ctypes.c_void_p()
        if nt.NtQueryInformationProcess(h, 26, ctypes.byref(wow_peb),   # Wow64Information
                                        ctypes.sizeof(wow_peb), None) == 0 and wow_peb.value:
            raw = _read(k32, h, wow_peb.value + 0x10, 4)              # PEB32.ProcessParameters
            if raw is None:
                return None
            params = int.from_bytes(raw, "little")
            raw = _read(k32, h, params + 0x24, 8)                     # CurrentDirectory.DosPath
            if raw is None:
                return None
            length = int.from_bytes(raw[0:2], "little")
            buffer = int.from_bytes(raw[4:8], "little")
        else:
            pbi = _PROCESS_BASIC_INFORMATION()
            if nt.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi),
                                            None) != 0 or not pbi.PebBaseAddress:
                return None
            raw = _read(k32, h, pbi.PebBaseAddress + 0x20, 8)         # PEB.ProcessParameters
            if raw is None:
                return None
            params = int.from_bytes(raw, "little")
            raw = _read(k32, h, params + 0x38, 16)                    # CurrentDirectory.DosPath
            if raw is None:
                return None
            length = int.from_bytes(raw[0:2], "little")
            buffer = int.from_bytes(raw[8:16], "little")
        if not length or not buffer or length > 32767 * 2:
            return None
        raw = _read(k32, h, buffer, length)
        if not raw:
            return None
        path = raw.decode("utf-16-le")
        return path.rstrip("\\") if len(path) > 3 else path   # 「C:\」以外は末尾の \ を落とす
    finally:
        k32.CloseHandle(h)


def _inside(path: str, folder: str) -> bool:
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\")
    f = os.path.normcase(os.path.abspath(folder)).rstrip("\\")
    return p == f or p.startswith(f + "\\")


def cwd_users(folder: str, procs: dict[int, str] | None = None) -> dict[int, str]:
    """フォルダ（の中）を作業フォルダにしているプロセス（ID → 作業フォルダ）。"""
    out = {}
    for pid in (procs or processes()):
        if pid in (0, 4):
            continue
        cwd = process_cwd(pid)
        if cwd and _inside(cwd, folder):
            out[pid] = cwd
    return out


def find(folder: str, progress=None, cancelled=None) -> dict:
    """まとめて調べる。結果は report() で文章にできる。

    時間がかかる（フォルダの中のファイルが多いと数十秒）ので、画面から呼ぶときは
    別スレッドで動かす。progress(済んだ数, 全部の数)、cancelled() が真なら止めて、
    それまでの結果に "cancelled": True を付けて返す。
    """
    files, truncated = list_files(folder)
    procs = processes()
    cwds = cwd_users(folder, procs)   # 速い（数十ミリ秒）
    users = file_users(files, progress, cancelled) if files else {}
    found = []
    for pid in sorted(set(users) | set(cwds)):
        exe = procs.get(pid, "")
        found.append({"pid": pid, "exe": exe,
                      "app": (users.get(pid) or {}).get("app") or exe,
                      "files": (users.get(pid) or {}).get("files", []),
                      "cwd": cwds.get(pid)})
    return {"folder": folder, "found": found, "file_count": len(files),
            "truncated": truncated, "cancelled": bool(cancelled and cancelled())}


def report(result: dict) -> str:
    """find() の結果を、利用者向けの文章にする。"""
    folder = result["folder"]
    lines = []
    if not result["found"]:
        lines.append(f"「{os.path.basename(folder) or folder}」を使っているアプリは見つかりません"
                     "でした。")
        lines.append("")
        lines.append("・もう一度、移動・削除を試してください（ウイルスの検査や検索の索引作成が"
                     "一時的に使っていただけのことがあります）。")
        lines.append("・同期ソフト（OneDrive など）や、管理者として動いているアプリは、ここでは"
                     "見つけられないことがあります。")
        lines.append("・それでも動かせないときは、Windows を再起動すると解放されます。")
    else:
        lines.append(f"「{os.path.basename(folder) or folder}」を使っているアプリ:")
        for f in result["found"]:
            lines.append("")
            name = f["app"] if f["app"] and f["app"] != f["exe"] else f["exe"]
            lines.append(f"■ {name}（{f['exe']}・プロセス番号 {f['pid']}）")
            if f["files"]:
                shown = [os.path.relpath(p, folder) for p in f["files"][:5]]
                more = f" ほか {len(f['files']) - 5} 個" if len(f["files"]) > 5 else ""
                lines.append(f"  開いているファイル: {'、'.join(shown)}{more}")
            elif not f["cwd"]:
                lines.append("  このフォルダの中のファイルを開いています。")
            if f["cwd"]:
                lines.append(f"  作業フォルダ: {f['cwd']}")
            hint = hint_for(f["exe"])
            if hint == DEFAULT_HINT and f["cwd"] and not f["files"]:
                hint = CWD_HINT
            lines.append(f"  → {hint}")
    if result.get("truncated"):
        lines.append("")
        lines.append(f"（ファイルが多いため、先頭の {MAX_FILES} 個だけを調べました）")
    return "\n".join(lines)
