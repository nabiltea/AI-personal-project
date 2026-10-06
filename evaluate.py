"""
Score the classifier against the hand-labelled test set (data/test_set.json).

Run:
    python evaluate.py --mode baseline             # v1: original prompt
    python evaluate.py --mode fewshot              # v2: v1 + similar past reviews
    python evaluate.py --mode rules                # v3: v1 + date + urgency rules
    python evaluate.py --mode rules-fewshot        # v4: v3 + past reviews, urgency only
    python evaluate.py --mode rules --dry-run      # print the prompts, no API calls
    python evaluate.py --report                    # compare everything scored so far

Each answer is saved to data/eval_results.jsonl the moment it comes back, and a
re-run skips cases already scored in the same run. On the free tier (20 Gemini
requests a day) a crash halfway must not throw away answers already paid for.
"""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from capture_corrections import SCORED_FIELDS
from enrich_lead import (
    CORRECTIONS_FILE,
    LIVE_MODE,
    PROMPT_MODES,
    QuotaExhausted,
    build_prompt,
    enrich_with_gemini,
    format_received,
)
from examples import find_similar, format_examples, load_pool

DATA = Path(__file__).resolve().parent / "data"
TEST_SET = DATA / "test_set.json"
RESULTS = DATA / "eval_results.jsonl"

# Each test case stores the date it "came in" (its labels are correct relative
# to that date), so the result is the same whenever the evaluation is run.


def load_test_set() -> list:
    with open(TEST_SET, encoding="utf-8") as handle:
        cases = json.load(handle)
    required = list(SCORED_FIELDS) + ["received"]
    incomplete = [c["id"] for c in cases if not all(c.get(f) for f in required)]
    if incomplete:
        raise RuntimeError(
            f"These test cases are missing a label or received date: {incomplete}"
        )
    return cases


def check_no_leak(cases: list, pool: list) -> None:
    """Refuse to run if a test request is also in the example pool.

    The model would then be shown the answer to the very question it is being
    scored on, and the score would mean nothing.
    """
    pool_texts = {r["product_request"].strip().lower() for r in pool}
    leaked = [
        c["id"] for c in cases if c["product_request"].strip().lower() in pool_texts
    ]
    if leaked:
        raise RuntimeError(f"Test cases also found in the example pool: {leaked}")


def load_results() -> list:
    if not RESULTS.exists():
        return []
    with open(RESULTS, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def save_result(record: dict) -> None:
    with open(RESULTS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def score(mode: str, pool_path: Path, dry_run: bool) -> None:
    cases = load_test_set()
    tell_date, use_examples, example_fields = PROMPT_MODES[mode]
    pool = load_pool(pool_path) if use_examples else []
    check_no_leak(cases, pool)

    # The run name ties results together, e.g. "baseline" or
    # "fewshot:corrections.jsonl", so a later run with a different example pool
    # is kept separate.
    run = f"{mode}:{pool_path.name}" if use_examples else mode
    done = {r["case_id"] for r in load_results() if r["run"] == run}

    for case in cases:
        if case["id"] in done:
            continue

        examples = find_similar(case["product_request"], pool) if pool else []
        block = format_examples(examples, example_fields) if examples else ""
        received = (
            format_received(date.fromisoformat(case["received"])) if tell_date else None
        )

        if dry_run:
            print(f"===== {case['id']} =====")
            print(build_prompt(case["product_request"], block, received), "\n")
            continue

        expected = {f: case[f] for f in SCORED_FIELDS}
        try:
            answer = enrich_with_gemini(case["product_request"], block, received)
            predicted = {f: answer[f] for f in SCORED_FIELDS}
            prompt_version, error = answer["prompt_version"], None
        except QuotaExhausted as stop:
            # Nothing is saved for this case, so the next run picks it up.
            print(f"\n{stop} Run the same command again after the reset.")
            break
        except Exception as failure:
            # A broken answer counts as wrong: in production it would be.
            predicted = {f: None for f in SCORED_FIELDS}
            prompt_version, error = None, str(failure)

        correct = {f: predicted[f] == expected[f] for f in SCORED_FIELDS}
        save_result(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run": run,
                "prompt_version": prompt_version,
                "case_id": case["id"],
                "expected": expected,
                "predicted": predicted,
                "correct": correct,
                "examples": [e["deal_id"] for e in examples],
                "error": error,
            }
        )
        verdict = "error: " + error[:60] if error else (
            "right" if all(correct.values()) else "WRONG"
        )
        print(f"{case['id']}: {predicted} expected {expected} -> {verdict}")


def resolve_run(name: str, runs: list) -> str:
    """Accept a full run name ("rules-fewshot:corrections.jsonl") or a mode."""
    if name in runs:
        return name
    matches = [run for run in runs if run.split(":")[0] == name]
    if len(matches) != 1:
        raise RuntimeError(f"No single saved run matches '{name}'. Saved: {runs}")
    return matches[0]


def gate(candidate: str, current: str) -> bool:
    """Decide whether the candidate may replace the current version.

    Rule: promote if the candidate gets at least as many test cases fully
    right (category and urgency) as the current version. A version that
    trades one category for three urgencies still counts as better overall.
    """
    cases = load_test_set()
    results = load_results()
    runs = list(dict.fromkeys(r["run"] for r in results))
    candidate, current = resolve_run(candidate, runs), resolve_run(current, runs)
    latest = {(r["run"], r["case_id"]): r for r in results}

    scores = {}
    for run in (candidate, current):
        missing = [c["id"] for c in cases if (run, c["id"]) not in latest]
        if missing:
            raise RuntimeError(f"'{run}' has not been scored on {missing} yet.")
        scores[run] = sum(all(latest[(run, c["id"])]["correct"].values()) for c in cases)

    promote = scores[candidate] >= scores[current]
    n = len(cases)
    print(
        f"candidate {candidate}: {scores[candidate]}/{n} fully right\n"
        f"current   {current}: {scores[current]}/{n} fully right\n"
        f"-> {'PROMOTE' if promote else 'REJECT'}"
    )
    return promote


def report() -> None:
    """Print every case against every run, then accuracy per run."""
    cases = load_test_set()
    results = load_results()
    runs = list(dict.fromkeys(r["run"] for r in results))
    if not runs:
        print("No results yet.")
        return
    latest = {(r["run"], r["case_id"]): r for r in results}

    def cell(result):
        if result is None:
            return "-"
        if result["error"]:
            return "error"
        p = result["predicted"]
        mark = "ok" if all(result["correct"].values()) else "XX"
        return f"{p['category'][:5]} u{p['urgency']} {mark}"

    print(f"\n{'case':<5} {'expected':<12}" + "".join(f"{run:<26}" for run in runs))
    for case in cases:
        expected = f"{case['category'][:5]} u{case['urgency']}"
        cells = "".join(f"{cell(latest.get((run, case['id']))):<26}" for run in runs)
        print(f"{case['id']:<5} {expected:<12}{cells}")

    print()
    for run in runs:
        scored = [latest[(run, c["id"])] for c in cases if (run, c["id"]) in latest]
        n = len(scored)
        parts = [
            f"{f} {sum(r['correct'][f] for r in scored)}/{n}" for f in SCORED_FIELDS
        ]
        both = sum(all(r["correct"].values()) for r in scored)
        print(f"{run:<26} " + ", ".join(parts) + f", both right {both}/{n}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=list(PROMPT_MODES))
    parser.add_argument(
        "--pool",
        type=Path,
        default=CORRECTIONS_FILE,
        help="Example pool for fewshot mode (default: data/corrections.jsonl)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print prompts only")
    parser.add_argument("--report", action="store_true", help="Compare saved runs")
    parser.add_argument(
        "--gate",
        metavar="CANDIDATE",
        help="Decide whether CANDIDATE may replace the --against run",
    )
    parser.add_argument(
        "--against",
        default=LIVE_MODE,
        help=f"Run the candidate must beat (default: the live one, {LIVE_MODE})",
    )
    args = parser.parse_args()

    if args.mode:
        score(args.mode, args.pool, args.dry_run)
    if args.report or (args.mode and not args.dry_run):
        report()
    if args.gate:
        # Exit code 0 = promote, 1 = reject, so scripts can act on it.
        return 0 if gate(args.gate, args.against) else 1
    if not (args.mode or args.report):
        parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
