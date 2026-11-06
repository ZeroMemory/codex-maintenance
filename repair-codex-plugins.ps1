[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Profile,
    [Parameter(Mandatory)][string]$Plan,
    [switch]$Apply,
    [switch]$AllowLiveProfile,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$taskArguments = @((Join-Path $PSScriptRoot 'codex_maintenance.py'), 'repair-plugins', '--profile', $Profile, '--plan', $Plan)
if ($Apply) { $taskArguments += '--apply' }
if ($AllowLiveProfile) { $taskArguments += '--allow-live-profile' }
& $Python @taskArguments
exit $LASTEXITCODE
