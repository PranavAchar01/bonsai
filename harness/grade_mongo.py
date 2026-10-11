"""Grade one nursery answer: run the pipeline on the full collection and compare with the expected rows."""

import json
import os
import sys

from pymongo import MongoClient

WRITES = ("$out", "$merge")


def same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.006
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def main() -> int:
    expected = json.load(open(sys.argv[2]))
    try:
        spec = json.load(open(sys.argv[1]))
        if spec["collection"] not in ("orders", "trees") or any(w in json.dumps(spec["pipeline"]) for w in WRITES):
            print("pipeline must read from orders or trees and must not write")
            return 1
        db = MongoClient(os.getenv("BONSAI_MONGO_URL", "mongodb://127.0.0.1:27018"), serverSelectionTimeoutMS=4000)["nursery"]
        got = json.loads(json.dumps(list(db[spec["collection"]].aggregate(spec["pipeline"], maxTimeMS=20000)), default=str))
    except Exception as e:
        print(f"pipeline did not run: {type(e).__name__}: {str(e)[:300]}")
        return 1
    if same(got, expected):
        print("correct")
        return 0
    # The agent is shown the shape of the miss, never the expected values.
    keys = sorted(got[0].keys()) if got and isinstance(got[0], dict) else []
    print(f"wrong answer: returned {len(got)} documents with fields {keys}; expected {len(expected)} "
          f"documents with fields {sorted(expected[0].keys())}")  # fmt: skip
    return 1


if __name__ == "__main__":
    sys.exit(main())
