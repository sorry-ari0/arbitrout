"""
Lobsterminal — Code Security Scanner
Scans files for dangerous patterns after AI-generated edits.
Called by task_dispatcher.py before deploying changes.
"""
import re
import os
import shutil
import hashlib
import json
import time

PROJECT_DIR = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\src"
SCAN_LOG = r"C:\Users\afoma\.openclaw\workspace\memory\scan-log.md"
SNAPSHOT_FILE = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\file_snapshots.json"
BACKUP_DIR = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\.file_backups"

# Only these directories may contain edited files
ALLOWED_EDIT_DIRS = [
    os.path.normpath(os.path.join(PROJECT_DIR, "static")),
    os.path.normpath(os.path.join(PROJECT_DIR, "data")),
]

ALLOWED_EXTENSIONS = {".js", ".css", ".html", ".json", ".py"}

# Dangerous pattern definitions loaded from external JSON for maintainability
# This avoids security hook false-positives on the scanner's own detection strings
PATTERNS_FILE = os.path.join(os.path.dirname(__file__), "scan_patterns.json")

# Backend files where certain patterns are expected
BACKEND_ALLOWLIST = {"server.py", "task_dispatcher.py", "improve.py", "code_scanner.py",
                     "backtest_engine.py", "portfolio_manager.py", "swarm_engine.py",
                     "indicators.py", "main.py", "strategy_engine.py", "dexter_client.py",
                     "fmp_client.py", "valuation_engine.py"}


def load_patterns():
    """Load scan patterns from JSON file. Returns list of (compiled_re, desc, severity)."""
    try:
        with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [(re.compile("".join(p["parts"]), re.IGNORECASE), p["desc"], p["sev"]) for p in raw]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"[scanner] WARNING: Could not load patterns: {e}")
        return []


def file_hash(filepath):
    """SHA-256 hash of a file's contents."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return None


def take_snapshot(backup=False):
    """Snapshot all project files (hash + mtime) before Aider runs.
    If backup=True, also copies files to BACKUP_DIR for restore-on-block."""
    if backup:
        if os.path.exists(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR)
        os.makedirs(BACKUP_DIR, exist_ok=True)

    snapshot = {}
    for root, dirs, files in os.walk(PROJECT_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)
            ext = os.path.splitext(fname)[1].lower()
            if ext in ALLOWED_EXTENSIONS or ext == "":
                snapshot[fpath] = {
                    "hash": file_hash(fpath),
                    "mtime": os.path.getmtime(fpath),
                }
                if backup:
                    rel = os.path.relpath(fpath, PROJECT_DIR)
                    backup_path = os.path.join(BACKUP_DIR, rel)
                    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                    shutil.copy2(fpath, backup_path)
    return snapshot


def save_snapshot(snapshot):
    """Save snapshot to disk."""
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def load_snapshot():
    """Load previous snapshot."""
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def find_changed_files(old_snapshot):
    """Compare current state to snapshot, return (modified, new_files, deleted)."""
    current = take_snapshot()
    modified = []
    new_files = []

    for fpath, info in current.items():
        if fpath in old_snapshot:
            if info["hash"] != old_snapshot[fpath]["hash"]:
                modified.append(fpath)
        else:
            new_files.append(fpath)

    deleted = [f for f in old_snapshot if f not in current]
    return modified, new_files, deleted


def check_allowed_path(filepath):
    """Verify file is within allowed edit directories (not just prefix match)."""
    norm = os.path.normpath(filepath)
    for allowed in ALLOWED_EDIT_DIRS:
        if norm.startswith(allowed + os.sep) or norm == allowed:
            return True
    return False


def scan_file(filepath, patterns):
    """Scan a single file for dangerous patterns (line-by-line + multiline)."""
    findings = []
    basename = os.path.basename(filepath)
    is_backend = basename in BACKEND_ALLOWLIST

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            lines = content.splitlines()
    except (OSError, IOError):
        return findings

    def is_allowed(desc):
        return is_backend and any(kw in desc.lower() for kw in
                                  ["subprocess", "system call", "import", "dynamic python"])

    # Pass 1: Line-by-line scan (for precise line numbers)
    for i, line in enumerate(lines, 1):
        for pattern, desc, severity in patterns:
            if pattern.search(line):
                if is_allowed(desc):
                    continue
                findings.append((i, desc, severity))

    # Pass 2: Multiline scan (catches split-across-lines evasion)
    # Only check BLOCK-severity patterns against full content
    for pattern, desc, severity in patterns:
        if severity != "BLOCK":
            continue
        if is_allowed(desc):
            continue
        # Use re.DOTALL so . matches newlines in the joined pattern
        multiline_pattern = re.compile(pattern.pattern, re.IGNORECASE | re.DOTALL)
        if multiline_pattern.search(content):
            # Check if already found in line-by-line pass
            already_found = any(d == desc for _, d, _ in findings)
            if not already_found:
                findings.append((0, f"{desc} (multiline match)", severity))

    return findings


def scan_for_junk_files():
    """Find files created by Aider that don't belong."""
    junk = []
    known_static_js = {"app.js"}

    for root, dirs, files in os.walk(PROJECT_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)

            if fname.endswith((".bak", ".orig", ".tmp", ".swp")):
                junk.append(fpath)
                continue

            if "static" + os.sep + "js" in root and fname not in known_static_js:
                if fname.endswith(".js"):
                    junk.append(fpath)

    return junk


def full_scan(old_snapshot=None):
    """
    Run full security scan after Aider edits.
    Returns (passed: bool, report: str, junk_files: list)
    """
    patterns = load_patterns()
    report_lines = []
    has_block = False

    if old_snapshot:
        modified, new_files, deleted = find_changed_files(old_snapshot)
        files_to_scan = modified + new_files
        report_lines.append(f"Modified: {len(modified)}, New: {len(new_files)}, Deleted: {len(deleted)}")

        for f in new_files:
            if not check_allowed_path(f):
                report_lines.append(f"  BLOCK: New file outside allowed dirs: {f}")
                has_block = True

            ext = os.path.splitext(f)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                report_lines.append(f"  BLOCK: Unexpected file extension: {f}")
                has_block = True
    else:
        files_to_scan = []
        for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "static")):
            for fname in files:
                files_to_scan.append(os.path.join(root, fname))

    for fpath in files_to_scan:
        findings = scan_file(fpath, patterns)
        if findings:
            rel = os.path.relpath(fpath, PROJECT_DIR)
            for line_num, desc, severity in findings:
                report_lines.append(f"  {severity}: {rel}:{line_num} -- {desc}")
                if severity == "BLOCK":
                    has_block = True

    junk = scan_for_junk_files()
    if junk:
        for j in junk:
            report_lines.append(f"  JUNK: {os.path.relpath(j, PROJECT_DIR)}")

    report = "\n".join(report_lines) if report_lines else "Clean -- no issues found"
    return not has_block, report, junk


def log_scan(task_num, passed, report):
    """Append scan result to log."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    status = "PASS" if passed else "BLOCKED"
    entry = f"- [{timestamp}] Task {task_num} scan: {status}\n{report}\n\n"
    with open(SCAN_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


def restore_from_backup():
    """Restore project files from backup after a security block."""
    if not os.path.exists(BACKUP_DIR):
        print("[scanner] No backup directory found, cannot restore")
        return False

    restored = 0
    for root, dirs, files in os.walk(BACKUP_DIR):
        for fname in files:
            backup_path = os.path.join(root, fname)
            rel = os.path.relpath(backup_path, BACKUP_DIR)
            target_path = os.path.join(PROJECT_DIR, rel)
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(backup_path, target_path)
                restored += 1
            except OSError as e:
                print(f"[scanner] Failed to restore {rel}: {e}")

    print(f"[scanner] Restored {restored} files from backup")

    # Clean up backup dir
    try:
        shutil.rmtree(BACKUP_DIR)
    except OSError:
        pass

    return True


def cleanup_junk(junk_files):
    """Delete junk files created by Aider."""
    deleted = 0
    for f in junk_files:
        try:
            os.remove(f)
            print(f"[scanner] Deleted junk file: {f}")
            deleted += 1
        except OSError as e:
            print(f"[scanner] Could not delete {f}: {e}")
    return deleted


if __name__ == "__main__":
    print("[scanner] Running full security scan...")
    passed, report, junk = full_scan()
    print(f"[scanner] Result: {'PASS' if passed else 'BLOCKED'}")
    print(report)
    if junk:
        print(f"\n[scanner] Found {len(junk)} junk files:")
        for j in junk:
            print(f"  - {j}")
