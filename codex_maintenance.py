"""Portable file operations adapted from the local Codex repair/restore helpers.

Every command requires an explicit profile. Preview is the default. The current
user's active profile needs --allow-live-profile and a stopped Codex process.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid


def cleanup_temporary_directory(path, onerror=None):
    try:
        shutil.rmtree(path, onerror=onerror)
    except OSError as exc:
        print(f"Warning: cleanup failed; temporary files retained in {path}: {exc}", file=sys.stderr)


VERSION = "0.1.1"
DIRECTORIES = (
    "memories", "sessions", "archived_sessions", "generated_images",
    "ambient-suggestions", "rules", "attachments", "dictation-history",
    "plans", "pets", "sqlite", "project-metadata",
)
FILES = (
    "history.jsonl", "session_index.jsonl", "transcription-history.jsonl",
    ".codex-global-state.json", ".codex-global-state.json.bak", "AGENTS.md",
    "installation_id", "version.json", "config.toml", "keybindings.json",
    "realtime-voice-continuity.json",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def path_state(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    metadata = tuple(getattr(info, key, None) for key in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
        "st_size", "st_mtime_ns", "st_birthtime_ns", "st_file_attributes", "st_reparse_tag"))
    content = None
    if not getattr(info, "st_reparse_tag", 0) and not stat.S_ISLNK(info.st_mode):
        if stat.S_ISREG(info.st_mode):
            content = sha256(path)
        elif stat.S_ISDIR(info.st_mode):
            content = tuple((p.name, path_state(p)) for p in sorted(path.iterdir()))
    return metadata, content


def require_state(path: Path, expected):
    if path_state(path) != expected:
        raise OSError(f"Concurrent change detected at {path}; rollback stopped")


def json_read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reject_links(path: Path, recursive=True):
    """Reject junctions/symlinks before traversing or mutating a tree."""
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError(f"Linked path is not supported: {part}")
    if recursive and path.is_dir():
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(root) / name
                if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                    raise ValueError(f"Linked path is not supported: {item}")


def disjoint(first: Path, second: Path):
    if first == second or first in second.parents or second in first.parents:
        raise ValueError("Source and destination must be separate, non-nested directories")


def safe_relative(value: str) -> Path:
    p = Path(value)
    if not value or p.is_absolute() or p.drive or ".." in p.parts or ":" in value or "\\" in value:
        raise ValueError(f"Unsafe relative path: {value}")
    if p.as_posix() != value or value == ".":
        raise ValueError(f"Non-canonical path: {value}")
    return p


def check_profile(args) -> Path:
    raw = Path(args.profile).absolute()
    reject_links(raw, recursive=False)
    profile = raw.resolve()
    if profile == Path(profile.anchor):
        raise ValueError("A filesystem root is not a profile")
    live_candidates = [Path.home() / ".codex"]
    if os.environ.get("CODEX_HOME"):
        live_candidates.append(Path(os.environ["CODEX_HOME"]))
    if any(profile == p.resolve() or p.resolve() in profile.parents for p in live_candidates):
        if not args.allow_live_profile:
            raise ValueError("Live profile requires --allow-live-profile; close Codex first")
        if args.apply:
            if os.name != "nt":
                raise ValueError("Live-profile process verification currently supports Windows only")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(Codex|ChatGPT|codex-app-server|codex-command-runner.*)$' }).Count"],
                capture_output=True, text=True, check=True,
            )
            if result.stdout.strip() != "0":
                raise ValueError("Codex is running; close it completely before applying")
    reject_links(profile)
    return profile


def copy_tree(source: Path, target: Path):
    reject_links(source)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def sqlite_snapshot(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    uri = source.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as reader, closing(sqlite3.connect(target)) as writer:
        reader.backup(writer)
        if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {source.name}")


def verify_sqlite(path: Path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"Invalid SQLite file: {path.name}")


def selected(profile: Path) -> list[Path]:
    entries = [profile / name for name in DIRECTORIES + FILES if (profile / name).exists()]
    entries.extend(p for p in sorted(profile.glob("*.sqlite")) if "pre-union" not in p.name)
    for path in entries:
        reject_links(path)
    return entries


def create_snapshot(profile: Path, output: Path):
    """Copy context, including dotfiles, and checkpoint databases through SQLite.

    Top-level auth.json, plugin caches, runtimes and project source worktrees are excluded.
    Codex should be closed for a cross-file point-in-time snapshot.
    """
    entries = selected(profile)
    if not entries:
        raise ValueError("No supported context entries were found")
    for path in entries:
        destination = output / path.name
        if path.suffix == ".sqlite" and path.is_file():
            sqlite_snapshot(path, destination)
        elif path.name == "sqlite" and path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            for item in path.rglob("*"):
                if item.is_dir():
                    (destination / item.relative_to(path)).mkdir(parents=True, exist_ok=True)
                elif item.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                elif item.suffix in (".sqlite", ".db"):
                    sqlite_snapshot(item, destination / item.relative_to(path))
                else:
                    copy_tree(item, destination / item.relative_to(path))
        else:
            copy_tree(path, destination)
    manifest = {
        "format": 1, "tool_version": VERSION,
        "files": {p.relative_to(output).as_posix(): sha256(p)
                  for p in sorted(output.rglob("*")) if p.is_file()},
        "directories": [p.relative_to(output).as_posix()
                        for p in sorted(output.rglob("*")) if p.is_dir()],
        "excluded": ["top-level auth.json", "plugin caches", "runtimes", "source worktrees"],
    }
    json_write(output / "MANIFEST.json", manifest)


def backup(args, profile):
    output = Path(args.output).absolute()
    reject_links(output)
    output = output.resolve()
    disjoint(profile, output)
    if output.exists():
        raise ValueError("Backup destination already exists; choose a new directory")
    entries = selected(profile)
    if not entries:
        raise ValueError("No supported context entries were found")
    if not args.apply:
        return {"action": "backup", "applied": False, "entries": [p.name for p in entries]}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=output.parent))
    try:
        create_snapshot(profile, stage)
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"action": "backup", "applied": True, "files": len(json_read(output / "MANIFEST.json")["files"]), "output": str(output)}


def read_snapshot(source: Path, legacy: bool, on_plan=None):
    reject_links(source, recursive=False)
    if not source.is_dir():
        raise ValueError("Backup directory is missing")
    if legacy:
        mapping = {}
        directories = []

        def add_entry(path, target):
            name = path.relative_to(source).as_posix()
            safe_relative(name)
            rel = safe_relative(target)
            if rel.parts[0] not in DIRECTORIES + FILES and not (len(rel.parts) == 1 and rel.suffix == ".sqlite"):
                raise ValueError(f"Unsupported context entry: {target}")
            mapping[name] = target
            if on_plan is not None:
                on_plan({name: target}, [])

        # Capture already identifiable top-level files before walking the tree.
        for name in FILES:
            path = source / name
            reject_links(path, recursive=False)
            if path.is_file():
                add_entry(path, name)
        active = source / "databases" / "active"
        reject_links(active, recursive=False)
        for path in active.glob("*.sqlite"):
            reject_links(path, recursive=False)
            if path.is_file():
                add_entry(path, path.name)
        # Discover supported trees without following links and snapshot each
        # target as soon as its canonical source name is known.
        for name in DIRECTORIES + FILES:
            path = source / name
            reject_links(path, recursive=False)
            if not path.is_dir():
                continue
            for root, dirs, files in os.walk(path, followlinks=False):
                current = Path(root)
                reject_links(current, recursive=False)
                if name in DIRECTORIES:
                    directories.append(current.relative_to(source).as_posix())
                for child in dirs:
                    reject_links(current / child, recursive=False)
                for child in files:
                    item = current / child
                    reject_links(item, recursive=False)
                    if item.is_file():
                        add_entry(item, item.relative_to(source).as_posix())
        if not mapping:
            raise ValueError("Legacy snapshot contains no supported context entries")
    else:
        reject_links(source / "MANIFEST.json", recursive=False)
        manifest = json_read(source / "MANIFEST.json")
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported or invalid backup manifest")
        mapping = {name: name for name in manifest["files"]}
        directories = manifest.get("directories")
        if not isinstance(directories, list) or not all(isinstance(name, str) for name in directories):
            raise ValueError("Backup directories must be an array of canonical relative paths")
        if len(directories) != len(set(directories)):
            raise ValueError("Backup directory manifest contains duplicates")
    for name, target in mapping.items():
        safe_relative(name)
        rel = safe_relative(target)
        if rel.parts[0] not in DIRECTORIES + FILES and not (len(rel.parts) == 1 and rel.suffix == ".sqlite"):
            raise ValueError(f"Unsupported context entry: {target}")
    for name in directories:
        rel = safe_relative(name)
        if rel.parts[0] not in DIRECTORIES:
            raise ValueError(f"Unsupported context directory: {name}")
    if on_plan is not None:
        on_plan(mapping, directories)
    reject_links(source)
    if not legacy:
        actual_dirs = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_dir()}
        if actual_dirs != set(directories):
            raise ValueError("Backup directories do not match manifest")
        actual = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file() and p != source / "MANIFEST.json"}
        if actual != set(mapping):
            raise ValueError("Backup contents do not match manifest")
        for name, checksum in manifest["files"].items():
            path = source / safe_relative(name)
            if not path.is_file() or sha256(path) != checksum:
                raise ValueError(f"Backup checksum mismatch: {name}")
    for name in mapping:
        if (source / name).suffix in (".sqlite", ".db"):
            if any(Path(str(source / name) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("Snapshot SQLite has sidecars; checkpoint the source before restoring")
            verify_sqlite(source / name)
    return mapping, directories


def move_new(source: Path, target: Path):
    if os.name == 'nt':
        source.rename(target)
    elif source.is_dir():
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform.startswith('linux'):
            rename = getattr(libc, 'renameat2', None)
            if rename is None:
                raise OSError('Exclusive directory move is unavailable')
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        elif sys.platform == 'darwin':
            result = libc.renamex_np(os.fsencode(source), os.fsencode(target), 4)
        else:
            raise OSError('Exclusive directory move is unsupported on this platform')
        if result:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
    else:
        os.link(source, target, follow_symlinks=False)
        source.unlink()


def replace_with_safety(profile: Path, replacements: dict[Path, Path], directories=(), *, expected=None):
    """Install selected entries, retaining originals outside plugin scan paths."""
    previous = expected if expected is not None else {rel: path_state(profile / rel) for rel in replacements}
    if set(previous) != set(replacements):
        raise ValueError('Missing destination state')
    profile.mkdir(parents=True, exist_ok=True)
    safety = profile.parent / (profile.name + "-maintenance-backups") / uuid.uuid4().hex
    reject_links(safety)
    safety.mkdir(parents=True)
    changed = {}
    created_dirs = []
    directory_times = {}
    for rel in [*directories, *replacements]:
        for parent in (profile / rel).parents:
            if parent.exists() and parent not in directory_times:
                info = parent.stat()
                directory_times[parent] = (info.st_atime_ns, info.st_mtime_ns)
            if parent == profile:
                break
    stage = safety / ".staging"
    stage.mkdir()
    staged = {}
    for i, (rel, source) in enumerate(replacements.items()):
        reject_links(profile / rel)
        staged[rel] = stage / str(i)
        copy_tree(source, staged[rel])

    def make_directory(path):
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for directory in reversed(missing):
            directory.mkdir()
            created_dirs.append(directory)

    def remove_readonly(function, path, _error):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    try:
        for rel in directories:
            make_directory(profile / rel)
        for rel, source in staged.items():
            target = profile / rel
            reject_links(target)
            require_state(target, previous[rel])
            expected_output = path_state(source)
            prior = previous[rel] is not None
            if prior:
                (safety / rel).parent.mkdir(parents=True, exist_ok=True)
                target.rename(safety / rel)
                changed[rel] = (True, None)
                require_state(safety / rel, previous[rel])
            else:
                changed[rel] = (False, None)
            make_directory(target.parent)
            reject_links(target)
            move_new(source, target)
            changed[rel] = (prior, expected_output)
    except BaseException as error:
        failures = []
        for index, (rel, (prior, installed)) in enumerate(reversed(list(changed.items()))):
            target = profile / rel
            try:
                if installed is not None and os.path.lexists(target):
                    held = stage / f'discard-{index}'
                    target.rename(held)
                    if path_state(held) != installed:
                        try:
                            move_new(held, target)
                        except OSError as exc:
                            failures.append(f'Changed output retained at {held}: {exc}')
                        else:
                            failures.append(f'Changed output retained at {target}')
                        continue
                if prior:
                    move_new(safety / rel, target)
            except (OSError, ValueError) as exc:
                failures.append(f'{target}: {exc}')
        for target in reversed(created_dirs):
            try:
                target.rmdir()
            except OSError:
                pass
        if failures:
            raise OSError(f'Could not complete rollback; recovery files in {safety}: ' + '; '.join(failures)) from error
        for directory, times in directory_times.items():
            os.utime(directory, ns=times)
        cleanup_temporary_directory(stage, onerror=remove_readonly)
        raise
    cleanup_temporary_directory(stage, onerror=remove_readonly)
    return safety


def restore(args, profile):
    source = Path(args.backup).absolute()
    reject_links(source, recursive=False)
    source = source.resolve()
    disjoint(source, profile)
    initial = {}

    def capture_targets(mapping, _directories):
        for target in mapping.values():
            if target == "config.toml" and not args.restore_config:
                target = "config.before-reinstall-reference.toml"
            rel = safe_relative(target)
            reject_links(profile / rel, recursive=False)
            if (profile / rel).is_dir():
                raise ValueError(f"Restore file conflicts with a target directory: {target}")
            if rel not in initial:
                initial[rel] = path_state(profile / rel)

    mapping, directories = read_snapshot(source, args.legacy_layout, capture_targets if args.apply else None)
    replacements = {}
    for name, target in mapping.items():
        if target == "config.toml" and not args.restore_config:
            target = "config.before-reinstall-reference.toml"
        if (profile / target).is_dir():
            raise ValueError(f"Restore file conflicts with a target directory: {target}")
        replacements[Path(target)] = source / name
        if Path(target).suffix in (".sqlite", ".db"):
            # A stale WAL can replay pages from the old target over restored data.
            for suffix in ("-wal", "-shm", "-journal"):
                if (profile / (target + suffix)).exists():
                    raise ValueError("Target SQLite has WAL/SHM/rollback-journal sidecars; close/checkpoint the target before restoring")
    for name in directories:
        target = profile / safe_relative(name)
        if target.exists() and not target.is_dir():
            raise ValueError(f"Restore directory conflicts with a target file: {name}")
    if not args.apply:
        return {"action": "restore", "applied": False, "files": len(replacements), "legacy_unverified_checksums": args.legacy_layout}
    safety = replace_with_safety(profile, replacements, [safe_relative(p) for p in directories], expected=initial)
    return {"action": "restore", "applied": True, "files": len(replacements), "safety": str(safety)}


def toml_replace(text: str, header: str, lines: list[str]):
    # Parse first; headers embedded in multiline strings must never be mistaken
    # for actual tables. We reject multiline strings for this surgical editor.
    tomllib.loads(text)
    if '\"\"\"' in text or "'''" in text:
        raise ValueError("Multiline TOML strings require manual editing; no files changed")
    pattern = r"(?ms)^[ \t]*" + re.escape(header) + r"[ \t]*(?:#[^\n]*)?\r?\n.*?(?=^[ \t]*\[|\Z)"
    block = header + "\n" + "\n".join(lines) + "\n"
    if re.search(pattern, text):
        return re.sub(pattern, lambda m: block, text)
    return text.rstrip() + "\n\n" + block



def trust_assignment(line: str) -> str:
    """Edit a parsed scalar trust key, preserving quoting and its inline comment."""
    try:
        assignment = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return line
    if set(assignment) != {"trust_level"} or isinstance(assignment["trust_level"], dict):
        return line
    key, _, value = line.partition("=")
    quote = None
    escaped = False
    comment = ""
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote and char == quote:
            quote = None
        elif not quote and char in ("'", '"'):
            quote = char
        elif not quote and char == "#":
            comment = " " + value[index:]
            break
    return key + '= "trusted"' + comment


def repair_config(args, profile):
    config = profile / "config.toml"
    state_paths = [profile / ".codex-global-state.json"]
    if (profile / ".codex-global-state.json.bak").exists():
        state_paths.append(profile / ".codex-global-state.json.bak")
    roots = json_read(Path(args.roots))
    if not isinstance(roots, list) or not roots or not all(isinstance(p, str) for p in roots):
        raise ValueError("Roots file must be a non-empty JSON array of existing directories")
    roots = list(dict.fromkeys(str(Path(p).resolve()) for p in roots))
    if any(not Path(p).is_dir() for p in roots):
        raise ValueError("A project root is missing")
    initial = {Path(p.name): path_state(p) for p in [config, *state_paths]}
    text = config.read_text(encoding="utf-8-sig")
    parsed = tomllib.loads(text)
    states = {p.name: json_read(p) for p in state_paths}
    if any(not isinstance(s, dict) for s in states.values()):
        raise ValueError("Global state must be a JSON object")
    if '\"\"\"' in text or "'''" in text:
        raise ValueError("Multiline TOML strings require manual editing; no files changed")
    # Retain project options other than trust_level, including old project entries.
    existing = parsed.get("projects", {})
    for root in roots:
        values = existing.get(root, {})
        header = "[projects." + json.dumps(root) + "]"
        # Match any legal header quoting for the same parsed key.
        for line in text.splitlines():
            candidate = line.strip()
            if candidate.startswith("[projects."):
                try:
                    key = next(iter(tomllib.loads(candidate).get("projects", {})))
                except (tomllib.TOMLDecodeError, StopIteration):
                    continue
                if key == root:
                    header = candidate
                    break
        if values:
            match = re.search(r"(?ms)^[ \t]*" + re.escape(header) + r"[ \t]*\r?\n(.*?)(?=^[ \t]*\[|\Z)", text)
            if not match:
                raise ValueError("Cannot safely edit this existing project table; no files changed")
            body = "\n".join(trust_assignment(line) for line in match.group(1).splitlines()).rstrip()
            if "trust_level" not in values:
                body += '\ntrust_level = "trusted"'
            text = toml_replace(text, header, body.splitlines())
        else:
            text = toml_replace(text, header, ['trust_level = "trusted"'])
    edited = tomllib.loads(text)
    if any(edited.get("projects", {}).get(root, {}).get("trust_level") != "trusted" for root in roots):
        raise ValueError("Cannot safely set this project trust assignment; no files changed")
    for state in states.values():
        state["electron-saved-workspace-roots"] = roots
        state["project-order"] = roots
        state["active-workspace-roots"] = [roots[0]]
    if not args.apply:
        return {"action": "repair-config", "applied": False, "roots": len(roots), "state_files": len(states)}
    tmp = tempfile.mkdtemp(prefix=".repair-", dir=profile.parent)
    try:
        stage = Path(tmp)
        (stage / "config.toml").write_text(text, encoding="utf-8")
        for name, state in states.items():
            json_write(stage / name, state)
        safety = replace_with_safety(profile, {Path(name): stage / name for name in ["config.toml", *states]}, expected=initial)
    finally:
        cleanup_temporary_directory(tmp)
    return {"action": "repair-config", "applied": True, "roots": len(roots), "safety": str(safety)}


def repair_plugins(args, profile):
    plan_path = Path(args.plan).resolve()
    plan = json_read(plan_path)
    entries = plan.get("copies")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Plugin plan needs a non-empty copies array")
    replacements = {}
    initial = {}
    for entry in entries:
        rel = safe_relative(entry["destination"])
        initial[rel] = path_state(profile / rel)
        if rel.parts[0] not in ("plugins", ".tmp", "local-marketplaces"):
            raise ValueError("Plugin destination must be under plugins, .tmp or local-marketplaces")
        source = (plan_path.parent / entry["source"]).absolute()
        reject_links(source)
        source = source.resolve()
        disjoint(source, profile)
        if not source.is_dir():
            raise ValueError("Plugin source directory is missing")
        manifest = source / ".codex-plugin" / "plugin.json"
        market = source / ".agents" / "plugins" / "marketplace.json"
        if not manifest.is_file() and not market.is_file():
            raise ValueError("Source needs plugin.json or marketplace.json")
        for p in (manifest, market):
            if p.is_file() and not isinstance(json_read(p), dict):
                raise ValueError("Plugin manifest must be an object")
        for prior in replacements:
            if prior == rel or prior in rel.parents or rel in prior.parents:
                raise ValueError("Plugin destinations overlap")
        replacements[rel] = source
    initial[Path("config.toml")] = path_state(profile / "config.toml")
    text = (profile / "config.toml").read_text(encoding="utf-8-sig")
    original = tomllib.loads(text)
    for block in plan.get("config_blocks", []):
        header = block["header"]
        if not re.fullmatch(r'\[(?:plugins|marketplaces)\..+\]', header):
            raise ValueError("Only plugin and marketplace tables are supported")
        if not isinstance(block["lines"], list) or not all(isinstance(p, str) and "\n" not in p and "\r" not in p for p in block["lines"]):
            raise ValueError("Config lines must be single strings")
        text = toml_replace(text, header, block["lines"])
    edited = tomllib.loads(text)
    unrelated = lambda value: {key: item for key, item in value.items() if key not in ("plugins", "marketplaces")}
    if unrelated(original) != unrelated(edited):
        raise ValueError("Plugin plan attempted to alter an unrelated configuration table")
    if not args.apply:
        return {"action": "repair-plugins", "applied": False, "copies": len(entries)}
    tmp = tempfile.mkdtemp(prefix=".plugin-repair-", dir=profile.parent)
    try:
        stage = Path(tmp)
        (stage / "config.toml").write_text(text, encoding="utf-8")
        # Stage all sources before changing any destination.
        staged = {Path("config.toml"): stage / "config.toml"}
        for i, (rel, source) in enumerate(replacements.items()):
            copy_tree(source, stage / str(i))
            staged[rel] = stage / str(i)
        safety = replace_with_safety(profile, staged, expected=initial)
    finally:
        cleanup_temporary_directory(tmp)
    return {"action": "repair-plugins", "applied": True, "copies": len(entries), "safety": str(safety)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("backup", "restore", "repair-config", "repair-plugins"):
        sub = commands.add_parser(name)
        sub.add_argument("--profile", required=True)
        sub.add_argument("--apply", action="store_true")
        sub.add_argument("--allow-live-profile", action="store_true")
        if name == "backup":
            sub.add_argument("--output", required=True)
        elif name == "restore":
            sub.add_argument("--backup", required=True)
            sub.add_argument("--restore-config", action="store_true")
            sub.add_argument("--legacy-layout", action="store_true", help="Import the older databases/active layout; its old manifest is not trusted")
        elif name == "repair-config":
            sub.add_argument("--roots", required=True)
        else:
            sub.add_argument("--plan", required=True)
    args = parser.parse_args()
    try:
        profile = check_profile(args)
        function = {"backup": backup, "restore": restore, "repair-config": repair_config, "repair-plugins": repair_plugins}[args.action]
        print(json.dumps(function(args, profile), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(json.dumps({"action": args.action, "applied": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
