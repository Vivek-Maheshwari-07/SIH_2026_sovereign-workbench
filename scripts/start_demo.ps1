# One-click start for the Sovereign AI Workbench demo (Track B, ticket B9, guide step 8.2).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_demo.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_demo.ps1 -SkipPrewarm -NoBrowser
#
# Steps (each prints [OK] or [FAIL] with a one-line fix; the first FAIL stops the script):
#   a. repo .venv Python only        e. backend (new window), wait for /api/health = ok
#   b. Ollama: restart as "ollama serve" with OLLAMA_NO_CLOUD=1 (+ our OLLAMA_* settings)
#   c. Docker engine + sandbox image f. /api/admin/prewarm (per-item result)
#   d. free the API and UI ports     g. UI (new window), open the browser
#                                    h. firewall state, NET-001, READY + total time
# Process ids of what it starts go to logs\demo_pids.json; scripts\stop_demo.ps1 stops exactly those.
# ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less file in the ANSI code page.

param(
    [switch]$SkipPrewarm,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$LogDir = Join-Path $Repo "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$PidFile = Join-Path $LogDir "demo_pids.json"

function Write-Ok([string]$Message) { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Write-Info([string]$Message) { Write-Host "       $Message" }
function Stop-WithFail([string]$Message, [string]$Fix) {
    Write-Host "[FAIL] $Message" -ForegroundColor Red
    Write-Host "       Fix: $Fix" -ForegroundColor Red
    exit 1
}

# .env reader (config comes from .env, never hard-coded; defaults match .env.example)
$EnvValues = @{}
$EnvFile = Join-Path $Repo ".env"
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith("#") -and $t.Contains("=")) {
            $k, $v = $t.Split("=", 2)
            $EnvValues[$k.Trim()] = $v.Trim()
        }
    }
}
function Get-Setting([string]$Key, [string]$Default) {
    if ($EnvValues.ContainsKey($Key) -and $EnvValues[$Key]) { return $EnvValues[$Key] }
    return $Default
}
$ApiPort = [int](Get-Setting "WB_API_PORT" "8000")
$UiPort = [int](Get-Setting "WB_UI_PORT" "8501")
$OllamaUrl = (Get-Setting "OLLAMA_HOST" "http://127.0.0.1:11434").TrimEnd("/")
$SandboxImage = Get-Setting "WB_SANDBOX_IMAGE" "wb-sandbox:1.0"
$OfflineKitTar = "C:\projects\offline_kit\wb-sandbox.tar"
$ApiUrl = "http://127.0.0.1:$ApiPort"
$UiUrl = "http://127.0.0.1:$UiPort"

function Test-Url([string]$Url, [int]$TimeoutSec = 3) {
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return $true
    } catch {
        return $false
    }
}

function Get-ProcessInfo([int]$ProcessId) {
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-OurServer([string]$CommandLine) {
    if (-not $CommandLine) { return $false }
    return ($CommandLine -match "uvicorn\s+backend\.main:app") -or ($CommandLine -match "streamlit\s+run\s+ui[\\/]app\.py")
}

$Started = [ordered]@{ started = (Get-Date).ToString("o") }
function Save-Pids { $Started | ConvertTo-Json | Set-Content -Path $PidFile -Encoding ASCII }

Write-Host ""
Write-Host "Sovereign AI Workbench - demo start" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------- a. repo .venv Python only
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Stop-WithFail ".venv not found at $Py (the global Python is never used)" `
        "py -3.11 -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.lock.txt"
}
$PyVersion = (& $Py -c "import sys; print(sys.version.split()[0])") 2>$null
Write-Ok "Python from .venv: $Py ($PyVersion)"

# ---------------------------------------------------------------- b. Ollama (no cloud)
$OllamaExe = $null
$cmd = Get-Command ollama -ErrorAction SilentlyContinue
if ($cmd) { $OllamaExe = $cmd.Source }
if (-not $OllamaExe) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $candidate) { $OllamaExe = $candidate }
}
if (-not $OllamaExe) { Stop-WithFail "ollama.exe not found" "install Ollama from the offline kit, then run this again" }

$old = Get-Process -Name "ollama app", "ollama", "ollama_llama_server" -ErrorAction SilentlyContinue
if ($old) {
    $names = ($old | ForEach-Object { "$($_.ProcessName) ($($_.Id))" }) -join ", "
    $old | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Info "Stopped the Ollama tray app / old server: $names"
}
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Test-Url $OllamaUrl 1)) { break }
    Start-Sleep -Milliseconds 500
}

# Settings for this server only (guide 1.5), plus no cloud and localhost binding. They are set
# inside the ollama window, not in this script's environment: the backend inherits this
# environment, and an OLLAMA_HOST without "http://" there would override the value in .env.
$OllamaBind = ($OllamaUrl -replace "^https?://", "")
$ollamaEnv = @{
    OLLAMA_NO_CLOUD = "1"; OLLAMA_KEEP_ALIVE = "30m"; OLLAMA_NUM_PARALLEL = "1"
    OLLAMA_MAX_LOADED_MODELS = "2"; OLLAMA_HOST = $OllamaBind
}
$setVars = ($ollamaEnv.GetEnumerator() | ForEach-Object { "set `"$($_.Key)=$($_.Value)`"" }) -join " & "
$OllamaLog = Join-Path $LogDir "ollama_serve.log"
Set-Content -Path $OllamaLog -Value "" -Encoding ASCII
$serveCmd = "/c title Workbench - ollama serve & $setVars & `"$OllamaExe`" serve >> `"$OllamaLog`" 2>&1"
$ollamaProc = Start-Process -FilePath "cmd.exe" -ArgumentList $serveCmd -WindowStyle Minimized -PassThru
$Started["ollama_window"] = $ollamaProc.Id
Save-Pids

$up = $false
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Url $OllamaUrl 2) { $up = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $up) { Stop-WithFail "Ollama did not answer at $OllamaUrl within 60 s" "see $OllamaLog" }

# Cloud check: the server logs "Ollama cloud disabled: true" at start when OLLAMA_NO_CLOUD=1.
$cloudLine = $null
for ($i = 0; $i -lt 20; $i++) {
    $cloudLine = Select-String -Path $OllamaLog -Pattern "Ollama cloud disabled: (true|false)" | Select-Object -First 1
    if ($cloudLine) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $cloudLine) {
    Stop-WithFail "Ollama is up, but its log has no 'Ollama cloud disabled' line" "update Ollama (0.34+), then check $OllamaLog"
}
if ($cloudLine.Line -notmatch "cloud disabled: true") {
    Stop-WithFail "Ollama cloud is NOT disabled ($($cloudLine.Line.Trim()))" "setx OLLAMA_NO_CLOUD 1, close this window, run start_demo again"
}
Write-Ok "Ollama serve at $OllamaUrl (log says 'Ollama cloud disabled: true'; keep_alive 30m, 1 parallel, 2 models)"

# ---------------------------------------------------------------- c. Docker + sandbox image
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) { Stop-WithFail "docker not found" "install Docker Desktop from the offline kit" }
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-WithFail "Docker engine is not running" "open Docker Desktop, wait for 'Engine running', run this again"
}
& docker image inspect $SandboxImage *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-WithFail "Docker image $SandboxImage not found" "docker load -i $OfflineKitTar"
}
Write-Ok "Docker engine running, image $SandboxImage present"

# ---------------------------------------------------------------- d. free the API and UI ports
foreach ($port in @($ApiPort, $UiPort)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owner in $listeners) {
        $info = Get-ProcessInfo $owner
        $what = if ($info) { "$($info.Name) (pid $owner): $($info.CommandLine)" } else { "pid $owner" }
        if ($info -and ($info.Name -like "python*") -and (Test-OurServer $info.CommandLine)) {
            & taskkill.exe /PID $owner /T /F *> $null
            Write-Info "Port ${port}: stopped our old server $what"
        } else {
            Stop-WithFail "port $port is used by $what" "this is not a workbench process: close it yourself (or tell us what it is), then run this again"
        }
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
}
Write-Ok "Ports $ApiPort and $UiPort are free"

# ---------------------------------------------------------------- e. backend
$backend = Start-Process -FilePath $Py -WorkingDirectory $Repo -WindowStyle Minimized -PassThru `
    -ArgumentList @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$ApiPort")
$Started["backend"] = $backend.Id
Save-Pids
$health = $null
for ($i = 0; $i -lt 120; $i++) {
    if ($backend.HasExited) { Stop-WithFail "the backend window closed at start" "see logs\backend.log" }
    try { $health = Invoke-RestMethod -Uri "$ApiUrl/api/health" -TimeoutSec 15 } catch { $health = $null }
    if ($health -and $health.status -eq "ok") { break }
    Start-Sleep -Seconds 1
}
if (-not $health) { Stop-WithFail "backend did not answer at $ApiUrl within 120 s" "see logs\backend.log" }
if ($health.status -ne "ok") {
    $bad = @()
    if (-not $health.ollama_ok) { $bad += "Ollama (restart start_demo)" }
    if (-not $health.sandbox_ok) { $bad += "sandbox (open Docker Desktop)" }
    if (-not $health.tesseract_ok) { $bad += "Tesseract (check TESSERACT_CMD in .env)" }
    Stop-WithFail "backend health is '$($health.status)'" ("fix: " + ($bad -join ", "))
}
Write-Ok "Backend at $ApiUrl, health ok (contract $($health.contract_version), $($health.kb_chunks) KB chunks)"

# ---------------------------------------------------------------- f. prewarm
if ($SkipPrewarm) {
    Write-Info "Prewarm skipped (-SkipPrewarm): the first job will be slower."
} else {
    try {
        $warm = Invoke-RestMethod -Method Post -Uri "$ApiUrl/api/admin/prewarm" -TimeoutSec 300
    } catch {
        Stop-WithFail "prewarm call failed: $($_.Exception.Message)" "see logs\backend.log, or run with -SkipPrewarm"
    }
    foreach ($item in $warm.warmed) { Write-Info "ready:  $item" }
    foreach ($item in $warm.failed) { Write-Info "failed: $item" }
    $secs = [math]::Round($warm.duration_ms / 1000, 1)
    if ($warm.failed.Count -gt 0) {
        Write-Warn "Prewarm finished in $secs s with $($warm.failed.Count) failed item(s); jobs still run, just slower"
    } else {
        Write-Ok "Prewarm finished in $secs s"
    }
}

# ---------------------------------------------------------------- g. UI
$ui = Start-Process -FilePath $Py -WorkingDirectory $Repo -WindowStyle Minimized -PassThru `
    -ArgumentList @("-m", "streamlit", "run", "ui/app.py", "--server.address", "127.0.0.1", "--server.port", "$UiPort")
$Started["ui"] = $ui.Id
Save-Pids
$uiUp = $false
for ($i = 0; $i -lt 60; $i++) {
    if ($ui.HasExited) { Stop-WithFail "the UI window closed at start" "run the streamlit command by hand to see the error" }
    if (Test-Url "$UiUrl/_stcore/health" 2) { $uiUp = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $uiUp) { Stop-WithFail "UI did not answer at $UiUrl within 60 s" "run the streamlit command by hand to see the error" }
if (-not $NoBrowser) { Start-Process $UiUrl }
Write-Ok "UI at $UiUrl$(if ($NoBrowser) { '' } else { ' (opened in the browser)' })"

# ---------------------------------------------------------------- h. firewall, NET-001, READY
try {
    $net = Invoke-RestMethod -Uri "$ApiUrl/api/network/status" -TimeoutSec 10
    if ($net.firewall_outbound_blocked -eq $true) {
        Write-Ok "Firewall: outbound blocked"
    } elseif ($net.firewall_outbound_blocked -eq $false) {
        Write-Warn "Firewall: outbound OPEN. Before the demo run scripts\firewall_block.ps1 as admin and switch Wi-Fi off"
    } else {
        Write-Warn "Firewall: state unknown (could not be read)"
    }
    $n = [int]$net.external_seen_since_start
    if ($n -eq 0) {
        Write-Ok "NET-001: 0 core external connections since backend start"
    } else {
        Write-Warn "NET-001: $n core external connection(s) since backend start - see the Network screen"
    }
} catch {
    Write-Warn "Network status not readable: $($_.Exception.Message)"
}

$Stopwatch.Stop()
$total = [math]::Round($Stopwatch.Elapsed.TotalSeconds)
Write-Host ""
Write-Host "READY in $total s  ->  $UiUrl" -ForegroundColor Green
if ($total -gt 180) { Write-Warn "Start took longer than the 3 minute target" }
Write-Host "Stop everything with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_demo.ps1"
exit 0
