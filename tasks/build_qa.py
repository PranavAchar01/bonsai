"""Build the question sets: legal (LegalBench), health (MedQA) and finance (FinQA).

  uv run --no-project python tasks/build_qa.py legal health finance

Each set gets 40 problems, q001..q040, drawn with a fixed seed. Odd numbers are practice and even
numbers are held out. Every problem folder holds the question and an expected.json that the
agent never sees. Graded by harness/grade_exact.py.
"""

import io
import json
import random
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
N = 40
SEED = {"rules": "Answer the question in .docs/instructions.md and write the answer to answer.txt.",
        "tools": ["Read", "Write"], "max_turns": 4}  # fmt: skip
NAMES = {"legal": "Legal: LegalBench", "health": "Health: MedQA (USMLE)", "finance": "Finance: FinQA"}


def get(url: str, tries: int = 4) -> bytes:
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except OSError:  # the datasets server sometimes answers 502
            if i == tries - 1:
                raise
            time.sleep(3)


def hf_rows(dataset: str, config: str, split: str) -> list[dict]:
    rows, offset = [], 0
    while True:
        url = f"https://datasets-server.huggingface.co/rows?dataset={dataset}&config={config}&split={split}&offset={offset}&length=100"
        page = json.loads(get(url))
        rows += [r["row"] for r in page["rows"]]
        offset += 100
        if offset >= page["num_rows_total"]:
            return rows


LEGAL = {
    "hearsay": ("Hearsay",
                "Under the US Federal Rules of Evidence, is the following evidence hearsay?",
                "Write only Yes or No to answer.txt."),
    # abercrombie and personal_jurisdiction were tried first and replaced: the seed got 12 of 13 right on each
    "citation_prediction_classification": ("Citation prediction",
                                           "Does the case named below support the legal statement that follows?",
                                           "Write only Yes or No to answer.txt."),
    "insurance_policy_interpretation": ("Insurance policy interpretation",
                                        "Read the policy and the claim. A: the claim is covered by the policy. "
                                        "B: the claim is not covered. C: it is ambiguous whether the claim is covered.",
                                        "Write only the letter A, B or C to answer.txt."),
}


def legal(rng: random.Random) -> list[tuple[str, str]]:
    out = []
    for i, (task, (title, ask, fmt)) in enumerate(LEGAL.items()):
        rows = rng.sample(hf_rows("nguha/legalbench", task, "test"), N // 3 + (i < N % 3))
        body = lambda r: (f"Policy: {r['policy']}\n>\n> Claim: {r['claim']}" if "policy" in r
                          else f"Case: {r['citation']}\n>\n> Statement: {r['text']}" if "citation" in r else r["text"].strip())  # noqa: E731
        out += [(f"# {title}\n\n{ask}\n\n> {body(r)}\n\n{fmt}\n", r["answer"]) for r in rows]
    return out


def health(rng: random.Random) -> list[tuple[str, str]]:
    url = "https://huggingface.co/datasets/GBaker/MedQA-USMLE-4-options/resolve/main/phrases_no_exclude_test.jsonl"
    rows = rng.sample([json.loads(line) for line in get(url).decode().splitlines() if line.strip()], N)
    return [(f"# Medical exam question\n\n{r['question'].strip()}\n\n"
             + "\n".join(f"{k}. {v}" for k, v in sorted(r["options"].items()))
             + "\n\nWrite only the letter A, B, C or D to answer.txt.\n", r["answer_idx"]) for r in rows]  # fmt: skip


def finance(rng: random.Random) -> list[tuple[str, str]]:
    z = zipfile.ZipFile(io.BytesIO(get("https://github.com/czyssrs/FinQA/archive/refs/heads/main.zip")))
    rows = json.loads(z.read("FinQA-main/dataset/test.json"))
    # Questions that need two or more calculation steps, with a plain numeric answer.
    ok = [r for r in rows if r["qa"]["program"].count("),") >= 1
          and re.fullmatch(r"-?\$?[\d,]*\.?\d+%?", r["qa"]["answer"].strip())]  # fmt: skip
    out = []
    for r in rng.sample(ok, N):
        table = "\n".join("| " + " | ".join(c.strip() for c in row) + " |" for row in r["table"])
        pct = r["qa"]["answer"].strip().endswith("%")
        out.append((f"# Financial report question\n\n{' '.join(r['pre_text'])}\n\n{table}\n\n{' '.join(r['post_text'])}\n\n"
                    f"**Question:** {r['qa']['question']}\n\n"
                    "Write only the number to answer.txt, with no units or words. "
                    "If the answer is a percentage, write it as a percent, for example 12.5 for 12.5%.\n",
                    r["qa"]["answer"].strip().replace("$", "").replace("%", "").replace(",", "") + ("%" if pct else "")))  # fmt: skip
    return out


for key in sys.argv[1:]:
    problems = {"legal": legal, "health": health, "finance": finance}[key](random.Random(0))
    random.Random(1).shuffle(problems)  # mixes legal's three tasks across practice and held-out
    root = HERE / key
    for old in root.glob("q*"):
        shutil.rmtree(old)
    for i, (text, answer) in enumerate(problems, 1):
        d = root / f"q{i:03d}"
        (d / ".docs").mkdir(parents=True)
        (d / ".docs/instructions.md").write_text(text)
        (d / "expected.json").write_text(json.dumps({"answer": answer}))
    (root / "set.json").write_text(json.dumps({
        "name": NAMES[key],
        "prompt": "Answer the question in .docs/instructions.md and write the answer to answer.txt.",
        "solution": ["answer.txt"],
        "hidden": ["expected.json"],
        "grade": "uv run -q --no-project python \"{root}/harness/grade_exact.py\" answer.txt \"{task}/expected.json\"",
        "seed": SEED,
    }, indent=2))
    print(key, len(problems), "problems")
