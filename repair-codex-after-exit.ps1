[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Profile,
    [Parameter(Mandatory)][string]$Roots,
    [switch]$Apply,
    [switch]$AllowLiveProfile,
    [int]$TimeoutSeconds = 120,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
if ($Apply -and $AllowLiveProfile) {
    $taskDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|ChatGPT|codex-app-server|codex-command-runner.*)$' }).Count -gt 0) {
        if ((Get-Date) -ge $taskDeadline) { throw 'Codex is still running; no files changed.' }
        Start-Sleep -Milliseconds 500
    }
}
# No process is closed or restarted automatically.
& (Join-Path $PSScriptRoot 'repair-codex-config.ps1') -Profile $Profile -Roots $Roots -Apply:$Apply -AllowLiveProfile:$AllowLiveProfile -Python $Python
exit $LASTEXITCODE
