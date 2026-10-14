"""Wait for the parent to persist our PID before replacing this process with Git."""

from __future__ import annotations

import os
import sys


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments or sys.stdin.buffer.readline().strip() != b"go":
        return 0
    os.execvpe(arguments[0], arguments, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
