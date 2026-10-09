"""
Capture the corrections a reviewer made on the HubSpot deal board.

Flagged deals (and a random share of the rest) are sent to "Rep Review
Required". The reviewer fixes the categories or urgency if the model got them
wrong, then moves the deal on. This script finds the deals that were sent to
review and have since left it, compares what the model said (from
data/runs.jsonl) with what the reviewer kept (from HubSpot), and writes one
record per reviewed deal to data/corrections.jsonl. Deals that went straight to
outreach were never checked, so they are left out.

Run:
    python capture_corrections.py
"""

import json
import sys

import requests

from enrich_lead import (
    CORRECTIONS_FILE,
    DEAL_PROPERTIES,
    STAGE_REVIEW as REVIEW_STAGE,
    HUBSPOT_BASE,
    RUN_LOG,
    check,
    hubspot_headers,
)

# The two fields the reviewer judges. The summary is free text, so there is no
# single right answer to compare it against. "categories" is a list: an order
# can span several (see LABELLING.md).
SCORED_FIELDS = ["categories", "urgency"]


def output_categories(answer: dict) -> list:
    """The categories in a model answer or label, as a list.

    Records written before multi-category support hold a single "category".
    """
    if answer.get("categories"):
        return list(answer["categories"])
    return [answer["category"]] if answer.get("category") else []


def answers_match(field: str, ai, human) -> bool:
    """Whether two answers for one field agree.

    Categories are compared as a set: "Home, Electronics" and "Electronics,
    Home" describe the same order. HubSpot may also show ticked checkboxes in
    its own option order, so the order carries no meaning here.
    """
    if field == "categories":
        return set(ai or []) == set(human or [])
    return ai == human


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
            ticked = values.get(DEAL_PROPERTIES["categories"]) or ""
            deals[deal["id"]] = {
                "stage": values.get("dealstage"),
                # "Multiple checkboxes" come back as "Home;Electronics".
                "categories": [c.strip() for c in ticked.split(";") if c.strip()],
                "urgency": values.get(DEAL_PROPERTIES["urgency"]),
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
            {"summary": "...", "categories": ["Home", "Electronics"], "urgency": "2"}
            (older runs have a single "category" instead)
    final - the deal as it is in HubSpot now, after review, e.g.
            {"stage": "decisionmakerboughtin", "categories": ["Home"],
             "urgency": "2"}

    A record is returned for every reviewed deal, including those where the
    model was right: they are needed to measure accuracy, and they make good
    examples of correct answers. Deals with an empty field never reach this
    function; main() sends them back for review instead.
    """
    ai = {"categories": output_categories(run["output"]), "urgency": run["output"]["urgency"]}
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
        "corrected_fields": [
            f for f in SCORED_FIELDS if not answers_match(f, ai[f], human[f])
        ],
    }


def main() -> int:
    # Runs logged before routing existed have no "sent_to_review" field; back
    # then every deal went to review, so they count as sent.
    runs = [run for run in load_runs() if run.get("sent_to_review", True)]
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
        print(f"  {field:<10} model was right on {right}/{len(corrections)}")
    print(f"\nWritten to {CORRECTIONS_FILE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
