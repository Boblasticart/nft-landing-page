"""Extracts file blocks from agent output and writes them safely into the local project directory."""
import json
import re
from pathlib import Path

FILE_FORMAT = """Return every file you create or change in full, each wrapped exactly like this:
=== FILE: relative/path.ext ===
<entire file content>
=== END FILE ===
Do not use markdown code fences inside the blocks. Text outside blocks is kept as notes."""

_BLOCK = re.compile(r"^=== FILE: (?P<path>[^\n]+?) ===\n(?P<body>.*?)^=== END FILE ===", re.M | re.S)

IGNORED_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".vite", "__pycache__"}


def project_files(root):
    """Files under root, skipping dependency and build output folders."""
    return sorted(p for p in Path(root).rglob("*")
                  if p.is_file() and not IGNORED_DIRS.intersection(p.relative_to(root).parts))


TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".md",
                 ".txt", ".svg", ".py", ".toml", ".yml", ".yaml", ".xml", ".env.example"}


def parse_files(text):
    """Return ({path: content}, leftover_notes)."""
    files = {}
    for m in _BLOCK.finditer(text):
        files[m.group("path").strip().strip("`")] = m.group("body")
    notes = _BLOCK.sub("", text).strip()
    return files, notes


_MARKER = re.compile(r"^(=== FILE: [^\n]+? ===|=== END FILE ===)$", re.M)


def unterminated_header(text):
    """Return the header line of a file block that was opened but never closed, else None."""
    markers = _MARKER.findall(text)
    if markers and markers[-1].startswith("=== FILE:"):
        return markers[-1]
    return None


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
    for p in project_files(root):
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        body = p.read_text(errors="replace")
        block = f"=== FILE: {p.relative_to(root)} ===\n{body}\n=== END FILE ===\n"
        if used + len(block) > max_chars:
            chunks.append(f"(… {p.relative_to(root)} omitted: context budget reached)\n")
            continue
        chunks.append(block)
        used += len(block)
    return "".join(chunks)


_HTML_REF = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
_CSS_REF = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.I)
_JS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(?\s*)["']([^"']+)["']""")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)
_JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs")
_NODE_BUILTINS = {"fs", "path", "url", "os", "http", "https", "crypto", "util", "events", "stream", "child_process",
                  "process", "buffer", "assert", "module", "zlib", "readline", "net", "tty", "worker_threads"}
_RESOLVE_TRIES = ("", ".js", ".jsx", ".ts", ".tsx", ".mjs", "/index.js", "/index.jsx", "/index.ts", "/index.tsx")


def check_project(root):
    """Cheap local integrity check: entry point, broken local references, undeclared npm deps."""
    root = Path(root)
    files = project_files(root) if root.is_dir() else []
    if not files:
        return ["the project has no files"]
    problems = []
    if not (root / "index.html").is_file():
        problems.append("there is no index.html at the project root, so the site cannot be opened")
    npm_packages = set()

    def missing(src, ref, tries=("",)):
        ref = ref.split("?")[0].split("#")[0]
        if not ref or _EXTERNAL.match(ref):
            return False
        base = root / ref.lstrip("/") if ref.startswith("/") else src.parent / ref
        return not any(Path(str(base) + t).exists() for t in tries)

    for p in files:
        rel = p.relative_to(root)
        suffix = p.suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            continue
        text = p.read_text(errors="replace")
        if suffix in (".html", ".htm"):
            refs = [(r, ("",)) for r in _HTML_REF.findall(text)]
        elif suffix == ".css":
            refs = [(r, ("",)) for r in _CSS_REF.findall(text)]
        elif suffix in _JS_SUFFIXES:
            refs = []
            for r in _JS_IMPORT.findall(text):
                if r.startswith("."):
                    refs.append((r, _RESOLVE_TRIES))
                elif not _EXTERNAL.match(r) and not r.startswith("/"):
                    parts = r.split("/")
                    name = "/".join(parts[:2]) if r.startswith("@") else parts[0]
                    if name not in _NODE_BUILTINS:
                        npm_packages.add(name)
        else:
            continue
        for ref, tries in refs:
            if missing(p, ref, tries):
                problems.append(f"{rel} references '{ref}', which does not exist")
    if npm_packages:
        pkg = root / "package.json"
        if not pkg.is_file():
            problems.append("the code imports npm packages but there is no package.json")
        else:
            try:
                data = json.loads(pkg.read_text())
                declared = set(data.get("dependencies", {})) | set(data.get("devDependencies", {}))
                for name in sorted(npm_packages - declared):
                    problems.append(f"the code imports '{name}' but package.json does not declare it")
            except (ValueError, AttributeError):
                problems.append("package.json is not valid JSON")
    return problems[:30]
