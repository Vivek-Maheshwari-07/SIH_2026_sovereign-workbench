# Undoes scripts\firewall_block.ps1: restores the Enabled + DefaultOutboundAction of the
# Domain, Private and Public profiles saved in logs\firewall_state.json. If no saved state
# exists, sets DefaultOutboundAction to Allow on all three (the Windows default).
# Run from an elevated (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\firewall_restore.ps1

$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This script must run as Administrator.' -ForegroundColor Red
    Write-Host 'Right-click PowerShell -> Run as administrator, then run this script again.' -ForegroundColor Red
    exit 1
}

$profiles = @('Domain', 'Private', 'Public')
$repoRoot = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path (Join-Path $repoRoot 'logs') 'firewall_state.json'

if (Test-Path $stateFile) {
    $saved = [System.IO.File]::ReadAllText($stateFile) | ConvertFrom-Json
    foreach ($name in $profiles) {
        $entry = $saved.$name
        if ($null -eq $entry) {
            Write-Host "No saved state for profile $name; setting Allow." -ForegroundColor Yellow
            Set-NetFirewallProfile -Name $name -DefaultOutboundAction Allow
            continue
        }
        Set-NetFirewallProfile -Name $name -Enabled $entry.Enabled -DefaultOutboundAction $entry.DefaultOutboundAction
    }
    Remove-Item $stateFile
    Write-Host "Restored the saved firewall state (and removed $stateFile)."
} else {
    Write-Host 'No saved state found; setting outbound to Allow on all profiles.' -ForegroundColor Yellow
    Set-NetFirewallProfile -Name $profiles -DefaultOutboundAction Allow
}

Write-Host ''
Write-Host 'Firewall state now:'
Get-NetFirewallProfile -Name $profiles | Format-Table Name, Enabled, DefaultOutboundAction -AutoSize | Out-String | Write-Host
Write-Host 'Loopback (127.0.0.1) is not affected; the app keeps working.'
