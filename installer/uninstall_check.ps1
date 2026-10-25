# Installer check: install -> simulate what the app / an auto-update leaves behind -> uninstall.
#
# Compiles installer\TriVReader.iss under TEST identifiers (another AppId, exe name, ProgID,
# data folder, registry key and "running" mutex), so nothing here touches a real TriV-Reader
# install, its shortcuts, its .pdf associations or its settings. Needs Inno Setup 6 (ISCC.exe).
#
#   powershell -ExecutionPolicy Bypass -File installer\uninstall_check.ps1
#
# Scenarios (all silent):
#   A  installed with the association task. Files a later update added, the previous-version
#      backup and the start-up records are removed; settings / OCR data are kept
#   B  association registered later by the app (viewer\associate.py): removed as well
#   C  a registration that points to ANOTHER install is left alone
#   D  uninstalling while the app is "running" (silent mode cannot ask, so it goes on)
# The dialogs of a non-silent uninstall (running warning, "delete settings too?") need a
# desktop and were checked by hand with UI Automation; they are not part of this script.
param([string]$Iss = (Join-Path $PSScriptRoot "TriVReader.iss"),
      [string]$Work = (Join-Path $env:TEMP "trv_inno_check"))
$ErrorActionPreference = "Stop"

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "ISCC.exe (Inno Setup 6) not found" }

$guid = "0F1E2D3C-4B5A-4978-8695-A4B3C2D1E0F9"
$prog = "TriVReaderTest.Document"
$exe  = "TriVReaderTest.exe"
$data = Join-Path $env:LOCALAPPDATA "trivreader_inno_test"
$org  = "HKCU:\Software\trivreader_inno_test"
$cls  = "HKCU:\Software\Classes"
$unin = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{$guid}_is1"
$res  = @()
function Check($name, $ok, $extra = "") { $script:res += [bool]$ok; "{0} {1} {2}" -f ($(if ($ok) { "[PASS]" } else { "[FAIL]" }), $name, $extra) }

# a fake onedir build (SourceDir)
$dist = Join-Path $Work "dist"
Remove-Item $dist -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$dist\_internal" | Out-Null
Copy-Item "$env:SystemRoot\System32\notepad.exe" "$dist\$exe"
Set-Content "$dist\pdf_doc.ico" "x"
Set-Content "$dist\_internal\base.dll" "dll"
$icon = Join-Path $env:USERPROFILE "TriVReader_dist\_build\trivreader.ico"
if (-not (Test-Path $icon)) { $icon = (Get-ChildItem (Join-Path $PSScriptRoot "..") -Recurse -Filter *.ico | Select-Object -First 1).FullName }

# --- compile -------------------------------------------------------------------------
$out = Join-Path $Work "out"
New-Item -ItemType Directory -Force $out | Out-Null
$isccArgs = @("/DMyAppVersion=9.9.9", "/DSourceDir=$dist", "/DOutputDir=$out", "/DAppIcon=$icon",
              "/DMyAppName=TriVTest", "/DMyAppGuid=$guid", "/DMyAppExeName=$exe", "/DMyProgId=$prog",
              "/DDataDirName=trivreader_inno_test", "/DRegOrgKey=trivreader_inno_test",
              "/DRunningMutexPrefix=TriVReaderTest-", $Iss)
$log = & $iscc @isccArgs 2>&1 | Out-String
$compiled = ($LASTEXITCODE -eq 0)
Check "the script compiles" $compiled ($(if (-not $compiled) { $log.Substring([Math]::Max(0, $log.Length - 600)) }))
if (-not $compiled) { exit 1 }
$setup = Join-Path $out "TriVReader-Setup-9.9.9.exe"

function Ensure($k) { if (-not (Test-Path -LiteralPath $k)) { New-Item -Path $k -Force | Out-Null } }
function Cleanup {
    foreach ($k in @("$cls\$prog", "$cls\Applications\$exe", $unin, $org)) { Remove-Item $k -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -ErrorAction SilentlyContinue
    Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue
}
function Install($dir, $tasks) {
    $a = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR=$dir", "/TASKS=$tasks")
    $p = Start-Process $setup -ArgumentList $a -Wait -PassThru
    return $p.ExitCode
}
function Uninstall($dir) {
    $u = Get-ChildItem $dir -Filter "unins*.exe" | Select-Object -First 1
    $p = Start-Process $u.FullName -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") -PassThru
    $p.WaitForExit()
    # the uninstaller hands the work to a copy of itself and returns first: wait for the file to go
    for ($i = 0; $i -lt 60; $i++) { if (-not (Test-Path $u.FullName)) { break }; Start-Sleep -Milliseconds 500 }
    Start-Sleep -Milliseconds 800
}
function Leftovers($dir) {
    $d = Join-Path $data "previous_version"
    New-Item -ItemType Directory -Force $d | Out-Null
    Set-Content (Join-Path $d "x.bin") "old app copy"
    Set-Content (Join-Path $data "previous_version.json") "{}"
    Set-Content (Join-Path $data "startup_pending.json") "{}"
    Set-Content (Join-Path $data "skip_update.txt") "1.0"
    New-Item -ItemType Directory -Force (Join-Path $data "tessdata") | Out-Null
    Set-Content (Join-Path $data "tessdata\jpn.traineddata") "lang"
    Ensure "$org\trivreader_inno_test"
    Set-ItemProperty "$org\trivreader_inno_test" -Name "zoom" -Value "1.5"
    # parts an auto-update added after the install (not in the installer's own record)
    New-Item -ItemType Directory -Force (Join-Path $dir "_internal\sub") | Out-Null
    Set-Content (Join-Path $dir "_internal\added_by_update.dll") "new"
    Set-Content (Join-Path $dir "_internal\sub\more.pyd") "new"
}

Cleanup
# ===== A: association chosen at install; an update added files; silent uninstall =====
$dirA = Join-Path $Work "app_A"
Remove-Item $dirA -Recurse -Force -ErrorAction SilentlyContinue
$rc = Install $dirA "associate"
Check "A: installs silently" ($rc -eq 0 -and (Test-Path "$dirA\$exe")) "(rc $rc)"
Check "A: association keys were written" ((Test-Path "$cls\$prog\shell\open\command") -and (Test-Path "$cls\Applications\$exe\shell\open\command"))
Leftovers $dirA
Uninstall $dirA
Check "A: the app folder is gone (including parts a later update added)" (-not (Test-Path $dirA)) ($(if (Test-Path $dirA) { (Get-ChildItem $dirA -Recurse -Force | ForEach-Object { $_.FullName.Substring($dirA.Length) }) -join ", " }))
Check "A: the previous-version backup is gone" (-not (Test-Path "$data\previous_version"))
Check "A: ...also previous_version.json / startup_pending.json / skip_update.txt" (-not ((Test-Path "$data\previous_version.json") -or (Test-Path "$data\startup_pending.json") -or (Test-Path "$data\skip_update.txt")))
Check "A: user data (OCR language data, settings) is kept in silent mode" ((Test-Path "$data\tessdata\jpn.traineddata") -and (Test-Path $org))
Check "A: the ProgID key is gone" (-not (Test-Path "$cls\$prog"))
Check "A: the Applications key is gone as a whole (it used to leave a ghost entry)" (-not (Test-Path "$cls\Applications\$exe"))
Check "A: the OpenWithProgids value is gone" ($null -eq (Get-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -ErrorAction SilentlyContinue))
Check "A: the uninstall entry is gone" (-not (Test-Path $unin))
Cleanup

# ===== B: no association at install; the app registered one later =====
$dirB = Join-Path $Work "app_B"
Remove-Item $dirB -Recurse -Force -ErrorAction SilentlyContinue
$rc = Install $dirB ""
Check "B: installs without the association task" ($rc -eq 0 -and -not (Test-Path "$cls\$prog")) "(rc $rc)"
# the same layout as viewer\associate.py register(), under the test names
$cmd = "`"$dirB\$exe`" `"%1`""
foreach ($pair in @(@("$cls\$prog", $null, "PDF"), @("$cls\$prog\DefaultIcon", $null, "x,0"), @("$cls\$prog\shell\open\command", $null, $cmd),
                    @("$cls\Applications\$exe", "FriendlyAppName", "TriVTest"), @("$cls\Applications\$exe\shell\open\command", $null, $cmd),
                    @("$cls\Applications\$exe\DefaultIcon", $null, "x,0"), @("$cls\Applications\$exe\SupportedTypes", ".pdf", ""))) {
    Ensure $pair[0]
    if ($pair[1]) { Set-ItemProperty $pair[0] -Name $pair[1] -Value $pair[2] } else { Set-ItemProperty $pair[0] -Name "(default)" -Value $pair[2] }
}
Ensure "$cls\.pdf\OpenWithProgids"
New-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -Value "" -PropertyType String -Force | Out-Null
Uninstall $dirB
Check "B: the runtime-registered ProgID is removed" (-not (Test-Path "$cls\$prog"))
Check "B: the runtime-registered Applications key is removed" (-not (Test-Path "$cls\Applications\$exe"))
Check "B: the runtime-registered OpenWithProgids value is removed" ($null -eq (Get-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -ErrorAction SilentlyContinue))
Cleanup

# ===== C: a registration pointing to another install is not ours to remove =====
$dirC = Join-Path $Work "app_C"
Remove-Item $dirC -Recurse -Force -ErrorAction SilentlyContinue
$rc = Install $dirC ""
$other = "`"C:\SomewhereElse\$exe`" `"%1`""
foreach ($k in @("$cls\$prog\shell\open\command", "$cls\Applications\$exe\shell\open\command")) {
    Ensure $k; Set-ItemProperty $k -Name "(default)" -Value $other
}
Ensure "$cls\.pdf\OpenWithProgids"
New-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -Value "" -PropertyType String -Force | Out-Null
Uninstall $dirC
Check "C: a registration that points to another install is left alone" ((Test-Path "$cls\$prog\shell\open\command") -and (Test-Path "$cls\Applications\$exe\shell\open\command") -and ($null -ne (Get-ItemProperty "$cls\.pdf\OpenWithProgids" -Name $prog -ErrorAction SilentlyContinue)))
Cleanup

# ===== D: silent uninstall while the app is "running" (cannot ask, so it goes on) =====
$dirD = Join-Path $Work "app_D"
Remove-Item $dirD -Recurse -Force -ErrorAction SilentlyContinue
$rc = Install $dirD ""
$mutex = New-Object System.Threading.Mutex($false, "TriVReaderTest-$env:USERNAME")
Uninstall $dirD
$mutex.Dispose()
Check "D: a silent uninstall while 'running' does not hang or fail" (-not (Test-Path "$dirD\unins000.exe"))
Remove-Item $dirD -Recurse -Force -ErrorAction SilentlyContinue
Cleanup

$n = ($res | Where-Object { $_ }).Count
"`n==== $n/$($res.Count) checks passed ===="
if ($n -ne $res.Count) { exit 1 }
