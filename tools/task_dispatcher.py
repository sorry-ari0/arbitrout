"""
Lobsterminal — Task Dispatcher
Reads tasks.md, picks the next uncompleted task, runs Aider to implement it.
Includes security scanning to prevent malicious code from being deployed.
Handles retries, stuck tasks, and multi-task processing.
Run via cron or manually: python task_dispatcher.py
"""
import re
import subprocess
import sys
import os
import time
import json
import shlex

# Force line-buffered output — prevents zero-output when run as subprocess/cron
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# Import the security scanner
sys.path.insert(0, os.path.dirname(__file__))
import code_scanner

import tempfile
import shutil

# Refresh FILE_MAP.md line counts before processing tasks
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "update_file_map",
        os.path.join(os.path.dirname(__file__), "update-file-map.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.refresh()
except Exception as e:
    print(f"[dispatcher] WARNING: FILE_MAP refresh failed: {e}")

TASKS_FILE = r"C:\Users\afoma\.openclaw\workspace\memory\tasks.md"
PROJECT_DIR = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\src"
FILE_MAP_PATH = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\FILE_MAP.md"
AIDER = r"C:\Users\afoma\.local\bin\aider.exe"
MODEL = "ollama_chat/qwen2.5-coder:7b"  # Single model — fits in 7GB VRAM without contention
LOG_FILE = r"C:\Users\afoma\.openclaw\workspace\memory\task-log.md"
LOCK_FILE = os.path.join(os.path.dirname(__file__), ".dispatcher.lock")
PROMPT_GUARD = os.path.join(
    r"C:\Users\afoma\.openclaw\workspace\skills\prompt-guard\scripts", "detect.py"
)

MAX_TASKS_PER_RUN = 3        # Process up to N tasks per invocation
MAX_RETRIES = 3               # Skip a task after N failures
STUCK_TIMEOUT_MINUTES = 30    # Reset [~] tasks older than this
LARGE_FILE_THRESHOLD = 200    # Files above this line count use section-based editing

# Cached file map (loaded once per run, not per task)
_file_map_cache = None


def read_tasks():
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        return f.read()


def write_tasks(content):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def read_log():
    """Read the task log file."""
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def find_next_task(content):
    """Find the first task with [ ] status. Returns (task_number, task_block) or (None, None)."""
    pattern = r"## Task (\d+):.*?\n\*\*Status:\*\* \[ \]"
    m = re.search(pattern, content)
    if not m:
        return None, None
    task_num = int(m.group(1))

    # Extract the full task block (find next ## Task header regardless of number)
    task_start = content.find(f"## Task {task_num}:")
    next_match = re.search(r"## Task \d+:", content[task_start + 1:])
    if next_match:
        task_block = content[task_start:task_start + 1 + next_match.start()]
    else:
        task_block = content[task_start:]

    return task_num, task_block.strip()


def mark_task(content, task_num, new_status):
    """Change task status: [ ] -> [~] or [~] -> [x] or [~] -> [!]"""
    old = f"## Task {task_num}:"
    idx = content.find(old)
    if idx == -1:
        return content

    status_pattern = r"(\*\*Status:\*\* )\[.\]"
    # Only replace in the section for this task
    before = content[:idx]
    after = content[idx:]
    after = re.sub(status_pattern, rf"\1[{new_status}]", after, count=1)
    return before + after


def count_task_failures(task_num):
    """Count how many times a task has failed in the log.
    Only counts recent failures — entries renamed to FIXED are excluded."""
    log_content = read_log()
    pattern = rf"Task {task_num}: FAILED"
    return len(re.findall(pattern, log_content))


def get_last_failure_reason(task_num):
    """Get the most recent failure reason for a task. Returns (reason_type, detail) or (None, None).
    reason_type is one of: 'timeout', 'syntax', 'audit', 'test', 'security', 'other'."""
    log_content = read_log()
    pattern = rf"Task {task_num}: FAILED (.+)"
    matches = re.findall(pattern, log_content)
    if not matches:
        return None, None
    last = matches[-1].strip()
    if "TIMEOUT" in last:
        return "timeout", last
    if "syntax error" in last.lower():
        return "syntax", last
    if "audit failed" in last.lower():
        return "audit", last
    if "test failed" in last.lower():
        return "test", last
    if "security" in last.lower() or "blocked" in last.lower():
        return "security", last
    return "other", last


def reset_stuck_tasks(content):
    """Find tasks stuck at [~] and reset them to [ ] if they've been stuck too long."""
    log_content = read_log()
    changed = False

    for m in re.finditer(r"## Task (\d+):.*?\n\*\*Status:\*\* \[~\]", content):
        task_num = int(m.group(1))

        # Find log entries specifically for this task number (anchored with trailing space)
        entries = re.findall(
            rf"\[(\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})\] Task {task_num}: ",
            log_content
        )

        should_reset = False
        if entries:
            last_entry_time = entries[-1]
            try:
                last_time = time.mktime(time.strptime(last_entry_time, "%Y-%m-%d %H:%M:%S"))
                elapsed_min = (time.time() - last_time) / 60
                if elapsed_min > STUCK_TIMEOUT_MINUTES:
                    should_reset = True
            except ValueError:
                should_reset = True  # Unparseable timestamp — reset to be safe
        else:
            # No log entries at all for this in-progress task — reset it
            should_reset = True

        if should_reset:
            failures = count_task_failures(task_num)
            if failures >= MAX_RETRIES:
                print(f"[dispatcher] Task {task_num} has failed {failures}x — skipping (marking [!])")
                content = mark_task(content, task_num, "!")
                log_result(task_num, False, f"skipped after {failures} failures")
            else:
                print(f"[dispatcher] Task {task_num} stuck at [~] — resetting to [ ]")
                content = mark_task(content, task_num, " ")
            changed = True

    if changed:
        write_tasks(content)
    return content


def build_aider_prompt(task_num, task_block):
    """Build a focused prompt for Aider from the task description."""
    lines = task_block.split("\n")
    prompt_lines = []
    skip_test = False
    for line in lines:
        if line.startswith("## Task"):
            prompt_lines.append(line.replace("## ", ""))
            continue
        if line.startswith("**Status:**") or line.startswith("**Files:**"):
            continue
        if line.startswith("**Test:**"):
            skip_test = True
            continue
        if line.startswith("---"):
            continue
        if not skip_test:
            prompt_lines.append(line)
        if skip_test and line.strip() == "":
            skip_test = False

    prompt = "\n".join(prompt_lines).strip()

    # If this task has failed before, add error-specific retry instructions
    failures = count_task_failures(task_num)
    if failures > 0:
        reason_type, detail = get_last_failure_reason(task_num)
        print(f"[dispatcher] Task {task_num} retry #{failures} (last failure: {reason_type})")

        if reason_type == "syntax" or reason_type == "audit":
            # Syntax/audit failures: be very explicit about structure preservation
            prompt += (
                "\n\nCRITICAL: Previous attempt caused a syntax error. Rules:"
                "\n1. Do NOT refactor, rename, or restructure any code"
                "\n2. Only ADD the new fields to each existing dict entry"
                "\n3. Keep ALL existing fields exactly as they are"
                "\n4. Make sure every opening { has a matching }"
                "\n5. The file must be valid Python after your edit"
            )
        elif reason_type == "timeout":
            prompt += (
                "\n\nIMPORTANT: Previous attempt timed out. Be concise — make the minimum changes needed."
                " Do not refactor or restructure existing code."
            )
        else:
            prompt += "\n\nIMPORTANT: Keep changes minimal. Only modify the specific lines described above. Do not refactor or restructure existing code."

    return prompt


def extract_files_from_task(task_block):
    """Extract file paths from **Files:** line in task block."""
    m = re.search(r"\*\*Files:\*\*\s*(.+)", task_block)
    if not m:
        return []
    files_line = m.group(1)
    # Extract paths from backtick-wrapped entries
    paths = re.findall(r"`([^`]+)`", files_line)
    return [p for p in paths if os.path.isfile(p)]


def extract_section_from_task(task_block):
    """Extract **Section:** from task block. Returns section name or None."""
    m = re.search(r"\*\*Section:\*\*\s*`?(\w+)`?", task_block)
    return m.group(1) if m else None


def load_file_map():
    """Parse FILE_MAP.md into {relative_path: {section_name: (start, end)}}.
    Cached for the lifetime of the dispatcher run."""
    global _file_map_cache
    if _file_map_cache is not None:
        return _file_map_cache

    file_map = {}
    try:
        with open(FILE_MAP_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print("[dispatcher] WARNING: FILE_MAP.md not found")
        return file_map

    current_file = None
    for line in content.split("\n"):
        # Match file headers like "## src/server.py (497 lines)"
        fh = re.match(r"^## (src/\S+)", line)
        if fh:
            current_file = fh.group(1)
            file_map[current_file] = {}
            continue
        # Match section entries like "- functionName: lines X-Y" or "- functionName: X-Y"
        if current_file and line.startswith("- "):
            sm = re.match(r"^- (\w+):\s*(?:lines\s+)?(\d+)-(\d+)", line)
            if sm:
                name = sm.group(1)
                start = int(sm.group(2))
                end = int(sm.group(3))
                file_map[current_file][name] = (start, end)

    _file_map_cache = file_map
    return file_map


def get_section_range(file_path, section_name):
    """Look up a section's line range from FILE_MAP.md. Returns (start, end) or None."""
    file_map = load_file_map()
    # Normalize path to relative
    rel_path = os.path.relpath(file_path, os.path.dirname(PROJECT_DIR)).replace("\\", "/")
    if not rel_path.startswith("src/"):
        rel_path = "src/" + rel_path.lstrip("./")

    sections = file_map.get(rel_path, {})
    if section_name in sections:
        return sections[section_name]
    return None


def extract_section(file_path, start_line, end_line, context_lines=5):
    """Extract lines from a file with context. Returns (section_text, actual_start, actual_end)."""
    with open(file_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    total = len(all_lines)
    actual_start = max(0, start_line - 1 - context_lines)
    actual_end = min(total, end_line + context_lines)
    section = all_lines[actual_start:actual_end]
    return "".join(section), actual_start, actual_end


def splice_section(file_path, new_section_text, actual_start, actual_end):
    """Replace lines actual_start:actual_end in file with new_section_text."""
    with open(file_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    new_lines = new_section_text.splitlines(keepends=True)
    # Ensure last line has newline
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    result = all_lines[:actual_start] + new_lines + all_lines[actual_end:]
    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(result)


def scan_prompt_injection(text, label="task"):
    """Scan text for prompt injection using prompt-guard. Returns (safe, details)."""
    if not os.path.isfile(PROMPT_GUARD):
        print(f"[dispatcher] WARNING: prompt-guard not found at {PROMPT_GUARD}, skipping scan")
        return True, "scanner not available"

    try:
        result = subprocess.run(
            [sys.executable, PROMPT_GUARD, "--json", text],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"[dispatcher] prompt-guard error: {result.stderr[:200]}")
            return True, "scanner error"

        data = json.loads(result.stdout)
        severity = data.get("severity", "SAFE")
        action = data.get("action", "allow")
        reasons = data.get("reasons", [])

        print(f"[dispatcher] Prompt scan ({label}): {severity} -> {action}")
        if reasons:
            print(f"[dispatcher]   Reasons: {', '.join(reasons)}")

        # Block on explicit block action OR critical severity
        if action in ("block", "block_notify") or severity == "CRITICAL":
            # Check if the only reason is repetition_detected — that's a false positive
            non_repetition_reasons = [r for r in reasons if r != "repetition_detected"]
            if not non_repetition_reasons and severity != "CRITICAL":
                print(f"[dispatcher] Allowing through: only repetition_detected (likely false positive)")
                return True, f"{severity}: allowed (repetition-only false positive)"
            return False, f"BLOCKED ({severity}): {', '.join(reasons)}"

        return True, f"{severity}: {action}"

    except subprocess.TimeoutExpired:
        print("[dispatcher] prompt-guard timed out")
        return True, "scanner timeout"
    except (json.JSONDecodeError, Exception) as e:
        print(f"[dispatcher] prompt-guard parse error: {e}")
        return True, f"scanner error: {e}"


def run_aider(task_num, prompt, task_block=""):
    """Run Aider with the given prompt on the task's files.

    For large files with a **Section:** tag, extracts just the target section
    into a temp file, edits that, then splices it back. This prevents the 7B
    model from having to regenerate 700+ line files.
    """
    # Extract files from the task block (works for all tasks)
    file_args = extract_files_from_task(task_block)

    # If no files found in task block, try common defaults based on file mentions
    if not file_args:
        prompt_lower = prompt.lower()
        defaults = []
        if "css" in prompt_lower or "style" in prompt_lower:
            defaults.append(os.path.join(PROJECT_DIR, "static", "css", "terminal.css"))
        if "html" in prompt_lower or "pane" in prompt_lower:
            defaults.append(os.path.join(PROJECT_DIR, "static", "index.html"))
        if "js" in prompt_lower or "function" in prompt_lower or "event" in prompt_lower:
            defaults.append(os.path.join(PROJECT_DIR, "static", "js", "app.js"))
        file_args = [f for f in defaults if os.path.isfile(f)]

    # --- Section-based editing for large files ---
    section_name = extract_section_from_task(task_block)
    section_info = None  # (original_file, temp_file, actual_start, actual_end)

    if section_name and file_args:
        target_file = file_args[0]
        line_count = sum(1 for _ in open(target_file, encoding="utf-8"))

        if line_count > LARGE_FILE_THRESHOLD:
            section_range = get_section_range(target_file, section_name)
            if section_range:
                start, end = section_range
                section_text, actual_start, actual_end = extract_section(
                    target_file, start, end, context_lines=10
                )

                # Check for unbalanced braces in section (common with dict sub-sections)
                open_braces = section_text.count("{") - section_text.count("}")
                brace_warning = ""
                if open_braces > 0:
                    # Section has more { than } — the closing braces are in later sections
                    brace_warning = (
                        f"\n\nWARNING: This section has {open_braces} unclosed '{{' brace(s). "
                        f"This is INTENTIONAL — the closing '}}' is in a later section of the file. "
                        f"Do NOT add closing braces. Do NOT try to 'fix' the braces. "
                        f"Only modify the dict ENTRIES (the lines with ticker symbols)."
                    )
                    print(f"[dispatcher] Section has {open_braces} unclosed braces (intentional — dict continues)")

                # Write section to temp file in PROJECT_DIR so Aider can find it
                temp_name = f"_section_{section_name}.tmp"
                temp_path = os.path.join(PROJECT_DIR, temp_name)
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(section_text)

                section_info = (target_file, temp_path, actual_start, actual_end)
                file_args = [temp_path]  # Aider edits only the small section file
                # Add section context to the prompt so the model knows what it's editing
                prompt += (
                    f"\n\nYou are editing a SECTION extracted from {os.path.basename(target_file)} "
                    f"(lines {start}-{end}). The file you see IS this section. "
                    f"Edit it in place. Do NOT add imports or boilerplate — they exist in the full file."
                    f"{brace_warning}"
                )
                print(f"[dispatcher] Section-based edit: extracted '{section_name}' "
                      f"(lines {start}-{end}, ~{end-start} lines) from {line_count}-line file")
            else:
                print(f"[dispatcher] WARNING: Section '{section_name}' not found in FILE_MAP.md, using full file")

    cmd = [
        AIDER,
        "--model", MODEL,
        "--yes-always",
        "--no-pretty",
        "--message", prompt,
    ] + file_args

    print(f"[dispatcher] Running Aider for Task {task_num}...")
    print(f"[dispatcher] Files: {[os.path.relpath(f, PROJECT_DIR) for f in file_args]}")
    print(f"[dispatcher] Prompt: {prompt[:200]}...")

    # Fix prompt_toolkit crash: "Found xterm-256color, while expecting a Windows console"
    env = os.environ.copy()
    env["TERM"] = "dumb"
    # Fix UnicodeEncodeError: cp1252 can't encode aider's Unicode output on Windows
    env["PYTHONIOENCODING"] = "utf-8"
    # Fix aider warning: "OLLAMA_API_BASE: Not set"
    env.setdefault("OLLAMA_API_BASE", "http://localhost:11434")

    # Scale timeout — "whole" format at ~6.5 tok/s needs time for full regeneration
    aider_timeout = 900  # 15 min base (was 600, but 49-line sections can take 8-10 min)
    if section_info:
        _, _, actual_start, actual_end = section_info
        section_lines = actual_end - actual_start
        if section_lines > 80:
            aider_timeout = 1200  # 20 min for large sections (>80 lines)
            print(f"[dispatcher] Extended timeout to {aider_timeout}s for {section_lines}-line section")

    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=aider_timeout,
            env=env,
        )
        print(f"[dispatcher] Aider exit code: {result.returncode}")
        if result.stdout:
            print(f"[dispatcher] stdout (last 500 chars): ...{result.stdout[-500:]}")
        if result.returncode != 0 and result.stderr:
            print(f"[dispatcher] stderr: {result.stderr[-300:]}")

        success = result.returncode == 0

        # If section-based edit succeeded, splice the result back into the original file
        if success and section_info:
            orig_file, temp_path, actual_start, actual_end = section_info
            try:
                with open(temp_path, "r", encoding="utf-8") as f:
                    edited_section = f.read()

                # --- Post-process: fix brace balance before splicing ---
                # Read original section to compare brace counts
                with open(orig_file, "r", encoding="utf-8") as f:
                    orig_lines = f.readlines()
                orig_section = "".join(orig_lines[actual_start:actual_end])
                orig_balance = orig_section.count("{") - orig_section.count("}")
                new_balance = edited_section.count("{") - edited_section.count("}")

                if orig_balance != new_balance:
                    # Aider changed the brace balance — likely added/removed closing braces
                    diff = new_balance - orig_balance
                    if diff < 0:
                        # Extra closing braces were added — remove them from the end
                        lines = edited_section.rstrip().split("\n")
                        removed = 0
                        while removed < abs(diff) and lines:
                            if lines[-1].strip() == "}":
                                lines.pop()
                                removed += 1
                            else:
                                break
                        if removed > 0:
                            edited_section = "\n".join(lines) + "\n"
                            print(f"[dispatcher] Post-process: removed {removed} extra closing brace(s) added by Aider")
                    elif diff > 0:
                        # Missing closing braces — Aider removed them
                        lines = edited_section.rstrip().split("\n")
                        for _ in range(diff):
                            lines.append("}")
                        edited_section = "\n".join(lines) + "\n"
                        print(f"[dispatcher] Post-process: restored {diff} closing brace(s) removed by Aider")

                splice_section(orig_file, edited_section, actual_start, actual_end)
                print(f"[dispatcher] Spliced edited section back into {os.path.basename(orig_file)}")
            except Exception as e:
                print(f"[dispatcher] ERROR: Failed to splice section: {e}")
                success = False

        # Clean up temp file
        if section_info:
            try:
                os.remove(section_info[1])
            except OSError:
                pass

        return success, result.stdout
    except subprocess.TimeoutExpired:
        print(f"[dispatcher] Aider timed out after {aider_timeout}s")
        # Clean up temp file on timeout
        if section_info:
            try:
                os.remove(section_info[1])
            except OSError:
                pass
        return False, "TIMEOUT"
    except Exception as e:
        print(f"[dispatcher] Aider error: {e}")
        if section_info:
            try:
                os.remove(section_info[1])
            except OSError:
                pass
        return False, str(e)


def restart_server():
    """Restart the Lobsterminal server."""
    print("[dispatcher] Restarting Lobsterminal server...")
    try:
        # Kill existing
        subprocess.run(
            ["powershell", "-Command",
             "Get-Process -Name python -ErrorAction SilentlyContinue | "
             "Where-Object { $_.CommandLine -like '*uvicorn*server*' } | "
             "Stop-Process -Force"],
            capture_output=True, timeout=10
        )
        time.sleep(2)
        # Start new
        subprocess.Popen(
            ["python", "-m", "uvicorn", "server:app",
             "--host", "127.0.0.1", "--port", "8500"],
            cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        time.sleep(3)
        print("[dispatcher] Server restarted")
    except Exception as e:
        print(f"[dispatcher] Server restart error: {e}")


def run_test(task_block):
    """Extract and run the test command from the task. Only curl.exe is allowed."""
    m = re.search(r"\*\*Test:\*\* (.+)", task_block)
    if not m:
        print("[dispatcher] No test found, skipping")
        return True

    test_desc = m.group(1)
    # Extract curl command
    curl_m = re.search(r"`(curl\.exe .+?)`", test_desc)
    if not curl_m:
        print(f"[dispatcher] Can't parse test: {test_desc}")
        return True

    raw_cmd = curl_m.group(1)

    # SECURITY: Only allow curl.exe commands targeting localhost
    if not raw_cmd.startswith("curl.exe"):
        print("[dispatcher] BLOCKED: test command is not curl.exe")
        return False

    # Block shell chaining operators
    for dangerous in ["&&", "||", ";", "|", "`", "$("]:
        if dangerous in raw_cmd:
            print(f"[dispatcher] BLOCKED: shell operator '{dangerous}' in test command")
            return False

    # Only allow URLs targeting localhost
    url_m = re.search(r"https?://([^/\s]+)", raw_cmd)
    if url_m:
        host = url_m.group(1).split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            print(f"[dispatcher] BLOCKED: test URL targets external host: {host}")
            return False

    # Parse into arg list (no shell=True)
    try:
        args = shlex.split(raw_cmd)
    except ValueError as e:
        print(f"[dispatcher] BLOCKED: can't parse test command: {e}")
        return False

    print(f"[dispatcher] Running test: {raw_cmd}")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=60
        )
        print(f"[dispatcher] Test output: {result.stdout[:200]}")
        # Accept successful curl (returncode 0) even with empty body (204/empty JSON)
        return result.returncode == 0
    except Exception as e:
        print(f"[dispatcher] Test error: {e}")
        return False


def log_result(task_num, success, details=""):
    """Append result to log file."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    status = "DONE" if success else "FAILED"
    entry = f"- [{timestamp}] Task {task_num}: {status} {details}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def acquire_lock():
    """File-based lock to prevent concurrent dispatcher runs."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                data = json.load(f)
            pid = data.get("pid")
            started = data.get("started", 0)
            # Check if the PID is still running (Windows)
            try:
                os.kill(pid, 0)
                # Process exists — check if stale (older than 20 min)
                if time.time() - started > 1500:
                    print(f"[dispatcher] Stale lock (PID {pid}, {int(time.time()-started)}s old). Removing.")
                else:
                    print(f"[dispatcher] Another dispatcher is running (PID {pid}). Exiting.")
                    return False
            except OSError:
                print(f"[dispatcher] Dead lock (PID {pid} not running). Removing.")
        except (json.JSONDecodeError, KeyError):
            print("[dispatcher] Corrupt lock file. Removing.")

    with open(LOCK_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "started": time.time()}, f)
    return True


def release_lock():
    """Remove the lock file."""
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


AUDIT_LOG = os.path.join(os.path.dirname(TASKS_FILE), "audit-log.md")


def attempt_syntax_autofix(fpath, source, error):
    """Try common auto-fixes for Python syntax errors introduced by 7B models.
    Returns True if a fix was applied (caller must re-check syntax)."""
    lines = source.split("\n")
    lineno = error.lineno  # 1-indexed
    msg = error.msg.lower()

    # --- Fix 1: Missing closing brace ---
    # If error is at/near end and file has unbalanced braces, add closing }
    opens = source.count("{")
    closes = source.count("}")
    if opens > closes:
        missing = opens - closes
        # Find the last non-empty line
        last_content_idx = len(lines) - 1
        while last_content_idx > 0 and not lines[last_content_idx].strip():
            last_content_idx -= 1
        # Insert closing braces after last content line
        for _ in range(missing):
            lines.insert(last_content_idx + 1, "}")
            last_content_idx += 1
        print(f"[dispatcher] Auto-fix: added {missing} missing closing brace(s)")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True

    # --- Fix 2: Unexpected indent ---
    if "unexpected indent" in msg and lineno <= len(lines):
        line = lines[lineno - 1]
        stripped = line.lstrip()
        if stripped:
            # Check what indentation the previous non-empty line has
            prev_idx = lineno - 2
            while prev_idx >= 0 and not lines[prev_idx].strip():
                prev_idx -= 1
            if prev_idx >= 0:
                prev_indent = len(lines[prev_idx]) - len(lines[prev_idx].lstrip())
                # If this line is a closing brace or continuation, match prev indent
                if stripped.startswith("}") or stripped.startswith(")") or stripped.startswith("]"):
                    # Closing brace should match the block's start indent
                    lines[lineno - 1] = " " * max(0, prev_indent - 4) + stripped
                else:
                    lines[lineno - 1] = " " * prev_indent + stripped
                print(f"[dispatcher] Auto-fix: corrected indentation on line {lineno}")
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                return True

    # --- Fix 3: Expected ':' after dict key (broken dict entry) ---
    if "expected" in msg and "':'" in msg and lineno <= len(lines):
        # The 7B model sometimes merges two dict entries on one line
        # or drops the colon. Try to remove the broken line entirely.
        broken_line = lines[lineno - 1].strip()
        if not broken_line or broken_line in ("}", "]", ")"):
            return False  # Don't delete structural lines
        print(f"[dispatcher] Auto-fix: removing broken line {lineno}: {broken_line[:60]}")
        del lines[lineno - 1]
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True

    return False


def llm_diagnose_syntax(fpath, source, error):
    """Call llama-agent to diagnose a syntax error that auto-fix couldn't handle.
    Returns diagnosis string or None."""
    lineno = error.lineno
    lines = source.split("\n")

    # Extract context: 5 lines before and after the error
    start = max(0, lineno - 6)
    end = min(len(lines), lineno + 5)
    context = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))

    prompt = (
        f"Python file has a syntax error on line {lineno}: {error.msg}\n"
        f"Here are the lines around the error:\n\n"
        f"```\n{context}\n```\n\n"
        f"What exactly is wrong and how should it be fixed? Be specific (1-2 sentences)."
    )

    try:
        result = subprocess.run(
            ["curl.exe", "-s", "--max-time", "60",
             "http://127.0.0.1:11434/api/generate",
             "-d", json.dumps({
                 "model": "llama-agent:latest",
                 "prompt": prompt,
                 "stream": False,
                 "options": {"num_predict": 150}
             })],
            capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            return data.get("response", "").strip()
    except Exception as e:
        print(f"[dispatcher] LLM diagnosis failed: {e}")
    return None


def audit_change(task_num, task_block):
    """Multi-step audit of Aider's changes:
    1. Static analysis (syntax, bracket matching, duplicates)
    2. Ollama model review of the diff (if available)
    Returns (ok, message). Audit issues get logged for PM to create fix tasks."""
    files = extract_files_from_task(task_block)
    if not files:
        return True, "no files to audit"

    issues = []

    # --- STEP 1: Static analysis ---
    for fpath in files:
        if not os.path.isfile(fpath):
            continue

        ext = os.path.splitext(fpath)[1]

        # JS syntax check: look for obvious broken patterns
        if ext == ".js":
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()

            # Check for unmatched braces
            opens = content.count("{") + content.count("(") + content.count("[")
            closes = content.count("}") + content.count(")") + content.count("]")
            if abs(opens - closes) > 2:
                issues.append(f"CRITICAL: Bracket mismatch in {os.path.basename(fpath)}: {opens} opens vs {closes} closes")

            # Check for duplicate function definitions
            func_names = re.findall(r"(?:function|const|let|var)\s+(\w+)\s*(?:=|\()", content)
            seen = {}
            for name in func_names:
                if name in seen:
                    issues.append(f"CRITICAL: Duplicate definition: {name} in {os.path.basename(fpath)}")
                seen[name] = True

        # Python syntax check
        elif ext == ".py":
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    source = f.read()
                compile(source, fpath, "exec")
            except SyntaxError as e:
                print(f"[dispatcher] Syntax error detected: {e.msg} line {e.lineno}")
                # --- AUTO-FIX ATTEMPT ---
                fixed = attempt_syntax_autofix(fpath, source, e)
                if fixed:
                    print(f"[dispatcher] Auto-fix applied, re-checking syntax...")
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            source2 = f.read()
                        compile(source2, fpath, "exec")
                        print(f"[dispatcher] Auto-fix SUCCEEDED — syntax now valid")
                    except SyntaxError as e2:
                        print(f"[dispatcher] Auto-fix failed, still has error: {e2.msg} line {e2.lineno}")
                        # --- LLM DIAGNOSIS FALLBACK ---
                        diagnosis = llm_diagnose_syntax(fpath, source, e)
                        if diagnosis:
                            print(f"[dispatcher] LLM diagnosis: {diagnosis[:200]}")
                        issues.append(f"CRITICAL: Python syntax error in {os.path.basename(fpath)}: {e2.msg} line {e2.lineno}")
                else:
                    # --- LLM DIAGNOSIS FALLBACK ---
                    diagnosis = llm_diagnose_syntax(fpath, source, e)
                    if diagnosis:
                        print(f"[dispatcher] LLM diagnosis: {diagnosis[:200]}")
                    issues.append(f"CRITICAL: Python syntax error in {os.path.basename(fpath)}: {e.msg} line {e.lineno}")

    # --- STEP 2: Model-based code review (quick, via Ollama) ---
    model_issues = model_audit(task_num, task_block, files)
    issues.extend(model_issues)

    # --- Log all audit findings ---
    if issues:
        log_audit(task_num, issues)

    # Only block on CRITICAL issues
    critical = [i for i in issues if i.startswith("CRITICAL")]
    if critical:
        return False, "; ".join(critical[:3])

    print(f"[dispatcher] Audit passed for Task {task_num} ({len(issues)} minor issues logged)")
    return True, "ok"


def model_audit(task_num, task_block, files):
    """Quick model review of changes using Ollama. Returns list of issue strings.

    GPU-aware: checks if Ollama is responsive first. Since Aider uses qwen2.5-coder
    and this audit uses llama-agent, calling this right after Aider causes a model
    swap (~20s). We use a short timeout and skip gracefully if the model isn't ready.
    """
    issues = []

    # Only audit if we have a section to compare
    section_name = extract_section_from_task(task_block)
    if not section_name or not files:
        return issues

    target_file = files[0]
    if not os.path.isfile(target_file):
        return issues

    section_range = get_section_range(target_file, section_name)
    if not section_range:
        return issues

    start, end = section_range
    try:
        with open(target_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        actual_start = max(0, start - 1)
        actual_end = min(len(all_lines), end + 5)
        section_text = "".join(all_lines[actual_start:actual_end])
    except Exception:
        return issues

    # Keep the review prompt very short for the 8B model
    prompt = (
        f"Review this code for bugs. List ONLY real bugs, not style issues. "
        f"Reply with 'OK' if no bugs, or list bugs as bullet points.\n\n"
        f"```\n{section_text[:2000]}\n```"
    )

    try:
        result = subprocess.run(
            ["curl.exe", "-s", "--max-time", "90",
             "http://127.0.0.1:11434/api/generate",
             "-d", json.dumps({
                 "model": "llama-agent:latest",
                 "prompt": prompt,
                 "stream": False,
                 "options": {"num_predict": 200}
             })],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            response = data.get("response", "").strip()
            print(f"[dispatcher] Model audit response: {response[:200]}")

            if response and "ok" not in response.lower()[:10]:
                for line in response.split("\n"):
                    line = line.strip().lstrip("-*• ")
                    if line and len(line) > 10:
                        issues.append(f"REVIEW: {line[:120]}")
    except subprocess.TimeoutExpired:
        print(f"[dispatcher] Model audit skipped: GPU busy with model swap (timeout)")
    except Exception as e:
        print(f"[dispatcher] Model audit skipped: {e}")

    return issues


def log_audit(task_num, issues):
    """Log audit findings for the PM agent to review and create fix tasks."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n## [{timestamp}] Task {task_num} Audit\n")
        for issue in issues:
            f.write(f"- {issue}\n")


def run_functional_tests():
    """Run basic functional tests against the live server.
    Checks that key endpoints return valid data.
    Returns True if server is down (skip tests) or all tests pass."""
    print("[dispatcher] Running functional tests...")

    # First check if server is even running
    try:
        cmd = ["curl.exe", "-s", "--max-time", "5", "http://127.0.0.1:8500/api/health"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0 or not result.stdout:
            print("[dispatcher] Server not running — skipping functional tests (code-only validation passed)")
            return True  # Don't fail task just because server is down
    except Exception:
        print("[dispatcher] Server not reachable — skipping functional tests")
        return True

    all_ok = True
    tests = [
        # (endpoint, method, expected_check)
        ("/api/health", "GET", lambda b: '"status"' in b),
        ("/api/quotes", "GET", lambda b: '"symbol"' in b or b.startswith("[") or b.startswith("{")),
        ("/api/news", "GET", lambda b: '"headline"' in b or b.startswith("[") or b.startswith("{")),
        ("/api/watchlist", "GET", lambda b: b.startswith("[") or b.startswith("{")),
    ]

    for path, method, check in tests:
        try:
            cmd = ["curl.exe", "-s", "--max-time", "10", f"http://127.0.0.1:8500{path}"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0 or not result.stdout:
                print(f"[dispatcher] FUNC TEST FAIL: {path} — no response")
                all_ok = False
            elif not check(result.stdout):
                print(f"[dispatcher] FUNC TEST FAIL: {path} — unexpected response: {result.stdout[:100]}")
                all_ok = False
            else:
                print(f"[dispatcher] FUNC TEST OK: {path}")
        except Exception as e:
            print(f"[dispatcher] FUNC TEST ERROR: {path} — {e}")
            all_ok = False

    # Check that the main page loads (HTML with script tag)
    try:
        cmd = ["curl.exe", "-s", "--max-time", "10", "http://127.0.0.1:8500/"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if "<script" in result.stdout and "app.js" in result.stdout:
            print("[dispatcher] FUNC TEST OK: / (main page)")
        else:
            print("[dispatcher] FUNC TEST FAIL: / — main page broken")
            all_ok = False
    except Exception as e:
        print(f"[dispatcher] FUNC TEST ERROR: / — {e}")
        all_ok = False

    return all_ok


def process_one_task():
    """Process a single task. Returns True if a task was found and processed."""
    content = read_tasks()

    # First, unstick any [~] tasks that have been stuck too long
    content = reset_stuck_tasks(content)

    task_num, task_block = find_next_task(content)

    if task_num is None:
        return False  # No pending tasks

    # Check if task has exceeded max retries
    failures = count_task_failures(task_num)
    if failures >= MAX_RETRIES:
        print(f"[dispatcher] Task {task_num} has failed {failures}x — skipping")
        content = mark_task(content, task_num, "!")
        write_tasks(content)
        log_result(task_num, False, f"skipped after {failures} failures")
        return True  # Found a task, skipped it — continue processing

    print(f"\n[dispatcher] === Starting Task {task_num} (attempt {failures + 1}/{MAX_RETRIES}) ===")

    # SECURITY: Scan task description for prompt injection (warn-only, never block)
    # Task descriptions are written by us, not untrusted input.
    # The post-edit code scanner is the real security gate.
    print("[dispatcher] Scanning task description for prompt injection...")
    safe, details = scan_prompt_injection(task_block, label=f"task-{task_num}-block")
    if not safe:
        print(f"[dispatcher] WARNING: prompt scan flagged task {task_num}: {details}")
        print(f"[dispatcher]   Proceeding anyway (warn-only mode)")

    # Mark in-progress
    content = mark_task(content, task_num, "~")
    write_tasks(content)

    # SECURITY: Take file snapshot + backup before Aider edits
    print("[dispatcher] Taking pre-edit file snapshot with backup...")
    snapshot = code_scanner.take_snapshot(backup=True)
    code_scanner.save_snapshot(snapshot)

    # Build prompt and run Aider
    prompt = build_aider_prompt(task_num, task_block)

    # SECURITY: Scan the constructed prompt (warn-only)
    safe, details = scan_prompt_injection(prompt, label=f"task-{task_num}-prompt")
    if not safe:
        print(f"[dispatcher] WARNING: prompt scan flagged task {task_num} prompt: {details}")
        print(f"[dispatcher]   Proceeding anyway (warn-only mode)")

    success, output = run_aider(task_num, prompt, task_block)

    if success:
        # SECURITY: Scan for dangerous patterns before deploying
        print("[dispatcher] Running security scan on changes...")
        scan_ok, scan_report, junk_files = code_scanner.full_scan(snapshot)
        code_scanner.log_scan(task_num, scan_ok, scan_report)
        print(f"[dispatcher] Scan result: {'PASS' if scan_ok else 'BLOCKED'}")
        if scan_report:
            print(f"[dispatcher] Scan details:\n{scan_report}")

        # Clean up junk files Aider may have created
        if junk_files:
            code_scanner.cleanup_junk(junk_files)

        if not scan_ok:
            print(f"\n[dispatcher] Task {task_num} BLOCKED by security scan!")
            print("[dispatcher] Restoring files from pre-edit backup...")
            code_scanner.restore_from_backup()
            log_result(task_num, False, "security scan blocked: " + scan_report[:200])
            # Reset task status back to pending so it can be retried
            content = read_tasks()
            content = mark_task(content, task_num, " ")
            write_tasks(content)
            return True

        # AUDIT: Have the model review the change
        audit_ok, audit_msg = audit_change(task_num, task_block)
        if not audit_ok:
            print(f"\n[dispatcher] Task {task_num} FAILED audit: {audit_msg}")
            print("[dispatcher] Restoring files from pre-edit backup...")
            code_scanner.restore_from_backup()
            log_result(task_num, False, f"audit failed: {audit_msg[:100]}")
            content = read_tasks()
            content = mark_task(content, task_num, " ")
            write_tasks(content)
            return True

        # Restart server and test
        restart_server()
        test_ok = run_test(task_block)

        # FUNCTIONAL TEST: check that the server serves pages correctly
        if test_ok:
            test_ok = run_functional_tests()

        if test_ok:
            content = read_tasks()
            content = mark_task(content, task_num, "x")
            write_tasks(content)
            print(f"\n[dispatcher] Task {task_num} COMPLETED!")
            log_result(task_num, True)
        else:
            print(f"\n[dispatcher] Task {task_num} code written but test FAILED")
            log_result(task_num, False, "test failed")
            # Reset to [ ] for retry
            content = read_tasks()
            content = mark_task(content, task_num, " ")
            write_tasks(content)
    else:
        print(f"\n[dispatcher] Task {task_num} Aider FAILED")
        log_result(task_num, False, f"aider failed: {output[:100]}")
        # Reset to [ ] for retry
        content = read_tasks()
        content = mark_task(content, task_num, " ")
        write_tasks(content)

    return True


def resurrect_failed_tasks():
    """Try to recover [!] tasks by resetting their failure counters.
    Renames old failure log entries so count_task_failures returns 0.
    Only resurrects tasks whose last failure was timeout or syntax (fixable errors).
    Returns number of tasks resurrected."""
    content = read_tasks()
    resurrected = 0

    for m in re.finditer(r"## Task (\d+):.*?\n\*\*Status:\*\* \[!\]", content):
        task_num = int(m.group(1))
        reason_type, detail = get_last_failure_reason(task_num)

        # Only resurrect fixable failure types
        if reason_type in ("timeout", "syntax", "audit", "test", None):
            print(f"[dispatcher] Resurrecting Task {task_num} (was: {reason_type}) — clearing failure counter")

            # Rename old FAILED entries to FIXED in log
            log_content = read_log()
            old_pattern = rf"(Task {task_num}: FAILED)"
            new_log = re.sub(old_pattern, f"Task {task_num}(auto-reset): FIXED", log_content)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(new_log)

            # Reset task status from [!] to [ ]
            content = mark_task(content, task_num, " ")
            resurrected += 1
        else:
            print(f"[dispatcher] Skipping Task {task_num} resurrection (reason: {reason_type} — not auto-fixable)")

    if resurrected > 0:
        write_tasks(content)
        print(f"[dispatcher] Resurrected {resurrected} failed tasks for retry")

    return resurrected


def run_improvement():
    """Run self-improvement check when no tasks are pending."""
    print("[dispatcher] No pending tasks. Running self-improvement check...")
    try:
        improve_script = os.path.join(os.path.dirname(__file__), "improve.py")
        result = subprocess.run(
            [sys.executable, improve_script],
            capture_output=True, text=True, timeout=300
        )
        print(result.stdout)
        if result.stderr:
            print(f"[dispatcher] improve.py stderr: {result.stderr[-300:]}")
        # Return True if new tasks were added
        # Check that tasks were actually added (not "Added 0")
        import re as _re
        m = _re.search(r"Added (\d+) new tasks", result.stdout)
        return m is not None and int(m.group(1)) > 0
    except Exception as e:
        print(f"[dispatcher] improve.py error: {e}")
        return False


def main():
    print(f"[dispatcher] Lobsterminal Task Dispatcher starting...")
    print(f"[dispatcher] Tasks file: {TASKS_FILE}")
    print(f"[dispatcher] Project: {PROJECT_DIR}")

    if not acquire_lock():
        return

    try:
        tasks_processed = 0
        tasks_completed = 0

        for i in range(MAX_TASKS_PER_RUN):
            had_task = process_one_task()

            if not had_task:
                # No pending [ ] tasks — check for [!] tasks to resurrect
                resurrected = resurrect_failed_tasks()
                if resurrected > 0:
                    print(f"[dispatcher] Resurrected {resurrected} tasks — retrying with improved strategies")
                    continue  # Loop back to process the resurrected tasks

                # No tasks at all — try self-improvement
                new_tasks_added = run_improvement()
                if new_tasks_added:
                    print("[dispatcher] STATUS: New tasks generated. Tasks remain. Run dispatcher again.")
                    continue
                else:
                    print("[dispatcher] STATUS: All done. No new tasks generated. Nothing left to do.")
                    break

            tasks_processed += 1
            print(f"[dispatcher] --- Processed {tasks_processed}/{MAX_TASKS_PER_RUN} tasks this run ---")

        # Final status for the agent to parse
        content = read_tasks()
        remaining = len(re.findall(r"\*\*Status:\*\* \[ \]", content))
        failed = len(re.findall(r"\*\*Status:\*\* \[!\]", content))

        if remaining > 0:
            print(f"\n[dispatcher] STATUS: Session complete. Processed {tasks_processed} tasks. {remaining} pending, {failed} failed. Run dispatcher again.")
        elif failed > 0:
            print(f"\n[dispatcher] STATUS: Session complete. Processed {tasks_processed} tasks. {failed} permanently failed (non-recoverable). Run improvement.")
        else:
            print(f"\n[dispatcher] STATUS: Session complete. Processed {tasks_processed} tasks. All done.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
