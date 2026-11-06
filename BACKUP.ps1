[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Profile,
    [Parameter(Mandatory)][string]$Output,
    [switch]$Apply,
    [switch]$AllowLiveProfile,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$taskArguments = @((Join-Path $PSScriptRoot 'codex_maintenance.py'), 'backup', '--profile', $Profile, '--output', $Output)
if ($Apply) { $taskArguments += '--apply' }
if ($AllowLiveProfile) { $taskArguments += '--allow-live-profile' }
& $Python @taskArguments
exit $LASTEXITCODE
