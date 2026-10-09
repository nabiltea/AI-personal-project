"""
Score prompt versions against a hand-labelled test set, compare them, and
decide which one the live pipeline runs.

Run:
    python evaluate.py --mode baseline              # v1: original prompt
    python evaluate.py --mode fewshot               # v2: v1 + similar past reviews
    python evaluate.py --mode rules                 # v3: v1 + date + urgency rules
    python evaluate.py --mode rules-fewshot         # v4: v3 + past reviews, urgency only
    python evaluate.py --mode rules-fewshot-alt     # v5: v4 + "could it be another category?"
    python evaluate.py --mode rules --dry-run       # print the prompts, no API calls
    python evaluate.py --report                     # compare everything scored so far
    python evaluate.py --gate rules-fewshot-alt     # compare with the live version
    python evaluate.py --gate rules-fewshot-alt --promote   # ...and switch if it passes
    python evaluate.py --rollback                   # undo the last promotion

Add --test-set data/<file>.json to use a different test set. Modes that use
examples take them from data/corrections.jsonl unless --pool says otherwise.

Each answer is saved to data/eval_results.jsonl the moment it comes back, and a
re-run skips cases already scored in the same run. On the free tier (20 Gemini
requests a day) a crash halfway must not throw away answers already paid for.
"""

import argparse
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from capture_corrections import SCORED_FIELDS
from enrich_lead import (
    CORRECTIONS_FILE,
    LIVE_FILE,
    POOLS_DIR,
    PROJECT_DIR,
    PROMPT_MODES,
    QuotaExhausted,
    build_prompt,
    enrich_with_gemini,
    format_received,
    live_pool_path,
    load_live,
    pool_id,
)
from examples import find_similar, format_examples, load_pool

DATA = PROJECT_DIR / "data"
DEFAULT_TEST_SET = DATA / "test_set.json"
RESULTS = DATA / "eval_results.jsonl"
PROMOTIONS = DATA / "promotions.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_test_set(path: Path) -> list:
    """Load and check a test set.

    Each case stores the date it "came in" (its labels are correct relative to
    that date), so the result is the same whenever the evaluation is run. An
    optional "needs_review" label says whether a person should see the case,
    which is what the flags are scored against.
    """
    with open(path, encoding="utf-8") as handle:
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


def run_name(mode: str, pool_path) -> str:
    """A run is a prompt version plus, if it uses examples, the exact pool.

    The pool is named by its contents, so a run against yesterday's
    corrections is kept apart from one against today's.
    """
    uses_examples = PROMPT_MODES[mode][1]
    return f"{mode}:{pool_id(pool_path)}" if uses_examples else mode


def live_run() -> str:
    live = load_live()
    return run_name(live["mode"], live_pool_path(live))


def load_results(test_set: Path) -> list:
    """Saved answers for one test set (older records predate --test-set)."""
    if not RESULTS.exists():
        return []
    with open(RESULTS, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [r for r in records if r.get("test_set", DEFAULT_TEST_SET.name) == test_set.name]


def save_result(record: dict) -> None:
    with open(RESULTS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def score(mode: str, pool_path: Path, test_set: Path, dry_run: bool) -> None:
    cases = load_test_set(test_set)
    tell_date, use_examples, example_fields, ask_alternative = PROMPT_MODES[mode]
    pool = load_pool(pool_path) if use_examples else []
    check_no_leak(cases, pool)

    run = run_name(mode, pool_path)
    done = {r["case_id"] for r in load_results(test_set) if r["run"] == run}

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
            print(build_prompt(case["product_request"], block, received, ask_alternative), "\n")
            continue

        expected = {f: case[f] for f in SCORED_FIELDS}
        try:
            answer = enrich_with_gemini(
                case["product_request"], block, received, ask_alternative
            )
            predicted = {f: answer[f] for f in SCORED_FIELDS}
            flags = answer["flags"]
            prompt_version, error = answer["prompt_version"], None
        except QuotaExhausted as stop:
            # Nothing is saved for this case, so the next run picks it up.
            print(f"\n{stop} Run the same command again after the reset.")
            break
        except Exception as failure:
            # A broken answer counts as wrong: in production it would be.
            predicted = {f: None for f in SCORED_FIELDS}
            flags = []
            prompt_version, error = None, str(failure)

        correct = {f: predicted[f] == expected[f] for f in SCORED_FIELDS}
        save_result(
            {
                "timestamp": now(),
                "test_set": test_set.name,
                "run": run,
                "prompt_version": prompt_version,
                "case_id": case["id"],
                "expected": expected,
                "predicted": predicted,
                "correct": correct,
                # Kept so the report can check whether flags land on the
                # cases that need a person.
                "flags": flags,
                "examples": [e["deal_id"] for e in examples],
                "error": error,
            }
        )
        verdict = "error: " + error[:60] if error else (
            "right" if all(correct.values()) else "WRONG"
        )
        print(f"{case['id']}: {predicted} expected {expected} -> {verdict}")


def records_for(run: str, cases: list, latest: dict) -> list:
    missing = [c["id"] for c in cases if (run, c["id"]) not in latest]
    if missing:
        raise RuntimeError(f"'{run}' has not been scored on {missing} yet.")
    return [latest[(run, c["id"])] for c in cases]


def flag_stats(records: list, cases: list):
    """How well the flags pick out the cases that should go to review.

    Returns None for runs from before flags were recorded. The precision and
    recall figures need every case to carry a "needs_review" label.
    """
    if not records or any("flags" not in r for r in records):
        return None
    labels = {c["id"]: c.get("needs_review") for c in cases}
    flagged = {r["case_id"] for r in records if r["flags"]}
    wrong = {r["case_id"] for r in records if not all(r["correct"].values())}
    stats = {
        "n": len(records),
        "flagged": len(flagged),
        "mistakes": len(wrong),
        "mistakes_flagged": len(wrong & flagged),
    }
    if all(labels[r["case_id"]] is not None for r in records):
        needed = {cid for cid, label in labels.items() if label and cid in labels}
        needed &= {r["case_id"] for r in records}
        stats.update(
            needed=len(needed),
            needed_flagged=len(needed & flagged),
            # Share of cases where flag and label agree, used to break ties.
            agree=sum((r["case_id"] in flagged) == bool(labels[r["case_id"]]) for r in records),
        )
    return stats


def describe_flags(stats: dict) -> str:
    text = f"flagged {stats['flagged']}/{stats['n']} ({stats['flagged'] / stats['n']:.0%} review load)"
    if "needed" in stats:
        text += (
            f", caught {stats['needed_flagged']} of {stats['needed']} cases that needed "
            f"review, {stats['flagged'] - stats['needed_flagged']} flags were unnecessary"
        )
    if stats["mistakes"]:
        text += f", {stats['mistakes_flagged']} of {stats['mistakes']} mistakes flagged"
    else:
        text += ", no mistakes to catch"
    return text


def gate(mode: str, pool_path: Path, test_set: Path, against: str = None):
    """Decide whether a candidate may replace the current version.

    Rule: promote if the candidate gets at least as many test cases fully
    right (category and urgency) as the current version. On a tie, it must
    also be no worse at flagging, when the test set says which cases need
    review. Returns (promote, candidate run, score text).
    """
    cases = load_test_set(test_set)
    latest = {(r["run"], r["case_id"]): r for r in load_results(test_set)}
    candidate = run_name(mode, pool_path)
    current = live_run() if against is None else run_name(against, CORRECTIONS_FILE)

    if candidate == current:
        print(f"{candidate} is already the current version.")
        return False, candidate, None

    right, flags = {}, {}
    for run in (candidate, current):
        records = records_for(run, cases, latest)
        right[run] = sum(all(r["correct"].values()) for r in records)
        flags[run] = flag_stats(records, cases)

    promote = right[candidate] > right[current]
    tie_note = ""
    if right[candidate] == right[current]:
        both_scored = all(flags[run] and "agree" in flags[run] for run in (candidate, current))
        promote = (not both_scored) or flags[candidate]["agree"] >= flags[current]["agree"]
        tie_note = " (tie, decided on flags)" if both_scored else " (tie)"

    n = len(cases)
    for label, run in (("candidate", candidate), ("current  ", current)):
        line = f"{label} {run}: {right[run]}/{n} fully right"
        if flags[run]:
            line += f"; {describe_flags(flags[run])}"
        print(line)
    print(f"-> {'PROMOTE' if promote else 'REJECT'}{tie_note}")
    return promote, candidate, f"{right[candidate]}/{n} vs {right[current]}/{n}"


def promote(mode: str, pool_path: Path, run: str, test_set: Path, score_text: str) -> None:
    """Make the candidate live, freezing the exact pool it was tested with."""
    live_pool = None
    if PROMPT_MODES[mode][1]:
        POOLS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = POOLS_DIR / f"{pool_id(pool_path)}.jsonl"
        shutil.copyfile(pool_path, snapshot)
        live_pool = str(snapshot.relative_to(PROJECT_DIR))

    entry = {
        "promoted_at": now(),
        "mode": mode,
        "pool": live_pool,
        "run": run,
        "test_set": test_set.name,
        "score": score_text,
    }
    LIVE_FILE.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    with open(PROMOTIONS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    print(f"Live pipeline now runs {run}.")


def rollback() -> None:
    """Go back to the version that was live before the current one."""
    history = []
    if PROMOTIONS.exists():
        history = [json.loads(l) for l in PROMOTIONS.read_text().splitlines() if l.strip()]
    promotions = [h for h in history if not h.get("rollback")]
    current = load_live().get("run")
    positions = [i for i, h in enumerate(promotions) if h["run"] == current]
    if not positions or positions[-1] == 0:
        raise RuntimeError("There is no earlier promotion to roll back to.")

    previous = promotions[positions[-1] - 1]
    LIVE_FILE.write_text(json.dumps(previous, indent=2) + "\n", encoding="utf-8")
    with open(PROMOTIONS, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"rollback": True, "at": now(), "from": current, "to": previous["run"]})
            + "\n"
        )
    print(f"Rolled back: live pipeline runs {previous['run']} again.")


def report(test_set: Path) -> None:
    """Print every case against every run, then accuracy and flags per run."""
    cases = load_test_set(test_set)
    results = load_results(test_set)
    runs = list(dict.fromkeys(r["run"] for r in results))
    if not runs:
        print("No results yet.")
        return
    latest = {(r["run"], r["case_id"]): r for r in results}
    width = max(len(run) for run in runs) + 2
    live = live_run()

    def cell(result):
        if result is None:
            return "-"
        if result["error"]:
            return "error"
        p = result["predicted"]
        mark = "ok" if all(result["correct"].values()) else "XX"
        flagged = " f" if result.get("flags") else ""
        return f"{p['category'][:5]} u{p['urgency']} {mark}{flagged}"

    print(f"\n{'case':<5} {'expected':<12}" + "".join(f"{run:<{width}}" for run in runs))
    for case in cases:
        expected = f"{case['category'][:5]} u{case['urgency']}"
        if case.get("needs_review"):
            expected += " r"
        cells = "".join(f"{cell(latest.get((run, case['id']))):<{width}}" for run in runs)
        print(f"{case['id']:<5} {expected:<12}{cells}")

    print()
    for run in runs:
        scored = [latest[(run, c["id"])] for c in cases if (run, c["id"]) in latest]
        n = len(scored)
        parts = [f"{f} {sum(r['correct'][f] for r in scored)}/{n}" for f in SCORED_FIELDS]
        both = sum(all(r["correct"].values()) for r in scored)
        marker = "  <- live" if run == live else ""
        print(f"{run:<{width}} " + ", ".join(parts) + f", both right {both}/{n}{marker}")

    flagged_runs = []
    for run in runs:
        scored = [latest[(run, c["id"])] for c in cases if (run, c["id"]) in latest]
        stats = flag_stats(scored, cases)
        if stats:
            flagged_runs.append((run, stats))
    if flagged_runs:
        print("\nflags (f in the table; r marks cases a person should review):")
    for run, stats in flagged_runs:
        print(f"{run:<{width}} {describe_flags(stats)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=list(PROMPT_MODES))
    parser.add_argument(
        "--pool",
        type=Path,
        default=CORRECTIONS_FILE,
        help="Example pool for modes that use examples (default: data/corrections.jsonl)",
    )
    parser.add_argument("--test-set", type=Path, default=DEFAULT_TEST_SET)
    parser.add_argument("--dry-run", action="store_true", help="Print prompts only")
    parser.add_argument("--report", action="store_true", help="Compare saved runs")
    parser.add_argument(
        "--gate", metavar="MODE", choices=list(PROMPT_MODES),
        help="Decide whether MODE (with --pool) may replace the live version",
    )
    parser.add_argument(
        "--against", metavar="MODE", choices=list(PROMPT_MODES),
        help="Compare with this mode instead of the live version (no promotion)",
    )
    parser.add_argument(
        "--promote", action="store_true", help="With --gate: switch if it passes"
    )
    parser.add_argument("--rollback", action="store_true", help="Undo the last promotion")
    args = parser.parse_args()

    if args.rollback:
        rollback()
        return 0
    if args.mode:
        score(args.mode, args.pool, args.test_set, args.dry_run)
    if args.report or (args.mode and not args.dry_run):
        report(args.test_set)
    if args.gate:
        if args.promote and args.against:
            raise RuntimeError("--promote only works against the live version.")
        passed, run, score_text = gate(args.gate, args.pool, args.test_set, args.against)
        if passed and args.promote:
            promote(args.gate, args.pool, run, args.test_set, score_text)
        # Exit code 0 = promote, 1 = reject, so scripts can act on it.
        return 0 if passed else 1
    if not (args.mode or args.report):
        parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
