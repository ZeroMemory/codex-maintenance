[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Profile,
    [Parameter(Mandatory)][string]$Backup,
    [switch]$Apply,
    [switch]$RestoreConfig,
    [switch]$LegacyLayout,
    [switch]$AllowLiveProfile,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$taskArguments = @((Join-Path $PSScriptRoot 'codex_maintenance.py'), 'restore', '--profile', $Profile, '--backup', $Backup)
if ($Apply) { $taskArguments += '--apply' }
if ($RestoreConfig) { $taskArguments += '--restore-config' }
if ($LegacyLayout) { $taskArguments += '--legacy-layout' }
if ($AllowLiveProfile) { $taskArguments += '--allow-live-profile' }
& $Python @taskArguments
exit $LASTEXITCODE
