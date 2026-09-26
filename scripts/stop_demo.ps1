# Stops what scripts\start_demo.ps1 started: the UI, the backend and the "ollama serve" window.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_demo.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_demo.ps1 -Force
#
# Only our own processes: each pid from logs\demo_pids.json is checked against what it must be
# (uvicorn backend.main:app / streamlit run ui/app.py / the "Workbench - ollama serve" window)
# before it is stopped. Without that file (e.g. servers started by hand) it only LISTS processes
# with those command lines; add -Force to stop them too.
# The Ollama tray app is not restarted; open Ollama from the Start menu if you want it back.
# ASCII only on purpose: Windows PowerShell 5.1 reads a BOM-less file in the ANSI code page.

param([switch]$Force)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Repo "logs\demo_pids.json"
$Stopped = 0

function Get-ProcessInfo([int]$ProcessId) {
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Get-Children([int]$ProcessId) {
    return @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue)
}

function Test-Expected([string]$Role, $Info) {
    if (-not $Info) { return $false }
    $line = [string]$Info.CommandLine
    switch ($Role) {
        "backend" { return $line -match "uvicorn\s+backend\.main:app" }
        "ui" { return $line -match "streamlit\s+run\s+ui[\\/]app\.py" }
        "ollama_window" { return ($Info.Name -eq "cmd.exe") -and ($line -match "Workbench - ollama serve") }
    }
    return $false
}

function Stop-Tree([string]$Role, [int]$ProcessId, [string]$What) {
    & taskkill.exe /PID $ProcessId /T /F *> $null
    Write-Host "[OK]   Stopped $Role (pid $ProcessId): $What" -ForegroundColor Green
    $script:Stopped++
}

$targets = @()
if (Test-Path $PidFile) {
    $saved = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($role in @("ui", "backend", "ollama_window")) {
        $procId = $saved.$role
        if (-not $procId) { continue }
        $info = Get-ProcessInfo ([int]$procId)
        if (-not $info) {
            Write-Host "       $role (pid $procId) is not running any more"
            continue
        }
        # The .venv python.exe is a launcher: the real server is its child with the same command line.
        $ok = (Test-Expected $role $info) -or @(Get-Children $info.ProcessId | Where-Object { Test-Expected $role $_ }).Count -gt 0
        if ($ok) {
            $targets += [pscustomobject]@{ Role = $role; Id = [int]$procId; What = [string]$info.CommandLine }
        } else {
            Write-Host "[SKIP] pid $procId is now '$($info.Name)', not our $role; left alone" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "       No logs\demo_pids.json (start_demo did not start them); looking by command line."
    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    foreach ($p in $all) {
        foreach ($role in @("ui", "backend", "ollama_window")) {
            if (Test-Expected $role $p) {
                $targets += [pscustomobject]@{ Role = $role; Id = [int]$p.ProcessId; What = [string]$p.CommandLine }
            }
        }
    }
}

if (-not (Test-Path $PidFile) -and -not $Force -and $targets.Count -gt 0) {
    foreach ($t in $targets) { Write-Host "       found $($t.Role) (pid $($t.Id)): $($t.What)" }
    Write-Host "[SKIP] Not stopped: they were not started by start_demo. Run again with -Force to stop them." -ForegroundColor Yellow
    exit 0
}
foreach ($t in $targets) { Stop-Tree $t.Role $t.Id $t.What }

if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
if ($Stopped -eq 0) {
    Write-Host "Nothing of ours was running."
} else {
    Write-Host "Stopped $Stopped process tree(s). Ollama's tray app was not restarted." -ForegroundColor Green
}
exit 0
