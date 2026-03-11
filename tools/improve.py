"""
Lobsterminal — Self-Improvement Engine
Tests endpoints, analyzes code, identifies bugs and improvements, writes new tasks.
Run via cron after task_dispatcher.py completes all tasks.
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback

# Force line-buffered output — prevents zero-output in subprocess/cron contexts
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

PROJECT_DIR = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\src"
TASKS_FILE = r"C:\Users\afoma\.openclaw\workspace\memory\tasks.md"
LOG_FILE = r"C:\Users\afoma\.openclaw\workspace\memory\improve-log.md"
BASE_URL = "http://127.0.0.1:8500"

AIDER = r"C:\Users\afoma\.local\bin\aider.exe"
MODEL = "ollama_chat/qwen2.5-coder:7b"

# Max new tasks per improvement run (prevents queue flooding)
MAX_NEW_TASKS = 5


def log(msg):
    print(f"[improve] {msg}")


def curl(path, method="GET", data=None, timeout=30):
    """Make HTTP request, return (status_code, body_text)."""
    cmd = ["curl.exe", "-s", "-w", "\n%{http_code}", "--max-time", str(timeout)]
    if method == "POST":
        cmd += ["-X", "POST", "-H", "Content-Type: application/json"]
        if data:
            cmd += ["-d", json.dumps(data)]
    cmd.append(f"{BASE_URL}{path}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        lines = r.stdout.strip().rsplit("\n", 1)
        if len(lines) == 2:
            body, code = lines
            return int(code), body
        return 0, r.stdout
    except Exception as e:
        return 0, str(e)


def read_file(path):
    try:
        with open(os.path.join(PROJECT_DIR, path), "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def read_tasks():
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        return f.read()


def has_pending_tasks():
    """Check if there are actionable tasks ([ ] or [~]). Tasks marked [!] don't count."""
    content = read_tasks()
    return bool(re.search(r"\*\*Status:\*\* \[[ ~]\]", content))


def get_next_task_number():
    content = read_tasks()
    nums = re.findall(r"## Task (\d+):", content)
    return max(int(n) for n in nums) + 1 if nums else 1


def task_exists(title_keywords):
    """Check if a task with similar keywords already exists (pending or done)."""
    content = read_tasks().lower()
    # Check if all keywords appear in any single task block
    task_blocks = re.split(r"## Task \d+:", content)
    for block in task_blocks:
        if all(kw.lower() in block for kw in title_keywords):
            return True
    return False


def append_task(task_num, title, files, description, test="", section=""):
    """Add a task to the queue. Returns next task number, or same number if skipped.

    Args:
        section: Section name from FILE_MAP.md (REQUIRED for app.js and server.py tasks).
                 Enables section-based editing so Aider only processes ~50 lines
                 instead of the full 800-line file.
    """
    # Duplicate detection: check if similar task already exists
    title_words = [w for w in title.lower().split() if len(w) > 3]
    if task_exists(title_words):
        log(f"  Skipped duplicate: {title}")
        return task_num

    files_str = ", ".join(f"`{os.path.normpath(f)}`" for f in files)
    block = f"""

---

## Task {task_num}: {title}
**Status:** [ ]
**Files:** {files_str}
"""
    if section:
        block += f"**Section:** `{section}`\n"

    block += f"\n{description}\n"

    if test:
        block += f"\n**Test:** {test}\n"

    with open(TASKS_FILE, "a", encoding="utf-8") as f:
        f.write(block)
    log(f"  Added Task {task_num}: {title}")
    return task_num + 1


def log_result(findings):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## [{timestamp}] Self-Improvement Run\n")
        for finding in findings:
            f.write(f"- {finding}\n")


# --- Checks ---

def check_endpoints():
    """Test all API endpoints, return list of issues."""
    issues = []

    # Health
    code, body = curl("/api/health")
    if code != 200:
        issues.append(("critical", "Health endpoint down", code, body))

    # Quotes
    code, body = curl("/api/quotes")
    if code != 200:
        issues.append(("warn", f"Quotes endpoint returned {code}", code, body))

    # History
    code, body = curl("/api/history/SPY?period=1mo&interval=1d")
    if code != 200:
        issues.append(("warn", f"History endpoint returned {code}", code, body))

    # Watchlist
    code, body = curl("/api/watchlist")
    if code != 200:
        issues.append(("warn", f"Watchlist endpoint returned {code}", code, body))

    # Screener — skip in auto-improvement to avoid unnecessary GPU model swap.
    # The coder agent uses qwen2.5-coder; loading llama-agent for screener test
    # wastes ~20s of GPU time. Only test screener manually or in tester agent.

    # Portfolio deploy
    code, body = curl("/api/portfolio/deploy", "POST",
                       {"tickers": ["AAPL", "MSFT"], "amount": 5000, "user_id": "improve-test"})
    if code == 200:
        try:
            data = json.loads(body)
            if "positions" not in data:
                issues.append(("bug", "Portfolio deploy missing positions field", code, body[:200]))
        except json.JSONDecodeError:
            issues.append(("bug", "Portfolio deploy returned invalid JSON", code, body[:200]))
    else:
        issues.append(("warn", f"Portfolio deploy returned {code}", code, body[:200]))

    # Backtest
    code, body = curl("/api/generate-asset/backtest", "POST",
                       {"tickers": ["AAPL", "MSFT"], "period": "6mo"}, timeout=60)
    if code == 200:
        try:
            data = json.loads(body)
            if "score" not in data:
                issues.append(("bug", "Backtest missing score field", code, body[:200]))
        except json.JSONDecodeError:
            issues.append(("bug", "Backtest returned invalid JSON", code, body[:200]))
    elif code == 400 and "price data" in body.lower():
        # Transient yfinance rate limit — not a code bug, skip
        pass
    elif code != 0:
        issues.append(("warn", f"Backtest returned {code}", code, body[:200]))

    return issues


def check_js_api_mismatches():
    """Check if frontend JS matches backend API response shapes."""
    issues = []
    js = read_file("static/js/app.js")
    if not js:
        return issues

    # Screener: API returns {tickers:[...]}, JS might use result.matches
    if "result.matches" in js and "result.tickers" not in js:
        issues.append(("bug", "JS screener uses result.matches but API returns result.tickers",
                        "static/js/app.js"))

    # Portfolio: API returns positions with allocated_amount, not value
    if "pos.value" in js and "pos.allocated_amount" not in js:
        issues.append(("bug", "JS portfolio uses pos.value but API returns pos.allocated_amount",
                        "static/js/app.js"))

    return issues


def check_missing_features():
    """Check for features that should exist but don't."""
    issues = []
    js = read_file("static/js/app.js")
    html = read_file("static/index.html")
    css = read_file("static/css/terminal.css")

    if not js or not html:
        return issues

    # Table styling for new panes
    if css:
        if "portfolio-table" not in css and "portfolio-table" in js:
            issues.append(("improve", "portfolio-table class used in JS but not styled in CSS",
                            "static/css/terminal.css"))

        if "screener-match" not in css and "screener-match" in js:
            issues.append(("improve", "screener-match class used in JS but not styled in CSS",
                            "static/css/terminal.css"))

    # Indicators not exposed in UI
    indicators_py = read_file("indicators.py")
    if indicators_py:
        if "def rsi(" in indicators_py and "rsi" not in js.lower():
            issues.append(("improve", "RSI indicator exists in Python but not shown in chart UI",
                            "static/js/app.js"))

        if "def macd(" in indicators_py and "macd" not in js.lower():
            issues.append(("improve", "MACD indicator exists in Python but not shown in chart UI",
                            "static/js/app.js"))

    # News feed — check if still using mock
    server = read_file("server.py")
    if server and "FINNHUB_KEY" in server and "feedparser" not in server:
        issues.append(("improve", "News still uses Finnhub (credits exhausted) — switch to RSS feeds",
                        "server.py"))

    return issues


def check_failed_tasks():
    """Analyze [!] tasks and suggest simpler alternatives or splits."""
    issues = []
    content = read_tasks()
    log_path = os.path.join(os.path.dirname(TASKS_FILE), "task-log.md")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
    except FileNotFoundError:
        return issues

    for m in re.finditer(r"## Task (\d+): (.+?)\n\*\*Status:\*\* \[!\]", content):
        task_num = int(m.group(1))
        task_title = m.group(2)

        # Count failure types
        failures = re.findall(rf"Task {task_num}: FAILED (.+)", log_content)
        timeout_count = sum(1 for f in failures if "TIMEOUT" in f)
        syntax_count = sum(1 for f in failures if "syntax" in f.lower())

        if timeout_count >= 2:
            issues.append(("improve",
                          f"Task {task_num} ({task_title}) timed out {timeout_count}x — "
                          f"section may be too large for 7B model, consider splitting",
                          "swarm_engine.py"))
        elif syntax_count >= 2:
            issues.append(("improve",
                          f"Task {task_num} ({task_title}) had {syntax_count} syntax errors — "
                          f"7B model struggles with this edit pattern",
                          "swarm_engine.py"))

    return issues


def check_audit_log():
    """Read the audit log and convert CRITICAL/REVIEW findings into issues."""
    issues = []
    audit_log = os.path.join(os.path.dirname(TASKS_FILE), "audit-log.md")
    try:
        with open(audit_log, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return issues

    # Find unresolved CRITICAL issues
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("- CRITICAL:"):
            desc = line[len("- CRITICAL:"):].strip()
            # Guess the file from the description
            if "app.js" in desc:
                issues.append(("bug", desc, "static/js/app.js"))
            elif "server.py" in desc:
                issues.append(("bug", desc, "server.py"))
            else:
                issues.append(("bug", desc, "static/js/app.js"))

    return issues


def check_code_quality():
    """Basic code quality checks."""
    issues = []
    js = read_file("static/js/app.js")
    if not js:
        return issues

    # Duplicate IDs in portfolio tables
    if js.count("id: 'portfolio-tbody'") > 1 or js.count("id: \"portfolio-tbody\"") > 1:
        issues.append(("bug", "Duplicate portfolio-tbody IDs will cause DOM conflicts",
                        "static/js/app.js"))

    # innerHTML usage (XSS risk)
    innerHTML_count = js.count(".innerHTML")
    if innerHTML_count > 2:
        issues.append(("warn", f"innerHTML used {innerHTML_count}x — potential XSS if user data displayed",
                        "static/js/app.js"))

    return issues


# --- Task Generation ---

# Map of issue descriptions to task definitions
# Each entry: (title, file_hint, description, test, section)
# section is the function name from FILE_MAP.md (REQUIRED for app.js/server.py)
TASK_TEMPLATES = {
    "result.matches": (
        "Fix screener API response field name",
        "static/js/app.js",
        "The screener API returns `{tickers: [...]}` but the JS reads `result.matches`. "
        "Change `result.matches` to `result.tickers` in the screenStocks function.",
        "`curl.exe -s http://127.0.0.1:8500/static/js/app.js` should contain `result.tickers`",
        "screenStocks"
    ),
    "position:relative": (
        "Add position:relative to chart pane",
        "static/css/terminal.css",
        "Add `position: relative;` to `#pane-chart` in CSS so the backtest results "
        "overlay positions correctly within the chart pane.",
        "`curl.exe -s http://127.0.0.1:8500/static/css/terminal.css` should contain `#pane-chart` with `position: relative`",
        ""
    ),
    "portfolio-tbody": (
        "Fix duplicate portfolio-tbody IDs",
        "static/js/app.js",
        "The deployPortfolio and loadPortfolio functions both create elements with "
        "id='portfolio-tbody'. Change loadPortfolio to use 'portfolio-load-tbody' "
        "to avoid DOM ID conflicts.",
        "",
        "loadPortfolio"
    ),
    "loading": (
        "Add loading state to screener",
        "static/js/app.js",
        "In the screenStocks function, show 'Screening stocks...' text in the "
        "results container BEFORE the fetch call. Clear on success or error.",
        "",
        "screenStocks"
    ),
    "console": (
        "Show screener errors to user",
        "static/js/app.js",
        "In the screenStocks function catch block, after console.error, show the error "
        "in screener-results with red text: color:var(--red). Use textContent, not innerHTML.",
        "",
        "screenStocks"
    ),
    "portfolio-table": (
        "Style new pane elements in CSS",
        "static/css/terminal.css",
        "Add CSS styles for `.portfolio-table` and `.screener-match` classes. "
        "Match the existing market-table styling: `width:100%; border-collapse:collapse;` "
        "with `th/td { padding: 4px 8px; text-align: right; border-bottom: 1px solid var(--border); }`. "
        "First th/td should be `text-align: left`. "
        "Screener matches: `list-style:none; padding:4px 8px; border-bottom:1px solid var(--border); color:var(--accent);`",
        "",
        ""
    ),
    "harvest": (
        "Add harvest click handler to portfolio",
        "static/js/app.js",
        "Add a click event listener for #portfolio-harvest-btn. When clicked, "
        "POST to /api/portfolio/harvest/{user_id}. Display JSON result in "
        "portfolio-display using textContent. Guard with if(getElementById).",
        "",
        "harvest_handler"
    ),
    "command bar": (
        "Add /screen command to command bar",
        "static/js/app.js",
        "In the handleCommand function, add: if action === 'screen' and parts.length > 1, "
        "set screener-input value to the rest of the command and call screenStocks().",
        "",
        "handleCommand"
    ),
    "rsi": (
        "Add RSI indicator to chart pane",
        "static/js/app.js",
        "In the renderChart function, after creating the candlestick series, calculate "
        "RSI(14) from the close prices. Add a new line series for RSI below the chart.",
        "",
        "renderChart"
    ),
    "innerHTML": (
        "Replace innerHTML in renderNews",
        "static/js/app.js",
        "In the renderNews function, replace any remaining innerHTML assignments with safe "
        "DOM construction using createElement/textContent/appendChild.",
        "",
        "renderNews"
    ),
}


def find_template_match(desc):
    """Find a matching task template for an issue description."""
    desc_lower = desc.lower()
    for key, template in TASK_TEMPLATES.items():
        if key in desc_lower:
            return template
    return None


# Map keywords in issue descriptions to section names for auto-detection
SECTION_HINTS = {
    "screener": "screenStocks",
    "screen stocks": "screenStocks",
    "portfolio": "deployPortfolio",
    "deploy": "deployPortfolio",
    "backtest": "runBacktest",
    "chart": "renderChart",
    "news": "renderNews",
    "watchlist": "renderWatchlist",
    "market table": "renderMarketTable",
    "websocket": "connectWebSocket",
    "command": "handleCommand",
    "keyboard": "setupKeyboard",
    "harvest": "harvest_handler",
    "quotes": "_fetch_quotes_sync",
    "rss": "_fetch_rss_news",
    "history": "_fetch_history_sync",
}


def guess_section(desc, file_hint):
    """Auto-detect the section name from issue description."""
    desc_lower = desc.lower()
    for keyword, section in SECTION_HINTS.items():
        if keyword in desc_lower:
            return section
    return ""


def generate_generic_task(next_num, severity, desc, file_hint):
    """Generate a task from an issue that doesn't match any template."""
    if not isinstance(file_hint, str):
        file_hint = "static/js/app.js"
    abs_file = os.path.join(PROJECT_DIR, file_hint) if not os.path.isabs(file_hint) else file_hint

    # Build a descriptive title from the issue
    title = desc[:60]
    if len(desc) > 60:
        title = title.rsplit(" ", 1)[0] + "..."

    # Prefix with Fix/Add/Improve based on severity
    if severity == "bug":
        title = f"Fix: {title}"
    elif severity == "improve":
        title = f"Add: {title}"
    elif severity == "warn":
        title = f"Fix: {title}"

    # Auto-detect section for targeted editing
    section = guess_section(desc, file_hint)

    return append_task(next_num, title, [abs_file], desc, section=section)


def generate_tasks(all_issues):
    """Convert issues into concrete tasks in tasks.md."""
    next_num = get_next_task_number()
    added = 0

    # Priority: bugs first, then warnings, then improvements
    bugs = [i for i in all_issues if i[0] == "bug"]
    warnings = [i for i in all_issues if i[0] == "warn"]
    improvements = [i for i in all_issues if i[0] == "improve"]
    ordered = bugs + warnings + improvements

    for issue in ordered:
        if added >= MAX_NEW_TASKS:
            break

        severity, desc, *extra = issue
        file_hint = extra[0] if extra else "static/js/app.js"
        if not isinstance(file_hint, str):
            file_hint = "static/js/app.js"
        abs_file = os.path.join(PROJECT_DIR, file_hint) if not os.path.isabs(file_hint) else file_hint

        # Try to match a template
        template = find_template_match(desc)
        if template:
            title, tmpl_file, tmpl_desc, tmpl_test, tmpl_section = template
            tmpl_abs = os.path.join(PROJECT_DIR, tmpl_file)
            old_num = next_num
            next_num = append_task(next_num, title, [tmpl_abs], tmpl_desc, tmpl_test, tmpl_section)
            if next_num > old_num:
                added += 1
        else:
            # Generic task generation
            old_num = next_num
            next_num = generate_generic_task(next_num, severity, desc, file_hint)
            if next_num > old_num:
                added += 1

    return added


# --- Main ---

def main():
    log("Self-Improvement Engine starting...")

    # Don't generate new tasks if there are pending ones
    if has_pending_tasks():
        log("Pending tasks exist — skipping improvement run. Complete tasks first.")
        return

    log("No pending tasks. Running checks...")
    all_issues = []
    findings = []

    # 1. Endpoint tests
    log("Testing API endpoints...")
    endpoint_issues = check_endpoints()
    all_issues.extend(endpoint_issues)
    for i in endpoint_issues:
        findings.append(f"[{i[0]}] endpoint: {i[1]}")
    log(f"  Found {len(endpoint_issues)} endpoint issues")

    # 2. JS/API mismatch
    log("Checking JS/API mismatches...")
    mismatch_issues = check_js_api_mismatches()
    all_issues.extend(mismatch_issues)
    for i in mismatch_issues:
        findings.append(f"[{i[0]}] mismatch: {i[1]}")
    log(f"  Found {len(mismatch_issues)} mismatches")

    # 3. Missing features
    log("Checking for missing features...")
    feature_issues = check_missing_features()
    all_issues.extend(feature_issues)
    for i in feature_issues:
        findings.append(f"[{i[0]}] feature: {i[1]}")
    log(f"  Found {len(feature_issues)} missing features")

    # 4. Code quality
    log("Checking code quality...")
    quality_issues = check_code_quality()
    all_issues.extend(quality_issues)
    for i in quality_issues:
        findings.append(f"[{i[0]}] quality: {i[1]}")
    log(f"  Found {len(quality_issues)} quality issues")

    # 5. Analyze failed tasks — suggest simpler alternatives
    log("Analyzing permanently failed tasks...")
    failed_issues = check_failed_tasks()
    all_issues.extend(failed_issues)
    for i in failed_issues:
        findings.append(f"[{i[0]}] failed-task: {i[1]}")
    log(f"  Found {len(failed_issues)} failed task patterns")

    # 6. Audit log — convert unresolved audit findings into fix tasks
    log("Checking audit log for unresolved issues...")
    audit_issues = check_audit_log()
    all_issues.extend(audit_issues)
    for i in audit_issues:
        findings.append(f"[{i[0]}] audit: {i[1]}")
    log(f"  Found {len(audit_issues)} audit issues")

    # Summary
    total = len(all_issues)
    log(f"\nTotal issues found: {total}")
    for i in all_issues:
        log(f"  [{i[0]}] {i[1]}")

    # Generate tasks
    if total > 0:
        log("\nGenerating tasks...")
        added = generate_tasks(all_issues)
        log(f"Added {added} new tasks to queue")
        findings.append(f"Generated {added} new tasks")
    else:
        log("No issues found — project is clean!")
        findings.append("All checks passed — no issues found")

    log_result(findings)
    log("Done.")


if __name__ == "__main__":
    main()
