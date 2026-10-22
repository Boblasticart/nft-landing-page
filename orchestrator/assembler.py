"""Extracts file blocks from agent output and writes them safely into the local project directory."""
import re
from pathlib import Path

FILE_FORMAT = """Return every file you create or change in full, each wrapped exactly like this:
=== FILE: relative/path.ext ===
<entire file content>
=== END FILE ===
Do not use markdown code fences inside the blocks. Text outside blocks is kept as notes."""

_BLOCK = re.compile(r"^=== FILE: (?P<path>[^\n]+?) ===\n(?P<body>.*?)^=== END FILE ===", re.M | re.S)

TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".md",
                 ".txt", ".svg", ".py", ".toml", ".yml", ".yaml", ".xml", ".env.example"}


def parse_files(text):
    """Return ({path: content}, leftover_notes)."""
    files = {}
    for m in _BLOCK.finditer(text):
        files[m.group("path").strip().strip("`")] = m.group("body")
    notes = _BLOCK.sub("", text).strip()
    return files, notes


def safe_path(root, rel):
    root = Path(root).resolve()
    target = (root / rel).resolve()
    if rel.startswith(("/", "\\")) or not target.is_relative_to(root):
        raise ValueError(f"refusing to write outside project: {rel}")
    return target


def write_files(root, files):
    """Write files under root; returns (written, rejected) so unsafe paths never abort a run."""
    written, rejected = [], []
    for rel, content in files.items():
        try:
            target = safe_path(root, rel)
        except ValueError:
            rejected.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(str(target.relative_to(Path(root).resolve())))
    return written, rejected


def read_project(root, max_chars=40000):
    """Concatenate the project's text files for use as context, within a character budget."""
    root = Path(root)
    if not root.is_dir():
        return ""
    chunks, used = [], 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        body = p.read_text(errors="replace")
        block = f"=== FILE: {p.relative_to(root)} ===\n{body}\n=== END FILE ===\n"
        if used + len(block) > max_chars:
            chunks.append(f"(… {p.relative_to(root)} omitted: context budget reached)\n")
            continue
        chunks.append(block)
        used += len(block)
    return "".join(chunks)
