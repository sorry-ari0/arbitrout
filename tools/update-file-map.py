"""Refresh FILE_MAP.md line counts from actual files."""
import os
import re
import sys

# Force line-buffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

PROJECT_DIR = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\src"
FILE_MAP = r"C:\Users\afoma\.openclaw\workspace\projects\bloomberg-terminal\FILE_MAP.md"

def refresh():
    with open(FILE_MAP, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    updated = []
    for line in lines:
        # Match file headers: ## src/server.py (496 lines)
        m = re.match(r"^(## src/\S+) \((\d+) lines\)", line)
        if m:
            rel_path = m.group(1).replace("## ", "")
            abs_path = os.path.join(os.path.dirname(PROJECT_DIR), rel_path)
            if os.path.isfile(abs_path):
                actual = sum(1 for _ in open(abs_path, encoding="utf-8"))
                updated.append(f"{m.group(1)} ({actual} lines)")
                continue
        updated.append(line)

    with open(FILE_MAP, "w", encoding="utf-8") as f:
        f.write("\n".join(updated))
    print(f"[file-map] Refreshed line counts in FILE_MAP.md")

if __name__ == "__main__":
    refresh()
