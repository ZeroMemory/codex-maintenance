# Codex Maintenance Toolkit

Back up and restore Codex chats, memory and metadata, or repair project and plugin settings in an existing profile. Version 0.1.1.

Requires Windows, Python 3.12 or later and PowerShell 5.1 or 7. The Python tools use the standard library. Run the commands below from the repository root.

## Back up and restore a profile

Close Codex and select a profile. Commands preview changes by default; pass `-Apply` or `--apply` to execute them.

```powershell
$profilePath = Read-Host 'Codex profile directory to back up'
$backupPath = Read-Host 'New backup directory'
.\BACKUP.ps1 -Profile "$profilePath" -Output "$backupPath"
.\BACKUP.ps1 -Profile "$profilePath" -Output "$backupPath" -Apply
```

To restore, select the backup and target profile:

```powershell
$backupPath = Read-Host 'Backup directory to restore'
$profilePath = Read-Host 'Target Codex profile directory'
.\RESTORE.ps1 -Profile "$profilePath" -Backup "$backupPath" -Apply
```

Backups include memory, active and archived chats, history, attachments, rules, plans, user metadata and SQLite databases. Hidden files and empty directories are included.

Restore checks the exact inventory and hashes in `MANIFEST.json`. It saves replaced files to the sibling `<profile>-maintenance-backups` directory and leaves unrelated files in place. By default, configuration goes to `config.before-reinstall-reference.toml`. Pass `-RestoreConfig` to replace `config.toml`. Use `-LegacyLayout` for backups with `databases/active`.

Checkpoint the source and target databases before restoring. For the active profile, add `-AllowLiveProfile` or `--allow-live-profile`.

## Repair projects and plugins

Project repair takes a JSON array of existing directories. It updates project roots and their order in the JSON state and its `.bak` copy, preserves unrelated settings and sets `trust_level = "trusted"` for the listed projects.

```powershell
$profilePath = Read-Host 'Codex profile directory'
$rootsFile = Read-Host 'Path to the project-roots JSON file'
.\repair-codex-config.ps1 -Profile "$profilePath" -Roots "$rootsFile" -Apply
# Preview project repair after closing Codex:
.\repair-codex-after-exit.ps1 -Profile "$profilePath" -Roots "$rootsFile"
```

Plugin repair uses local sources. The JSON `-Plan` contains `copies` with a `source` relative to the plan and a `destination` relative to the profile. Optional `config_blocks` specify a `header` and a `lines` array. Each source needs `.codex-plugin/plugin.json` or `.agents/plugins/marketplace.json`. Use `config_blocks` for single-line TOML settings.

```powershell
$profilePath = Read-Host 'Codex profile directory'
$planFile = Read-Host 'Path to the plugin-repair plan JSON file'
.\repair-codex-plugins.ps1 -Profile "$profilePath" -Plan "$planFile" -Apply
```

## Launch Codex after sign-in

Check the registered Codex package and Windows autologon policy from the repository root.

```powershell
.\autostart\Start-CodexAndLock.ps1 -ValidateOnly
```

Package detection uses AppID, including when the Start menu shows ChatGPT. For multiple matches, pass `-AppUserModelId` from `Get-StartApps`. Use `-ProcessName ChatGPT` for the older executable.

With Windows autologon configured, pass `-Apply` to launch Codex and lock the session after sign-in.
