from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "remote_runs",
}
EXCLUDE_FILES = {"quiz_log.json"}
INCLUDE_EXTS = {".py", ".yaml", ".yml", ".txt", ".md"}


def should_exclude_path(p: Path) -> bool:
    parts = {part.lower() for part in p.parts}
    if any(d.lower() in parts for d in EXCLUDE_DIRS):
        return True
    if p.name.lower() in {f.lower() for f in EXCLUDE_FILES}:
        return True
    return False


def main() -> None:
    total = 0
    by_ext: dict[str, int] = {}
    file_count = 0

    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        if should_exclude_path(p):
            continue
        if p.suffix.lower() not in INCLUDE_EXTS:
            continue

        try:
            data = p.read_bytes()
        except Exception:
            continue

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")

        n = len(text.splitlines())
        total += n
        by_ext[p.suffix.lower()] = by_ext.get(p.suffix.lower(), 0) + n
        file_count += 1

    print(f"ROOT={ROOT}")
    print(f"FILES={file_count}")
    print(f"TOTAL_LINES={total}")
    for ext, n in sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{ext}: {n}")


if __name__ == "__main__":
    main()

