"""
Capture the corrections a reviewer made on the HubSpot deal board.

Every enriched deal lands in "Rep Review Required". The reviewer fixes the
category or urgency if the model got them wrong, then moves the deal on. This
script finds the deals that have left the review stage, compares what the model
said (from data/runs.jsonl) with what the reviewer kept (from HubSpot), and
writes one record per reviewed deal to data/corrections.jsonl.

Run:
    python capture_corrections.py
"""

import json
import sys

import requests

from enrich_lead import (
    CORRECTIONS_FILE,
    DEAL_PROPERTIES,
    DEAL_STAGE as REVIEW_STAGE,
    HUBSPOT_BASE,
    RUN_LOG,
    check,
    hubspot_headers,
)

# The two fields the reviewer judges. The summary is free text, so there is no
# single right answer to compare it against.
SCORED_FIELDS = ["category", "urgency"]


def load_runs() -> list:
    with open(RUN_LOG, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def fetch_deals(deal_ids: list) -> dict:
    """Current stage and field values for each deal, keyed by deal id.

    Uses HubSpot's batch endpoint: one call for up to 100 deals instead of one
    call per deal. Deals that have been deleted are simply missing.
    """
    properties = ["dealstage"] + [DEAL_PROPERTIES[f] for f in SCORED_FIELDS]
    deals = {}
    for start in range(0, len(deal_ids), 100):
        response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/deals/batch/read",
            headers=hubspot_headers(),
            json={
                "properties": properties,
                "inputs": [{"id": i} for i in deal_ids[start : start + 100]],
            },
            timeout=30,
        )
        check(response, "deal batch read")
        for deal in response.json().get("results", []):
            values = deal["properties"]
            deals[deal["id"]] = {
                "stage": values.get("dealstage"),
                **{f: values.get(DEAL_PROPERTIES[f]) for f in SCORED_FIELDS},
            }
    return deals


def send_back_for_review(deal_id: str) -> None:
    """Move a deal back to the review stage so a person looks at it again."""
    response = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}",
        headers=hubspot_headers(),
        json={"properties": {"dealstage": REVIEW_STAGE}},
        timeout=30,
    )
    check(response, "deal stage update")


def build_correction(run: dict, final: dict) -> dict:
    """Compare the model's answer with what the reviewer kept.

    run   - one line from data/runs.jsonl. The model's answer is in
            run["output"], e.g.
            {"summary": "...", "category": "Electronics", "urgency": "2"}
    final - the deal as it is in HubSpot now, after review, e.g.
            {"stage": "decisionmakerboughtin", "category": "Cosmetics",
             "urgency": "2"}

    A record is returned for every reviewed deal, including those where the
    model was right: they are needed to measure accuracy, and they make good
    examples of correct answers. Deals with an empty field never reach this
    function; main() sends them back for review instead.
    """
    ai = {field: run["output"][field] for field in SCORED_FIELDS}
    human = {field: final[field] for field in SCORED_FIELDS}
    return {
        "deal_id": run["deal_id"],
        "company_name": run["company_name"],
        "product_request": run["product_request"],
        # Kept so results can later be split by model and prompt version, and
        # so we can check whether flagged deals really are corrected more often.
        "model": run["model"],
        "prompt_version": run["prompt_version"],
        "flags": run["flags"],
        "ai": ai,
        "human": human,
        "corrected_fields": [f for f in SCORED_FIELDS if ai[f] != human[f]],
    }


def main() -> int:
    runs = load_runs()
    deals = fetch_deals([run["deal_id"] for run in runs])

    corrections, waiting, sent_back = [], 0, 0
    for run in runs:
        final = deals.get(run["deal_id"])
        if final is None:
            continue  # deal was deleted in HubSpot
        if final["stage"] == REVIEW_STAGE:
            waiting += 1
            continue  # not reviewed yet

        # An empty field is not a real answer (most likely cleared by
        # accident), so it would be a misleading label. Ask for a re-review.
        empty = [f for f in SCORED_FIELDS if not final[f]]
        if empty:
            send_back_for_review(run["deal_id"])
            print(
                f"  {run['company_name']}: {', '.join(empty)} empty, "
                "moved back to review"
            )
            sent_back += 1
            continue

        corrections.append(build_correction(run, final))

    # Rebuilt from scratch on every run rather than appended to: HubSpot holds
    # the reviewer's latest decision, and they may change their mind.
    with open(CORRECTIONS_FILE, "w", encoding="utf-8") as handle:
        for record in corrections:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(
        f"{len(corrections)} reviewed deal(s), {waiting} waiting for review, "
        f"{sent_back} sent back for re-review."
    )
    for field in SCORED_FIELDS:
        right = sum(field not in c["corrected_fields"] for c in corrections)
        print(f"  {field:<9} model was right on {right}/{len(corrections)}")
    print(f"\nWritten to {CORRECTIONS_FILE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
