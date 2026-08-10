#!/usr/bin/env python3
"""명세 ↔ 코드 양방향 링크 검사 (CLAUDE.md §2.4).

`docs/spec/**/*.md` 의 `### SPEC-<AREA>-<NNN>` 항목과, 저장소 전체에 흩어진
`[SPEC-<AREA>-<NNN>]` 태그가 서로를 가리키는지 확인한다.

언어 중립이다 — 주석 문법을 파싱하지 않고 대괄호 태그를 문자열로 찾는다.
의존성 없음(표준 라이브러리만). 실패가 하나라도 있으면 종료 코드 1.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = ROOT / "docs" / "spec"
RETIRED_FILE = SPEC_DIR / "retired-ids.json"

# 검사 대상에서 제외하는 디렉터리 이름
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    "out", "bin", "obj", "target", "vendor", "Pods", ".next", ".gradle",
    ".idea", ".vs", ".evidence",
}
# 태그를 찾을 때 건너뛰는 파일 확장자 (바이너리 · 대용량)
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".mp4", ".mov", ".webm", ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll",
    ".dylib", ".exe", ".bin", ".wasm", ".lock",
}
MAX_FILE_BYTES = 2_000_000

FENCE_RE = re.compile(r"^\s*(```|~~~)")
MD_FENCE_BLOCK_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.DOTALL | re.MULTILINE)

ID_RE = re.compile(r"SPEC-[A-Z0-9]+-\d+")
HEADING_RE = re.compile(r"^#{2,4}\s+(SPEC-[A-Z0-9]+-\d+)\b(.*)$")
TAG_RE = re.compile(r"\[(SPEC-[A-Z0-9]+-\d+)\]")
# - **상태** — ✅   /   - **Status** - done
FIELD_RE = re.compile(
    r"^\s*[-*]\s*\**\s*(상태|구현|검증|status|impl|implementation|verify|test)\s*\**\s*[—\-–:]\s*(.+?)\s*$",
    re.IGNORECASE,
)
FIELD_ALIASES = {
    "상태": "status", "status": "status",
    "구현": "impl", "impl": "impl", "implementation": "impl",
    "검증": "verify", "verify": "verify", "test": "verify",
}
DONE_MARKS = ("✅", "done", "complete", "완료")

errors: list[str] = []
warnings: list[str] = []


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


def parse_specs() -> dict[str, dict]:
    """docs/spec/**/*.md 에서 명세 항목을 읽는다."""
    specs: dict[str, dict] = {}
    if not SPEC_DIR.is_dir():
        errors.append(f"{rel(SPEC_DIR)} 가 없다 (CLAUDE.md §2.4)")
        return specs

    for md in sorted(SPEC_DIR.rglob("*.md")):
        text = read_text(md)
        if text is None:
            continue
        current: dict | None = None
        fence: str | None = None
        for lineno, line in enumerate(text.splitlines(), 1):
            f_open = FENCE_RE.match(line)
            if f_open:
                marker = f_open.group(1)
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence is not None:
                continue  # 코드 펜스 안의 예시는 명세가 아니다
            m = HEADING_RE.match(line)
            if m:
                spec_id, title = m.group(1), m.group(2).strip(" ·-—")
                if spec_id in specs:
                    prev = specs[spec_id]
                    errors.append(
                        f"ID 중복: {spec_id} — {rel(md)}:{lineno} 와 "
                        f"{prev['file']}:{prev['line']}"
                    )
                current = {
                    "id": spec_id, "title": title, "file": rel(md), "line": lineno,
                    "status": "", "impl": [], "verify": [],
                }
                specs[spec_id] = current
                continue
            if current is None:
                continue
            if line.startswith("#"):
                current = None
                continue
            f = FIELD_RE.match(line)
            if not f:
                continue
            key = FIELD_ALIASES[f.group(1).lower()]
            value = f.group(2).strip()
            if key == "status":
                current["status"] = value
            else:
                for item in re.split(r"[,·]", value):
                    item = item.strip().strip("`").strip()
                    if item and item != "-":
                        current[key].append(item)
    return specs


def load_retired() -> set[str]:
    if not RETIRED_FILE.is_file():
        return set()
    try:
        data = json.loads(RETIRED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"{rel(RETIRED_FILE)} 를 읽을 수 없다: {exc}")
        return set()
    out: set[str] = set()
    for item in data.get("retired", []):
        if isinstance(item, str):
            out.add(item)
        elif isinstance(item, dict) and "id" in item:
            out.add(item["id"])
    return out


def scan_repo(specs: dict[str, dict]) -> tuple[dict[str, set[str]], list[str]]:
    """저장소 전체에서 [SPEC-…] 태그와 검증 이름을 찾는다.

    반환: (태그 → 그 태그가 있는 파일 집합, 검증 이름을 찾을 때 쓸 텍스트 조각들)
    """
    tags: dict[str, set[str]] = {}
    wanted_verify = {v for s in specs.values() for v in s["verify"]}
    found_verify: set[str] = set()

    for path in walk_files():
        r = rel(path)
        if r.startswith("docs/spec/"):
            continue
        text = read_text(path)
        if text is None:
            continue
        if path.suffix.lower() in (".md", ".markdown"):
            # 문서의 코드 예시는 실제 태그가 아니다
            text = MD_FENCE_BLOCK_RE.sub("", text)
        for tag in set(TAG_RE.findall(text)):
            tags.setdefault(tag, set()).add(r)
        if wanted_verify:
            for name in wanted_verify - found_verify:
                if name in text:
                    found_verify.add(name)

    return tags, sorted(wanted_verify - found_verify)


def main() -> int:
    specs = parse_specs()
    retired = load_retired()
    tags, missing_verify = scan_repo(specs)

    for spec_id, spec in sorted(specs.items()):
        where = f"{spec['file']}:{spec['line']}"

        if spec_id in retired:
            errors.append(f"{spec_id}: 폐기된 ID 인데 명세에 살아 있다 ({where})")

        if not spec["status"]:
            errors.append(f"{spec_id}: **상태** 필드가 없다 ({where})")

        status = spec["status"].lower()
        done = any(mark in status for mark in DONE_MARKS)

        # 구현 경로가 실제로 존재하고, 그 파일이 ID 를 되가리키는가
        for impl in spec["impl"]:
            target = ROOT / impl
            if not target.exists():
                errors.append(f"{spec_id}: 구현 경로가 없다 — {impl} ({where})")
                continue
            if impl not in tags.get(spec_id, set()):
                if target.is_dir():
                    hit = any(impl in f for f in tags.get(spec_id, set()))
                    if hit:
                        continue
                errors.append(
                    f"{spec_id}: {impl} 에 [{spec_id}] 태그가 없다 — "
                    f"코드가 명세를 되가리키지 않는다 ({where})"
                )

        if done and not spec["impl"]:
            errors.append(f"{spec_id}: 완료인데 **구현** 이 없다 ({where})")
        if done and not spec["verify"]:
            errors.append(
                f"{spec_id}: 완료인데 **검증** 이 없다 — "
                f"'돌아간다'를 완료로 적었다 ({where})"
            )

    # 코드에만 있고 명세에 없는 태그
    for tag, files in sorted(tags.items()):
        if tag in specs:
            continue
        loc = ", ".join(sorted(files)[:3]) + (" …" if len(files) > 3 else "")
        if tag in retired:
            errors.append(f"{tag}: 폐기된 ID 를 코드가 아직 가리킨다 — {loc}")
        else:
            errors.append(f"{tag}: 명세에 없는 태그를 코드가 가리킨다 — {loc}")

    for name in missing_verify:
        owners = ", ".join(s["id"] for s in specs.values() if name in s["verify"])
        errors.append(f"검증 테스트를 찾을 수 없다: '{name}' ({owners})")

    for bad in sorted(ID_RE.findall(RETIRED_FILE.read_text(encoding="utf-8"))
                      if RETIRED_FILE.is_file() else []):
        if bad in specs and bad not in retired:
            warnings.append(f"{bad}: retired-ids.json 에 언급됐지만 retired 목록에는 없다")

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")

    total = len(specs)
    if errors:
        print(f"\n실패: 명세 {total}개 중 {len(errors)}건의 링크 문제 (CLAUDE.md §2.4)")
        return 1
    print(f"통과: 명세 {total}개, 태그 {len(tags)}개 — 링크 이상 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
