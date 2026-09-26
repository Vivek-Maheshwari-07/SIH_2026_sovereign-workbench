# Blocks ALL outbound traffic on this laptop with Windows Firewall (demo "air gap").
# Saves the current Enabled + DefaultOutboundAction of the Domain, Private and Public
# profiles to logs\firewall_state.json first, so scripts\firewall_restore.ps1 can undo it.
# Run from an elevated (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\firewall_block.ps1

$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This script must run as Administrator.' -ForegroundColor Red
    Write-Host 'Right-click PowerShell -> Run as administrator, then run this script again.' -ForegroundColor Red
    exit 1
}

$profiles = @('Domain', 'Private', 'Public')
$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot 'logs'
$stateFile = Join-Path $logDir 'firewall_state.json'

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

if (Test-Path $stateFile) {
    # Already blocked once and not restored: keep the ORIGINAL state, never overwrite it with "Block".
    Write-Host "Saved state already exists, keeping it: $stateFile" -ForegroundColor Yellow
} else {
    $saved = @{}
    foreach ($p in Get-NetFirewallProfile -Name $profiles) {
        $saved[$p.Name] = @{
            Enabled               = "$($p.Enabled)"
            DefaultOutboundAction = "$($p.DefaultOutboundAction)"
        }
    }
    $json = $saved | ConvertTo-Json -Depth 3
    # UTF-8 without BOM (Out-File / Set-Content in PowerShell 5.1 would write UTF-16 / ANSI).
    [System.IO.File]::WriteAllText($stateFile, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Saved current firewall state to $stateFile"
}

# A disabled profile ignores DefaultOutboundAction, so the firewall is switched on too.
Set-NetFirewallProfile -Name $profiles -Enabled True -DefaultOutboundAction Block

Write-Host ''
Write-Host 'Outbound traffic is now BLOCKED:' -ForegroundColor Green
$table = Get-NetFirewallProfile -Name $profiles | Format-Table Name, Enabled, DefaultOutboundAction -AutoSize | Out-String
Write-Host $table -ForegroundColor Green
Write-Host 'Note: existing outbound ALLOW rules still apply to the programs they name.'
Write-Host 'Loopback (127.0.0.1) is not affected; the app keeps working.'
