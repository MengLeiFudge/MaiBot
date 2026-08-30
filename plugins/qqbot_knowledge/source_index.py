from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import threading
import time

from .domains import build_search_terms, normalize_text


SUPPORTED_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".cs",
        ".ini",
        ".js",
        ".json",
        ".lua",
        ".md",
        ".py",
        ".toml",
        ".ts",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
SKIP_DIR_NAMES = frozenset(
    {
        ".agent-reference",
        ".codex",
        ".git",
        ".github",
        ".idea",
        ".vs",
        ".vscode",
        "__pycache__",
        "bin",
        "build",
        "cache",
        "coverage",
        "dist",
        "log",
        "logs",
        "node_modules",
        "obj",
        "out",
        "packages",
        "target",
        "temp",
        "tmp",
    }
)
SENSITIVE_NAME_MARKERS = (
    ".env",
    "authorization",
    "cookie",
    "credential",
    "login",
    "password",
    "secret",
    "session",
    "token",
)
NOISY_FILE_NAMES = frozenset({"package-lock.json", "pnpm-lock.yaml", "tsconfig.tsbuildinfo"})
_CONTEXT_LINES = 2
_MAX_LINE_CHARS = 500
_MAX_ROOTS_PER_DOMAIN = 64
_MIN_SCAN_FILES_PER_DOMAIN = 32


@dataclass(frozen=True, slots=True)
class RootSpec:
    domain: str
    configured_path: str


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_results: int = 4
    max_chars: int = 2600
    max_files_per_domain: int = 80
    max_file_bytes: int = 220_000
    refresh_seconds: int = 600
    search_timeout_seconds: float = 3.0
    cache_ttl_seconds: int = 600
    cache_max_entries: int = 128


@dataclass(frozen=True, slots=True)
class SearchResult:
    domain: str
    display_path: str
    line_start: int
    line_end: int
    score: float
    excerpt: str
    supplemental: bool


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    evidence: str
    domains: tuple[str, ...]
    result_count: int
    char_count: int
    timed_out: bool


@dataclass(frozen=True, slots=True)
class _ValidatedRoot:
    domain: str
    configured_path: str
    path: Path
    is_file: bool


@dataclass(frozen=True, slots=True)
class _SourcePath:
    root: _ValidatedRoot
    path: Path
    display_path: str
    priority: int


class SourceIndex:
    """Bounded, in-memory source path index and lexical retriever."""

    def __init__(self, roots: Iterable[RootSpec], limits: SearchLimits) -> None:
        self._root_specs = tuple(roots)
        self.limits = limits
        self._paths_by_domain: dict[str, tuple[_SourcePath, ...]] = {}
        self._indexed_at: dict[str, float] = {}
        self._result_cache: dict[str, tuple[float, SearchOutcome]] = {}
        self._lock = threading.RLock()

    @property
    def available_domains(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(spec.domain for spec in self._root_specs if spec.domain))

    def clear(self) -> None:
        with self._lock:
            self._paths_by_domain.clear()
            self._indexed_at.clear()
            self._result_cache.clear()

    def search(self, query: str, domains: tuple[str, ...]) -> SearchOutcome:
        terms = build_search_terms(query)
        if not terms or not domains:
            return SearchOutcome("", domains, 0, 0, False)
        cache_key = _cache_key(query, domains)
        now = time.monotonic()
        with self._lock:
            cached = self._result_cache.get(cache_key)
            if cached is not None and now - cached[0] <= self.limits.cache_ttl_seconds:
                return cached[1]

            deadline = now + self.limits.search_timeout_seconds
            results, timed_out = self._search_uncached(terms, domains, deadline)
            evidence, included = _format_evidence(
                results,
                max_results=self.limits.max_results,
                max_chars=self.limits.max_chars,
            )
            outcome = SearchOutcome(evidence, domains, included, len(evidence), timed_out)
            self._prune_result_cache(now)
            self._result_cache[cache_key] = (now, outcome)
            return outcome

    def _search_uncached(
        self,
        terms: tuple[str, ...],
        domains: tuple[str, ...],
        deadline: float,
    ) -> tuple[list[SearchResult], bool]:
        rows: list[SearchResult] = []
        for domain in domains:
            if time.monotonic() >= deadline:
                return _prefer_primary_results(rows), True
            paths, timed_out = self._paths_for_domain(domain, deadline)
            if timed_out:
                return _prefer_primary_results(rows), True
            domain_rows: list[SearchResult] = []
            scanned = 0
            sorted_paths = sorted(paths, key=lambda item: _query_path_priority(item, terms))
            for source_path in sorted_paths[: self.limits.max_files_per_domain]:
                if time.monotonic() >= deadline:
                    rows.extend(domain_rows)
                    return _prefer_primary_results(rows), True
                text = _read_source_text(source_path, self.limits.max_file_bytes)
                scanned += 1
                if not text:
                    continue
                score = _score_source(source_path, text, terms)
                if score <= 0:
                    continue
                line_start, line_end, excerpt = _best_excerpt(text, terms)
                if not excerpt:
                    continue
                domain_rows.append(
                    SearchResult(
                        domain=domain,
                        display_path=source_path.display_path,
                        line_start=line_start,
                        line_end=line_end,
                        score=score,
                        excerpt=excerpt,
                        supplemental=_is_release_note(source_path.path),
                    )
                )
                if (
                    scanned >= min(_MIN_SCAN_FILES_PER_DOMAIN, self.limits.max_files_per_domain)
                    and len(domain_rows) >= self.limits.max_results
                ):
                    break
            rows.extend(domain_rows)
        return _prefer_primary_results(rows), False

    def _paths_for_domain(self, domain: str, deadline: float) -> tuple[tuple[_SourcePath, ...], bool]:
        now = time.monotonic()
        cached = self._paths_by_domain.get(domain)
        if cached is not None and now - self._indexed_at.get(domain, 0.0) <= self.limits.refresh_seconds:
            return cached, False

        roots = tuple(
            root
            for spec in self._root_specs
            if spec.domain == domain
            if (root := _validate_root(spec)) is not None
        )[:_MAX_ROOTS_PER_DOMAIN]
        candidates: list[_SourcePath] = []
        seen: set[Path] = set()
        timed_out = False
        for root in roots:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            root_candidate_count = 0
            for source_path in _iter_root_paths(root, self.limits.max_file_bytes, deadline):
                if source_path.path in seen:
                    continue
                seen.add(source_path.path)
                candidates.append(source_path)
                root_candidate_count += 1
                if root_candidate_count >= self.limits.max_files_per_domain:
                    break
            if time.monotonic() >= deadline:
                timed_out = True
                break

        candidates.sort(key=lambda item: (item.priority, item.display_path.casefold()))
        indexed = tuple(candidates)
        if not timed_out:
            self._paths_by_domain[domain] = indexed
            self._indexed_at[domain] = now
        return indexed, timed_out

    def _prune_result_cache(self, now: float) -> None:
        expired = [
            key
            for key, (created_at, _outcome) in self._result_cache.items()
            if now - created_at > self.limits.cache_ttl_seconds
        ]
        for key in expired:
            self._result_cache.pop(key, None)
        overflow = len(self._result_cache) - self.limits.cache_max_entries + 1
        if overflow > 0:
            oldest = sorted(self._result_cache, key=lambda key: self._result_cache[key][0])[:overflow]
            for key in oldest:
                self._result_cache.pop(key, None)


def configured_path(raw_path: str) -> Path:
    cleaned = str(raw_path or "").strip()
    drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", cleaned)
    if drive_match:
        return Path(f"/mnt/{drive_match.group(1).lower()}/{drive_match.group(2).replace('\\', '/')}")
    return Path(cleaned).expanduser()


def _validate_root(spec: RootSpec) -> _ValidatedRoot | None:
    path = configured_path(spec.configured_path)
    if not path.is_absolute() or path.is_symlink() or _has_forbidden_component(path):
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if resolved != path.absolute() or _has_forbidden_component(resolved):
        return None
    if not resolved.is_dir() and not resolved.is_file():
        return None
    return _ValidatedRoot(spec.domain, spec.configured_path, resolved, resolved.is_file())


def _iter_root_paths(root: _ValidatedRoot, max_file_bytes: int, deadline: float) -> Iterable[_SourcePath]:
    if root.is_file:
        source = _validated_source_path(root, root.path, max_file_bytes)
        if source is not None:
            yield source
        return

    for current, dir_names, file_names in os.walk(root.path, topdown=True, followlinks=False):
        if time.monotonic() >= deadline:
            return
        current_path = Path(current)
        safe_dirs: list[str] = []
        for name in sorted(dir_names, key=str.casefold):
            child = current_path / name
            if _is_ignored_dir(name) or child.is_symlink() or _has_forbidden_component(child):
                continue
            if not _is_within_root(child, root.path):
                continue
            safe_dirs.append(name)
        dir_names[:] = safe_dirs
        for name in sorted(file_names, key=lambda item: (_source_priority(Path(item)), item.casefold())):
            if time.monotonic() >= deadline:
                return
            source = _validated_source_path(root, current_path / name, max_file_bytes)
            if source is not None:
                yield source


def _validated_source_path(root: _ValidatedRoot, path: Path, max_file_bytes: int) -> _SourcePath | None:
    if path.is_symlink() or path.name.casefold() in NOISY_FILE_NAMES or _has_forbidden_component(path):
        return None
    if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        return None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or not _is_within_root(resolved, root.path):
            return None
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > max_file_bytes:
        return None
    display_path = resolved.name if root.is_file else resolved.relative_to(root.path).as_posix()
    return _SourcePath(root, resolved, display_path, _source_priority(resolved))


def _read_source_text(source_path: _SourcePath, max_file_bytes: int) -> str:
    if not _is_within_root(source_path.path, source_path.root.path) or source_path.path.is_symlink():
        return ""
    try:
        with source_path.path.open("rb") as source_file:
            raw = source_file.read(max_file_bytes + 1)
    except OSError:
        return ""
    if len(raw) > max_file_bytes or b"\x00" in raw[:4096]:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _has_forbidden_component(path: Path) -> bool:
    for part in path.parts:
        normalized = part.casefold()
        if normalized in SKIP_DIR_NAMES or any(marker in normalized for marker in SENSITIVE_NAME_MARKERS):
            return True
    return False


def _is_ignored_dir(name: str) -> bool:
    normalized = name.casefold()
    return normalized in SKIP_DIR_NAMES or normalized.endswith(".egg-info")


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError:
        return False
    if resolved_root.is_file():
        return resolved_path == resolved_root
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _source_priority(path: Path) -> int:
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}
    if _is_release_note(path):
        return 80
    if name.startswith("readme") or "design" in name or "architecture" in name:
        return 10
    if "decompiledsource" in parts or "decompiled" in parts:
        return 15
    if parts & {"src", "source", "scripts"} or path.suffix.casefold() in {".cs", ".lua", ".py", ".ts", ".js"}:
        return 20
    if parts & {"docs", "doc"}:
        return 30
    return 40


def _query_path_priority(source_path: _SourcePath, terms: tuple[str, ...]) -> tuple[float, int, str]:
    path_text = normalize_text(source_path.display_path)
    path_score = sum(_term_weight(term) for term in terms if term in path_text)
    return (-path_score, source_path.priority, path_text)


def _score_source(source_path: _SourcePath, text: str, terms: tuple[str, ...]) -> float:
    lower_text = normalize_text(text)
    lower_path = normalize_text(source_path.display_path)
    score = 0.0
    for term in terms:
        weight = _term_weight(term)
        if term in lower_path:
            score += weight * 6
        score += weight * min(lower_text.count(term), 8)
    if score:
        score += max(0, 30 - source_path.priority) / 10
    return score


def _term_weight(term: str) -> float:
    if len(term) >= 12:
        return 8.0
    if len(term) >= 6:
        return 5.0
    if len(term) >= 4:
        return 3.0
    return 1.5


def _best_excerpt(text: str, terms: tuple[str, ...]) -> tuple[int, int, str]:
    lines = text.splitlines()
    best_index = -1
    best_score = 0.0
    for index, line in enumerate(lines):
        normalized = normalize_text(line)
        score = sum(_term_weight(term) for term in terms if term in normalized)
        if _is_boilerplate_line(normalized):
            score *= 0.1
        if score > best_score:
            best_index = index
            best_score = score
    if best_index < 0:
        return 0, 0, ""
    start = max(0, best_index - _CONTEXT_LINES)
    end = min(len(lines), best_index + _CONTEXT_LINES + 1)
    excerpt_lines = [line.strip()[:_MAX_LINE_CHARS] for line in lines[start:end] if line.strip()]
    return start + 1, end, "\n".join(excerpt_lines)


def _is_boilerplate_line(normalized_line: str) -> bool:
    return normalized_line.startswith(("using ", "namespace ", "import ", "from ", "#include "))


def _prefer_primary_results(rows: list[SearchResult]) -> list[SearchResult]:
    rows.sort(key=lambda row: (row.supplemental, -row.score, row.domain, row.display_path, row.line_start))
    return rows


def _format_evidence(results: list[SearchResult], *, max_results: int, max_chars: int) -> tuple[str, int]:
    chunks: list[str] = []
    used = 0
    for result in results:
        if used >= max_results:
            break
        line_ref = str(result.line_start)
        if result.line_end != result.line_start:
            line_ref = f"{line_ref}-{result.line_end}"
        chunk = f"[{result.domain}] {result.display_path}:{line_ref}\n{result.excerpt.strip()}"
        separator = "\n\n" if chunks else ""
        remaining = max_chars - len(separator) - sum(len(item) for item in chunks)
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining].rstrip()
        if not chunk:
            break
        chunks.append(f"{separator}{chunk}")
        used += 1
    return "".join(chunks), used


def _is_release_note(path: Path) -> bool:
    name = path.name.casefold()
    return "changelog" in name or "release" in name


def _cache_key(query: str, domains: tuple[str, ...]) -> str:
    digest = sha256(query.encode("utf-8", errors="replace")).hexdigest()
    return f"{','.join(domains)}:{digest}"
