#!/usr/bin/env python3
"""Checks a commit message: Conventional Commits, English (ASCII) header.

Usage: check_commit_msg.py <message file>   (also used as the commit-msg git hook)
       check_commit_msg.py -                (message on stdin)
"""

import re
import sys

TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
HEADER = re.compile(rf"^({TYPES})(\([a-z0-9-]+\))?!?: \S.*$")
MAX_HEADER = 72


def problems(message: str) -> list[str]:
    lines = [line for line in message.splitlines() if not line.startswith("#")]  # git's own comment lines
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ["empty commit message"]
    header, found = lines[0], []
    if header.startswith("Merge "):
        return []
    if not HEADER.match(header):
        found.append(f'header must look like "type(scope): subject" with type one of {TYPES.replace("|", ", ")}')
    if not header.isascii():
        found.append("header must be in English (ASCII only)")
    if len(header) > MAX_HEADER:
        found.append(f"header is {len(header)} characters, the limit is {MAX_HEADER}")
    if header.endswith("."):
        found.append("header must not end with a period")
    if len(lines) > 1 and lines[1].strip():
        found.append("leave a blank line between the header and the body")
    return found


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "-"
    message = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    found = problems(message)
    for problem in found:
        print(f"commit message: {problem}", file=sys.stderr)
    if found:
        print(f"  got: {message.splitlines()[0] if message.strip() else ''}", file=sys.stderr)
        print("  example: feat(library): add sorting by date", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
