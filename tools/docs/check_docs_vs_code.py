#!/usr/bin/env python3
"""문서 ↔ 코드 어긋남 검사 (CLAUDE.md §2.5 · §2.6 · §2.8).

명세 링크 검사(`tools/spec/check_spec_links.py`)는 **명세에 쓴 것**만 지킨다.
정작 다음 세션을 속이는 것은 명세 밖의 산문이다 — 지운 개념의 이름이 주석에
남아 있거나, 문서가 없는 함수를 태연히 가리키거나, 옛 기록이 현재형으로
읽히는 것.

네 가지를 본다. 등록부는 `docs/docs-vs-code.json` 하나뿐이다.

    1. 지운 이름이 코드에 다시 나타나지 않는가          (§2.5)
    2. 지운 말이 글에 '묘비' 없이 쓰이지 않는가          (§2.5)
    3. '지금' 인 문서가 없는 이름을 가리키지 않는가       (§2.6)
    4. 문서 머리의 시제 표시가 등록부와 같은가            (§2.8)

언어 중립이다 — 파서를 쓰지 않고 낱말 경계와 백틱만 본다.
의존성 없음(표준 라이브러리만). 실패가 하나라도 있으면 종료 코드 1.

⚠️ **주석 제거는 근사다.** `#` · `//` · `--` 를 줄 주석으로 보므로 C 의
`#include` 같은 줄이 같이 날아간다. 그쪽에서 지운 이름을 놓칠 수는 있어도
없는 것을 있다고 하지는 않는다 — 관문은 **거짓 실패가 없는 쪽**으로 기운다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "docs" / "docs-vs-code.json"

# 검사 대상에서 제외하는 디렉터리 이름 (check_spec_links.py 와 같은 규약)
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    "out", "bin", "obj", "target", "vendor", "Pods", ".next", ".gradle",
    ".idea", ".vs", ".evidence",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".mp4", ".mov", ".webm", ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll",
    ".dylib", ".exe", ".bin", ".wasm", ".lock",
}
MAX_FILE_BYTES = 2_000_000
DOC_SUFFIXES = {".md", ".markdown"}

# 이 검사기와 등록부 자신. 지운 이름 · 지운 말이 **적혀 있는 것이 정상**이라
# 훑는 대상에서 뺀다. 코드 더미(§2.6)에서도 뺀다 — 등록부에 이름이 적혀
# 있다는 이유로 "코드에 있다" 가 되면 검사가 스스로를 통과시킨다.
SELF = {
    str(Path(__file__).resolve().relative_to(ROOT)),
    str(CONFIG_FILE.relative_to(ROOT)),
}

# 백틱 안이 **파일 이름**이면 코드 이름이 아니다. 프로젝트에서 늘린다.
FILE_SUFFIXES = {
    "md", "markdown", "json", "yml", "yaml", "toml", "ini", "xml", "csv", "txt",
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "sql", "html", "css", "sh",
    "go", "rs", "java", "kt", "swift", "cs", "cpp", "cc", "c", "h", "hpp",
    "rb", "php", "lua", "gradle", "proto", "bat", "ps1",
}

BACKTICK_RE = re.compile(r"`([^`]+)`")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
TENSE_RE = re.compile(r"\*\*시제:\s*(지금|그때|앞으로)\.?\*\*")

BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/|<!--.*?-->|\"\"\".*?\"\"\"|'''.*?'''", re.DOTALL)
LINE_COMMENT_RE = re.compile(r"(^|\s)(//|#|--|;;).*$", re.MULTILINE)
# 이어 붙인 다음 줄 머리의 주석 표시 (`unwrap`)
COMMENT_LEAD_RE = re.compile(r"^(\*|//|--|#)\s*")

# 마크다운 취소선. 같은 **줄**에 있으면 그것만으로 묘비다 — 금지어 표는
# 한 줄이 곧 묘비 하나라 문단으로 묶어 봐야 옆줄까지 닿지 않는다.
TOMBSTONE_INLINE = ("~~",)
# 묘비를 찾을 범위(줄). 문단 하나가 대개 이 안에 든다.
NEAR = 12
# 이 말이 제목에 있으면 그 아래는 **지운 이름을 일부러 적는 자리**다 (§2.6).
DEAD_SECTION_MARKS = ("금지어", "쓰지 않는 말")
# 문서 머리에서 시제 표시를 찾을 줄 수
TENSE_HEAD_LINES = 8

errors: list[str] = []


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def walk_files():
    stack = [ROOT]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            continue
        for e in entries:
            if e.is_symlink():
                continue
            if e.is_dir():
                if e.name not in SKIP_DIRS:
                    stack.append(e)
            elif e.is_file() and e.suffix.lower() not in SKIP_SUFFIXES:
                yield e


def read_text(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def load_config() -> dict:
    if not CONFIG_FILE.is_file():
        errors.append(f"{rel(CONFIG_FILE)} 가 없다 (CLAUDE.md §2.5)")
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"{rel(CONFIG_FILE)} 를 읽을 수 없다: {exc}")
        return {}


def collect() -> dict[str, str]:
    """훑을 파일 전부 — 경로 → 내용. 목록을 손으로 들지 않는다."""
    out: dict[str, str] = {}
    for path in walk_files():
        r = rel(path)
        if r in SELF:
            continue
        text = read_text(path)
        if text is not None:
            out[r] = text
    return out


def is_code(path: str) -> bool:
    return Path(path).suffix.lower() not in DOC_SUFFIXES


def is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def matches_prefix(path: str, keys) -> str | None:
    """등록부의 키가 이 경로를 덮는가. `/` 로 끝나면 접두어로 본다."""
    for key in keys:
        if not key:
            continue  # 설명용 빈 키
        if key.endswith("/") and path.startswith(key):
            return key
        if path == key:
            return key
    return None


def strip_comments(source: str) -> str:
    """주석을 지운다. 묘비는 주석에 있으니 이름을 볼 때는 방해다."""
    return LINE_COMMENT_RE.sub(r"\1", BLOCK_COMMENT_RE.sub(" ", source))


def blank_fences(text: str) -> list[str]:
    """코드 펜스 안을 빈 줄로 만든다. **줄 번호를 지키려고** 지우지 않는다.

    문서의 코드 예시는 「지금 있는 것」이 아니다 — `SessionPolicy` 같은
    설명용 이름이 실재해야 한다고 하면 예시를 쓸 수가 없다.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        opener = FENCE_RE.match(line)
        if opener:
            marker = opener.group(1)
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            out.append("")
            continue
        out.append("" if fence is not None else line)
    return out


def word_at(name: str, lines: list[str]) -> int | None:
    """낱말 경계로 찾는다. `mount` 가 `unmount` 를 잡으면 안 된다."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])")
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            return i
    return None


def unwrap(line: str, nxt: str | None) -> str:
    """접힌 주석 두 줄을 한 줄로 편다.

    ⚠️ 줄바꿈으로 갈린 말은 한 줄만 봐서는 안 걸린다. 이어 붙일 때 주석
    표시와 앞뒤 공백을 지운다 — 그것들이 낱말 사이에 끼면 이은 뜻이 없다.
    """
    if nxt is None:
        return line
    tail = COMMENT_LEAD_RE.sub("", nxt.lstrip())
    return f"{line.rstrip()} {tail}"


def identifiers_in(text: str) -> list[str]:
    """그 줄이 **코드 이름으로 적은** 것들. 백틱 안의 식별자만 본다.

    `a.b` 는 앞 마디만 본다 — 속성까지 쫓으면 아직 안 만든 필드가 전부
    걸린다. 경로와 파일 이름은 코드 이름이 아니라 여기서 빠진다.
    """
    out: list[str] = []
    for inner in BACKTICK_RE.findall(text):
        if "/" in inner or "\\" in inner:
            continue
        tail = inner.rsplit(".", 1)
        if len(tail) == 2 and tail[1].lower() in FILE_SUFFIXES:
            continue
        name = inner.split(".")[0]
        if IDENT_RE.match(name):
            out.append(name)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. 지운 이름이 코드에 다시 나타나지 않는가 (§2.5)
# ─────────────────────────────────────────────────────────────────────────────
def check_retired_names(cfg: dict, files: dict[str, str]) -> None:
    entries = cfg.get("retired_names") or []
    for path, source in sorted(files.items()):
        if not is_code(path):
            continue
        lines = strip_comments(source).splitlines()
        for entry in entries:
            name = entry.get("이름") or entry.get("name")
            if not name:
                continue
            at = word_at(name, lines)
            if at is not None:
                why = entry.get("왜") or entry.get("why") or ""
                instead = entry.get("대신") or entry.get("instead")
                hint = f" → {instead}" if instead else ""
                errors.append(f"{path}:{at} — 지운 이름 `{name}`{hint} 가 코드에 있다. {why}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 지운 말이 글에 묘비 없이 쓰이지 않는가 (§2.5)
# ─────────────────────────────────────────────────────────────────────────────
def check_retired_words(cfg: dict, files: dict[str, str]) -> None:
    entries = [e for e in (cfg.get("retired_words") or []) if e.get("말") or e.get("word")]
    if not entries:
        return
    tombstones = cfg.get("tombstones") or []
    exempt = cfg.get("tombstone_exempt") or {}

    for path, source in sorted(files.items()):
        if matches_prefix(path, exempt):
            continue  # 통째로 '그때의 기록' 인 문서
        lines = source.splitlines()
        for i, line in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else None
            # 표는 안 잇는다. 한 줄이 한 문장이라 이어 붙이면 **없던 말이
            # 생긴다** — 옆 줄 끝의 말과 다음 줄 머리의 말이 붙는 식으로.
            joined = line if is_table_row(line) else unwrap(line, nxt)
            if any(t in line for t in TOMBSTONE_INLINE):
                continue
            # 표는 한 줄이 한 문장이다. 옆줄의 묘비를 빌려 오지 못한다.
            near = line if is_table_row(line) else "\n".join(lines[max(0, i - NEAR): i + NEAR])
            if any(t in near for t in tombstones):
                continue
            for entry in entries:
                word = entry.get("말") or entry.get("word")
                if word not in line:
                    # 이은 줄에서만 걸렸다. 다음 줄에 통째로 있으면 그때
                    # 다시 걸리므로 여기서는 넘긴다 — **줄바꿈으로 갈린
                    # 것만** 여기서 잡는다.
                    if word not in joined or (nxt is not None and word in nxt):
                        continue
                instead = entry.get("대신") or entry.get("instead") or ""
                errors.append(
                    f"{path}:{i + 1} — 지운 말 「{word}」 를 묘비 없이 썼다"
                    f"{f' → 「{instead}」' if instead else ''}"
                )


# ─────────────────────────────────────────────────────────────────────────────
# 3. '지금' 인 문서가 없는 이름을 가리키지 않는가 (§2.6)
# ─────────────────────────────────────────────────────────────────────────────
def check_live_docs(cfg: dict, files: dict[str, str]) -> None:
    live = (cfg.get("tense") or {}).get("지금") or []
    tombstones = cfg.get("tombstones") or []
    not_code = cfg.get("not_code") or {}

    haystack = "\n".join(src for path, src in files.items() if is_code(path))

    def exists(name: str) -> bool:
        return re.search(rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])", haystack) is not None

    for doc in live:
        source = files.get(doc)
        if source is None:
            # 목록을 손으로 들고 있으므로 **이름이 틀리면 조용히 안 훑는다.**
            # 그게 이 검사가 막으려는 것과 같은 종류의 구멍이라 크게 운다.
            errors.append(f"{doc} — 등록부의 「지금」 목록에 있는데 파일이 없다 (§2.8)")
            continue
        lines = blank_fences(source)
        for i, line in enumerate(lines):
            heading = HEADING_RE.match(line)
            if heading and any(m in heading.group(1) for m in DEAD_SECTION_MARKS):
                break  # 여기부터는 지운 이름을 일부러 적는 자리
            near = line if is_table_row(line) else "\n".join(lines[max(0, i - NEAR): i + NEAR])
            if any(t in near for t in tombstones):
                continue
            for name in identifiers_in(line):
                if name in not_code:
                    continue
                if not exists(name):
                    errors.append(f"{doc}:{i + 1} — `{name}` 이 코드에 없다 (§2.6)")


# ─────────────────────────────────────────────────────────────────────────────
# 4. 문서 머리의 시제 표시가 등록부와 같은가 (§2.8)
# ─────────────────────────────────────────────────────────────────────────────
def check_tense(cfg: dict, files: dict[str, str]) -> None:
    tense = cfg.get("tense") or {}
    exempt = cfg.get("tense_exempt") or {}
    registry: dict[str, str] = {}
    for kind in ("지금", "그때", "앞으로"):
        for doc in tense.get(kind) or []:
            if doc in registry:
                errors.append(f"{doc} — 등록부에 시제가 둘이다: 「{registry[doc]}」 와 「{kind}」")
            registry[doc] = kind

    for path, source in sorted(files.items()):
        if is_code(path) or matches_prefix(path, exempt):
            continue
        head = "\n".join(source.splitlines()[:TENSE_HEAD_LINES])
        found = TENSE_RE.search(head)
        listed = registry.get(path)
        if not found:
            errors.append(
                f"{path} — 머리에 「시제」 표시가 없다. "
                f"'> **시제: 지금.**' 처럼 적거나 tense_exempt 에 이유와 함께 넣는다 (§2.8)"
            )
            continue
        if listed is None:
            errors.append(f"{path} — 머리는 「{found.group(1)}」인데 등록부에 없다 (§2.8)")
        elif listed != found.group(1):
            errors.append(
                f"{path} — 머리는 「{found.group(1)}」인데 등록부에는 「{listed}」 다 (§2.8)"
            )


def main() -> int:
    cfg = load_config()
    files = collect()
    if cfg:
        check_retired_names(cfg, files)
        check_retired_words(cfg, files)
        check_live_docs(cfg, files)
        check_tense(cfg, files)

    for e in errors:
        print(f"error: {e}")

    docs = sum(1 for p in files if not is_code(p))
    if errors:
        print(f"\n실패: {len(errors)}건 — 문서와 코드가 갈라져 있다 (CLAUDE.md §2.5~§2.8)")
        return 1
    print(f"통과: 문서 {docs}개 · 파일 {len(files)}개 — 문서와 코드 이상 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
