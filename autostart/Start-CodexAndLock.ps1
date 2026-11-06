[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$ValidateOnly,
    [string]$AppUserModelId,
    [string]$ProcessName = 'Codex',
    [int]$MaximumWaitSeconds = 8
)
$ErrorActionPreference = 'Stop'
if ($MaximumWaitSeconds -lt 1 -or $MaximumWaitSeconds -gt 60) { throw 'Wait must be between 1 and 60 seconds.' }
if ($Apply -and $ValidateOnly) { throw 'Use either -Apply or -ValidateOnly.' }
if (-not $Apply -and -not $ValidateOnly) {
    [pscustomobject]@{ ApplyRequired = $true; ActionsExecuted = $false; MaximumAppWaitSeconds = $MaximumWaitSeconds }
    return
}
if (-not $AppUserModelId) {
    $taskApp = @(Get-StartApps | Where-Object AppID -like 'OpenAI.Codex_*!*')
    if ($taskApp.Count -ne 1) { throw 'Specify -AppUserModelId from Get-StartApps; Codex identity is ambiguous or missing.' }
    $AppUserModelId = $taskApp[0].AppID
}
if (@(Get-StartApps | Where-Object AppID -eq $AppUserModelId).Count -ne 1) { throw 'The selected app is not registered for this account.' }
$taskSessionId = (Get-Process -Id $PID).SessionId
$taskWinlogonKey = Get-Item -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$taskAutoUser = [string]$taskWinlogonKey.GetValue('DefaultUserName')
$taskShouldLock = $taskWinlogonKey.GetValue('AutoAdminLogon') -eq '1' -and
    ($taskAutoUser -ieq $env:USERNAME -or $taskAutoUser -ieq ($env:USERDOMAIN + '\' + $env:USERNAME))
if ($ValidateOnly) {
    [pscustomobject]@{ AppUserModelId = $AppUserModelId; ProcessName = $ProcessName; SessionId = $taskSessionId; LockAfterStartupEnabled = $taskShouldLock; ActionsExecuted = $false }
    return
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CodexStartupNative {
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool LockWorkStation();
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WTSQuerySessionInformationW(IntPtr server, int sessionId, int infoClass, out IntPtr buffer, out int bytes);
    [DllImport("wtsapi32.dll")]
    private static extern void WTSFreeMemory(IntPtr buffer);
    public static bool? IsLocked(int sessionId) {
        IntPtr buffer; int bytes;
        if (!WTSQuerySessionInformationW(IntPtr.Zero, sessionId, 25, out buffer, out bytes)) return null;
        try {
            if (bytes < 20 || Marshal.ReadInt32(buffer, 0) != 1 || Marshal.ReadInt32(buffer, 8) != sessionId) return null;
            int flags = Marshal.ReadInt32(buffer, 16);
            return flags == 0 ? true : flags == 1 ? false : (bool?)null;
        } finally { WTSFreeMemory(buffer); }
    }
}
'@
$taskLogPath = Join-Path $PSScriptRoot 'last-startup.log'
function Write-TaskStartupLog([string]$Message) {
    Add-Content -LiteralPath $taskLogPath -Value ('{0:o} {1}' -f (Get-Date), $Message) -Encoding UTF8
}
$taskFailed = $false
try {
    Start-Process -FilePath (Join-Path $env:WINDIR 'explorer.exe') -ArgumentList ('shell:AppsFolder\' + $AppUserModelId) -WindowStyle Hidden
    Write-TaskStartupLog 'Codex activation requested.'
    $taskWatch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $taskAppWindow = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $taskSessionId -and $_.MainWindowHandle -ne 0 })
        if ($taskAppWindow.Count -gt 0) { break }
        Start-Sleep -Milliseconds 250
    } while ($taskWatch.Elapsed.TotalSeconds -lt $MaximumWaitSeconds)
    if ($taskAppWindow.Count -eq 0) { throw 'Codex window was not detected before the timeout.' }
    Write-TaskStartupLog 'Codex window detected.'
} catch {
    $taskFailed = $true
    Write-TaskStartupLog ('Codex activation failed: ' + $_.Exception.Message)
} finally {
    if ($taskShouldLock -and [CodexStartupNative]::IsLocked($taskSessionId) -ne $true) {
        if (-not [CodexStartupNative]::LockWorkStation()) { $taskFailed = $true; Write-TaskStartupLog 'Lock request failed.' }
        else { Write-TaskStartupLog 'Lock requested.' }
    } else { Write-TaskStartupLog 'Lock not required.' }
}
if ($taskFailed) { exit 1 }
