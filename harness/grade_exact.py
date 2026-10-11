"""Grade one answer against the hidden expected answer. Exit 0 on a match.

  python grade_exact.py answer.txt expected.json

Text is compared after trimming, lowercasing and dropping a trailing full stop.
If both sides are numbers ($, % and commas removed), they match within 1%.
"""

import json
import re
import sys
from pathlib import Path


def norm(s: str) -> str:
    return " ".join(str(s).strip().lower().rstrip(".").split())


def number(s: str) -> float | None:
    try:
        return float(re.sub(r"[$%,\s]", "", s))
    except ValueError:
        return None


def main() -> int:
    answer_file, expected_file = Path(sys.argv[1]), Path(sys.argv[2])
    if not answer_file.exists():
        print("no answer.txt")
        return 1
    got, want = norm(answer_file.read_text()), norm(json.loads(expected_file.read_text())["answer"])
    a, b = number(got), number(want)
    ok = got == want or (a is not None and b is not None and abs(a - b) <= 0.01 * max(abs(b), 1e-9))
    print("correct" if ok else f"wrong: got {got[:60]!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
