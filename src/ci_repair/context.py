"""Bounded, deterministic evidence extraction; logs remain untrusted data."""

import re

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR = re.compile(
    r"(?:\b(?:[A-Za-z]*Error|Exception):|\bTraceback\b|\bFAILED\b|\bFAIL:|"
    r"\berror(?:\[|:| )|^E\s{2,})",
    re.IGNORECASE,
)
LOCATION = re.compile(
    r'File "([^"\n]+)", line (\d{1,9})(?!\d)|(?:^|\s)([^\s:]+\.(?:py|js|ts|tsx|go|rs|c|cpp)):(\d{1,9})(?!\d)'
)


def failure_evidence(log: str) -> dict:
    # Line numbers always refer to the original log, including timestamp prefixes.
    lines = [ANSI.sub("", line) for line in log.splitlines()]
    matches = [i for i, line in enumerate(lines) if ERROR.search(line)]
    anchors = sorted(set(matches[:4] + matches[-4:]))
    spans = []
    for anchor in anchors:
        start, end = max(0, anchor - 2), min(len(lines), anchor + 4)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    blocks = []
    remaining = 7000
    for start, end in spans:
        if remaining <= 0:
            break
        raw = "\n".join(lines[start:end])
        excerpt = raw[: min(2000, remaining)]
        blocks.append(
            {
                "start_line": start + 1,
                "end_line": end,
                "text": excerpt,
                "truncated": len(excerpt) < len(raw),
            }
        )
        remaining -= len(excerpt)
    locations = []
    seen = set()
    for number, line in enumerate(lines, 1):
        match = LOCATION.search(line)
        if match:
            path, lineno = (match[1], match[2]) if match[1] else (match[3], match[4])
            item = (path, lineno)
            if item not in seen:
                locations.append({"path": path[:160], "line": int(lineno), "log_line": number})
                seen.add(item)
            if len(locations) == 8:
                break
    raw = log if len(log) <= 6000 else log[:2000] + "\n[log middle omitted]\n" + log[-4000:]
    return {
        "parser_version": 1,
        "total_lines": len(lines),
        "error_blocks": blocks,
        "locations": locations,
        "raw_excerpt": raw,
        "raw_truncated": len(log) > 6000,
    }


TIMESTAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ")
WORKDIR = re.compile(r"/home/runner/work/[^/\s]+/[^/\s]+/|/__w/[^/\s]+/[^/\s]+/|/workspace/")


def error_signature(text: str) -> set[str]:
    """Normalized error lines, independent of runner paths, timestamps and colors."""
    signature = set()
    for line in text.splitlines():
        line = WORKDIR.sub("", TIMESTAMP.sub("", ANSI.sub("", line))).strip()
        if ERROR.search(line):
            signature.add(" ".join(line.split())[:200])
    return signature


def evidence_overlap(ci_log: str, replay_output: str) -> bool | None:
    """Does the replayed baseline show at least one error line the CI log showed?

    None means the CI log had no recognizable error line to compare.
    """
    expected = error_signature(ci_log)
    return bool(expected & error_signature(replay_output)) if expected else None
