"""
Inbound wholesale lead enrichment pipeline.

Takes a raw wholesale inquiry (free-text product request plus contact details),
uses Gemini to extract structured fields from the free text, then writes a
contact and an associated deal into HubSpot.

Every run is appended to data/runs.jsonl (input, prompt version, raw model
output, validated fields, flags) so later corrections can be compared against
exactly what the model said.

Run:
    python enrich_lead.py sample_submission.json
    python enrich_lead.py submissions/*.json                  # several at once
    python enrich_lead.py sample_submission.json --dry-run   # no API calls
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from examples import ALL_FIELDS, find_similar, format_examples, load_pool

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
HUBSPOT_BASE = "https://api.hubapi.com"

# The Gemini free tier allows only a few requests per minute. When it says
# "too many requests" (429), wait and try again rather than failing the
# submission.
GEMINI_MAX_ATTEMPTS = 5
DEFAULT_RETRY_SECONDS = 30
# A per-minute limit asks for a wait of under a minute. A much longer wait
# means the daily quota is used up, and sleeping for hours helps nobody.
MAX_RETRY_WAIT_SECONDS = 120

# Deal pipeline/stage the new deal lands in. Renamed stages keep their original
# internal ids, so "presentationscheduled" is what this account calls
# "Stage 2: Rep Review Required". Every AI-enriched deal goes there so a person
# checks it; those checks are the labels the evaluation is built on.
DEAL_PIPELINE = "default"
DEAL_STAGE = "presentationscheduled"

# Prompt versions, recorded on every run and deal so accuracy can be compared
# per version (see prompt_version()). Add a new one whenever the prompt changes.
#   v1  original prompt
#   v2  v1 + similar past reviewed requests as examples (see examples.py)
#   v3  v1 + the date the request came in + the written urgency rules
#   v4  v3 + similar past reviewed requests, showing urgency only

RUN_LOG = Path(__file__).resolve().parent / "data" / "runs.jsonl"
# Every deal a rep has reviewed; the example pool for v2 and v4.
CORRECTIONS_FILE = RUN_LOG.parent / "corrections.jsonl"

# What each prompt version is made of. evaluate.py scores these modes; the live
# pipeline runs LIVE_MODE.
PROMPT_MODES = {
    # mode:          (tell the date?, use examples?, fields shown in examples)
    "baseline":      (False, False, None),           # v1
    "fewshot":       (False, True, ALL_FIELDS),      # v2
    "rules":         (True, False, None),            # v3
    "rules-fewshot": (True, True, ("urgency",)),     # v4
}

# The version the live pipeline uses. Only change this when
# `python evaluate.py --gate <candidate>` says PROMOTE.
# 2026-10-06: promoted by the gate, v1 6/10 -> v4 9/10
LIVE_MODE = "rules-fewshot"

CATEGORIES = ["Cosmetics", "Electronics", "General", "Fashion", "Home"]

# Requests shorter than this rarely carry enough detail to classify reliably.
SHORT_REQUEST_WORDS = 6

# HubSpot internal property names for the three enriched fields.
#
# Contacts and deals use different internal names here because the properties
# were created separately as the CRM grew, which is normal in a real account.
# Keeping the mapping in one place means pointing this script at a different
# HubSpot portal is a config change rather than a code change.
CONTACT_PROPERTIES = {
    "summary": "ai_research_summary",
    "category": "product_category_fit",
    "urgency": "urgency_tier",
}

DEAL_PROPERTIES = {
    "summary": "order_summary",
    "category": "product_category",
    "urgency": "urgency_tier",
    "flags": "ai_flags",
    "prompt_version": "ai_prompt_version",
}

PROMPT_TEMPLATE = """You are a Sales Operations Assistant for a wholesale distributor.
Your job is to analyse inbound wholesale buyer requests and extract specific data.

Here is the request from the buyer:
{product_request}

Extract the following and return it strictly as a clean JSON object with no
additional text, markdown, or formatting:
{{"summary": "Write a 1-sentence summary of the products requested",
  "urgency": "Assign an urgency tier of exactly 1, 2, or 3 (1 = no rush, 2 = standard, 3 = high/ASAP)",
  "category": "Pick the single best fit from this exact list: {categories}"}}"""


# v3: the model cannot know what day it is, so "by mid-November" means nothing
# to it unless it is told when the request came in. The thresholds are the
# ones in LABELLING.md, written before the test set existed.
PROMPT_TEMPLATE_V3 = """You are a Sales Operations Assistant for a wholesale distributor.
Your job is to analyse inbound wholesale buyer requests and extract specific data.

The request was received on {received}. Urgency is how soon the buyer needs
the goods, counted from that date:
- 3: needed within a week (e.g. "ASAP", "by Friday", "reopens in 3 days")
- 2: needed in one week to one month (e.g. "within two weeks", "sometime this
  month"), or no deadline mentioned
- 1: needed more than a month away (e.g. "next quarter", "for next season")
If the request gives conflicting signals, choose the higher urgency.

Here is the request from the buyer:
{product_request}

Extract the following and return it strictly as a clean JSON object with no
additional text, markdown, or formatting:
{{"summary": "Write a 1-sentence summary of the products requested",
  "urgency": "Assign an urgency tier of exactly 1, 2, or 3 following the rules above",
  "category": "Pick the single best fit from this exact list: {categories}"}}"""

REQUEST_HEADING = "Here is the request from the buyer:"


def format_received(day: date) -> str:
    """The date a request came in, as the prompt states it: "Monday 5 October 2026".

    In production this is the form submission's timestamp; in the test set it
    is stored with each case, so evaluations give the same result whenever
    they are run.
    """
    return f"{day:%A} {day.day} {day:%B %Y}"


def prompt_version(examples_block: str = "", received: str = None) -> str:
    if received:
        return "v4" if examples_block else "v3"
    return "v2" if examples_block else "v1"


def build_prompt(
    product_request: str, examples_block: str = "", received: str = None
) -> str:
    """Fill in the prompt template, optionally with a date and past examples.

    received (e.g. "Monday 5 October 2026") switches to the v3 template with
    the urgency rules. Examples go just before the buyer's request. With
    neither, the prompt is exactly v1, so v1 results stay comparable.
    """
    template = PROMPT_TEMPLATE_V3 if received else PROMPT_TEMPLATE
    prompt = template.format(
        product_request=product_request,
        categories="[" + ", ".join(CATEGORIES) + "]",
        received=received,
    )
    if examples_block:
        prompt = prompt.replace(
            REQUEST_HEADING, f"{examples_block}\n\n{REQUEST_HEADING}", 1
        )
    return prompt


def strip_code_fences(text: str) -> str:
    """Gemini often wraps JSON in ```json ... ``` despite being told not to.

    This was the single most common failure in the original no-code version of
    this pipeline, so it gets handled explicitly rather than hoped away.
    """
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    return cleaned.replace("```", "").strip()


def validate_enrichment(data: dict, product_request: str = "") -> dict:
    """Coerce the model's output into something safe to write to a CRM.

    An LLM will occasionally return urgency as "2" or "high", or invent a
    category outside the allowed list. Writing that straight into HubSpot gives
    you dirty data that is painful to clean up later, so it is normalised here.

    Every time a value has to be repaired or looks doubtful, a flag is recorded.
    A silent fallback would make a wrong answer look like a confident one, and
    the reviewer (and the evaluation) would never know.
    """
    flags = []

    summary = str(data.get("summary", "")).strip()
    if not summary:
        flags.append("empty_summary")

    raw_urgency = str(data.get("urgency", "")).strip()
    urgency_match = re.search(r"[123]", raw_urgency)
    if urgency_match:
        urgency = urgency_match.group(0)
        if raw_urgency != urgency:
            flags.append(f"urgency_coerced:{raw_urgency}")
    else:
        urgency = "2"
        flags.append(f"urgency_fallback:{raw_urgency or 'missing'}")

    raw_category = str(data.get("category", "")).strip()
    match = next(
        (c for c in CATEGORIES if c.lower() == raw_category.lower()), None
    )
    if match is None:
        category = "General"
        flags.append(f"category_fallback:{raw_category or 'missing'}")
    else:
        category = match
        if category == "General":
            # The model choosing the catch-all usually means it was unsure.
            flags.append("general_category")

    if len(product_request.split()) < SHORT_REQUEST_WORDS:
        flags.append("short_request")

    return {
        "summary": summary,
        "urgency": urgency,
        "category": category,
        "flags": flags,
    }


class QuotaExhausted(RuntimeError):
    """Gemini's daily free quota is used up. Retrying today will not help."""


def retry_delay_seconds(response: requests.Response) -> int:
    """How long Gemini asks us to wait after a 429, e.g. "37s" -> 37.

    The error body usually carries a RetryInfo entry with the exact delay.
    If it is missing or unreadable, fall back to a safe default.
    """
    try:
        for detail in response.json()["error"]["details"]:
            if "retryDelay" in detail:
                return int(float(detail["retryDelay"].rstrip("s"))) + 1
    except (ValueError, KeyError, TypeError):
        pass
    return DEFAULT_RETRY_SECONDS


def enrich_with_gemini(
    product_request: str, examples_block: str = "", received: str = None
) -> dict:
    """Send the free-text request to Gemini and return validated fields.

    The raw model text is kept alongside the validated fields so the run log
    shows what the model actually said, not just what survived validation.
    examples_block and received select the prompt version (see build_prompt).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. See .env.example.")

    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        response = requests.post(
            GEMINI_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
            json={
                "contents": [
                    {
                        "parts": [
                            {"text": build_prompt(product_request, examples_block, received)}
                        ]
                    }
                ],
                # temperature 0 keeps classifications stable across identical
                # submissions, which matters more here than variety.
                # gemini-2.5-flash "thinks" before answering and that thinking
                # counts towards maxOutputTokens; at 1024 a long request could
                # cut the JSON off halfway, so leave plenty of room.
                "generationConfig": {"temperature": 0, "maxOutputTokens": 4096},
            },
            timeout=30,
        )
        if response.status_code != 429 or attempt == GEMINI_MAX_ATTEMPTS:
            break
        wait = retry_delay_seconds(response)
        if wait > MAX_RETRY_WAIT_SECONDS:
            raise QuotaExhausted(
                f"Gemini daily quota used up; it resets in about "
                f"{wait / 3600:.1f} hours."
            )
        print(f"  Gemini rate limit hit, waiting {wait}s (attempt {attempt})...")
        time.sleep(wait)
    response.raise_for_status()

    candidate = response.json()["candidates"][0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise RuntimeError("Gemini's reply was cut off by the output token limit.")
    raw_text = candidate["content"]["parts"][0]["text"]
    try:
        parsed = json.loads(strip_code_fences(raw_text))
    except ValueError:
        # Show what came back; a bare JSON error hides the actual cause.
        raise RuntimeError(f"Gemini did not return valid JSON: {raw_text[:300]!r}")
    enrichment = validate_enrichment(parsed, product_request)
    enrichment["raw_output"] = raw_text
    enrichment["prompt_version"] = prompt_version(examples_block, received)
    return enrichment


def hubspot_headers() -> dict:
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set. See .env.example.")
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def check(response: requests.Response, action: str) -> requests.Response:
    """Raise with HubSpot's actual explanation rather than a bare status code.

    HubSpot puts a useful message in the response body (which scope is missing,
    which property does not exist, which dropdown value was rejected).
    response.raise_for_status() discards it, which makes debugging much slower
    than it needs to be.
    """
    if response.ok:
        return response

    try:
        detail = response.json().get("message", response.text)
    except ValueError:
        detail = response.text

    hint = ""
    if response.status_code == 403:
        hint = "\nHint: the Service Key is missing a scope for this call."
    elif response.status_code == 400:
        hint = (
            "\nHint: usually a property that does not exist, or a dropdown "
            "value that is not one of the defined options."
        )

    raise RuntimeError(
        f"HubSpot {action} failed ({response.status_code}): {detail}{hint}"
    )


def find_contact_by_email(email: str):
    """Return an existing contact id for this email, or None."""
    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=hubspot_headers(),
        json={
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "limit": 1,
        },
        timeout=30,
    )
    check(response, "contact search")
    results = response.json().get("results", [])
    return results[0]["id"] if results else None


def upsert_contact(submission: dict, enrichment: dict) -> str:
    """Create the contact, or update it if that email already exists.

    Inbound forms get filled in twice all the time, so creating blindly would
    leave duplicate contacts for the same buyer.
    """
    properties = {
        "email": submission["company_email"],
        "firstname": submission["first_name"],
        "lastname": submission["last_name"],
        "company": submission["company_name"],
        CONTACT_PROPERTIES["summary"]: enrichment["summary"],
        CONTACT_PROPERTIES["category"]: enrichment["category"],
        CONTACT_PROPERTIES["urgency"]: enrichment["urgency"],
    }

    existing_id = find_contact_by_email(submission["company_email"])

    if existing_id:
        response = requests.patch(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{existing_id}",
            headers=hubspot_headers(),
            json={"properties": properties},
            timeout=30,
        )
        check(response, "contact update")
        return existing_id

    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=hubspot_headers(),
        json={"properties": properties},
        timeout=30,
    )
    check(response, "contact create")
    return response.json()["id"]


def create_deal(submission: dict, enrichment: dict) -> str:
    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/deals",
        headers=hubspot_headers(),
        json={
            "properties": {
                "dealname": f"{submission['company_name']} - Wholesale Order",
                "pipeline": DEAL_PIPELINE,
                "dealstage": DEAL_STAGE,
                # The buyer's own words, so the reviewer can judge the AI fields
                # against the source. The AI summary lives in order_summary.
                "description": submission["product_request"],
                DEAL_PROPERTIES["summary"]: enrichment["summary"],
                DEAL_PROPERTIES["category"]: enrichment["category"],
                DEAL_PROPERTIES["urgency"]: enrichment["urgency"],
                DEAL_PROPERTIES["flags"]: ", ".join(enrichment["flags"]),
                DEAL_PROPERTIES["prompt_version"]: enrichment["prompt_version"],
            }
        },
        timeout=30,
    )
    check(response, "deal create")
    return response.json()["id"]


def associate_deal_with_contact(deal_id: str, contact_id: str) -> None:
    response = requests.put(
        f"{HUBSPOT_BASE}/crm/v4/objects/deals/{deal_id}"
        f"/associations/default/contacts/{contact_id}",
        headers=hubspot_headers(),
        timeout=30,
    )
    check(response, "deal-contact association")


def log_run(
    submission: dict,
    enrichment: dict,
    deal_id: str,
    contact_id: str,
    received: date,
    examples: list,
):
    """Append one line per processed submission to the run log.

    The deal id is the join key: when a reviewer later edits the deal in
    HubSpot, this line is the record of what the model originally said.
    Contact names and emails are left out; they are not needed to learn from.
    """
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "received": received.isoformat(),
        "deal_id": deal_id,
        "contact_id": contact_id,
        "model": GEMINI_MODEL,
        "prompt_version": enrichment["prompt_version"],
        # Which past reviews were shown as examples, to trace a bad answer
        # back to the example that caused it.
        "examples": [e["deal_id"] for e in examples],
        "company_name": submission["company_name"],
        "product_request": submission["product_request"],
        "raw_output": enrichment["raw_output"],
        "output": {
            "summary": enrichment["summary"],
            "urgency": enrichment["urgency"],
            "category": enrichment["category"],
        },
        "flags": enrichment["flags"],
    }
    RUN_LOG.parent.mkdir(exist_ok=True)
    with open(RUN_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def live_prompt_inputs(product_request: str, received: date) -> tuple:
    """The examples and date the live prompt gets, according to LIVE_MODE.

    The example pool is re-read on every call, so a correction captured by
    capture_corrections.py is used from the very next submission on.
    """
    tell_date, use_examples, example_fields = PROMPT_MODES[LIVE_MODE]
    examples = []
    if use_examples and CORRECTIONS_FILE.exists():
        examples = find_similar(product_request, load_pool(CORRECTIONS_FILE))
    block = format_examples(examples, example_fields) if examples else ""
    received_text = format_received(received) if tell_date else None
    return examples, block, received_text


def process_submission(submission: dict, received: date = None) -> dict:
    """Enrich one submission, write it to HubSpot and log the run.

    received is the day the request came in (the form's timestamp); it
    defaults to today. Shared by both entry points so the form path and the
    file path cannot drift apart.
    """
    received = received or date.today()
    examples, block, received_text = live_prompt_inputs(
        submission["product_request"], received
    )
    enrichment = enrich_with_gemini(submission["product_request"], block, received_text)
    contact_id = upsert_contact(submission, enrichment)
    deal_id = create_deal(submission, enrichment)
    associate_deal_with_contact(deal_id, contact_id)
    log_run(submission, enrichment, deal_id, contact_id, received, examples)
    return {"enrichment": enrichment, "deal_id": deal_id, "contact_id": contact_id}


def load_submission(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        submission = json.load(handle)

    required = [
        "first_name",
        "last_name",
        "company_name",
        "company_email",
        "product_request",
    ]
    missing = [field for field in required if not submission.get(field)]
    if missing:
        raise ValueError(f"Submission is missing required fields: {missing}")
    return submission


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "submissions", nargs="+", help="Path(s) to submission JSON files"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent to HubSpot without calling any API",
    )
    args = parser.parse_args()

    failures = 0
    for path in args.submissions:
        submission = load_submission(path)
        # A file may say when the request came in ("received": "2026-10-05");
        # otherwise it is treated as arriving today.
        received = (
            date.fromisoformat(submission["received"])
            if submission.get("received")
            else date.today()
        )
        print(f"Processing inquiry from {submission['company_name']} ({path})...")

        if args.dry_run:
            _, block, received_text = live_prompt_inputs(
                submission["product_request"], received
            )
            print(f"DRY RUN - no API calls made. Live mode: {LIVE_MODE}\n")
            print("Prompt that would be sent to Gemini:")
            print("-" * 60)
            print(build_prompt(submission["product_request"], block, received_text))
            print("-" * 60 + "\n")
            continue

        try:
            result = process_submission(submission, received)
        except Exception as error:
            # Same rule as the sheet path: one bad submission does not stop
            # the batch.
            print(f"  failed: {error}\n")
            failures += 1
            continue

        enrichment = result["enrichment"]
        flags = ", ".join(enrichment["flags"]) or "none"
        print(
            f"  {enrichment['category']} / urgency {enrichment['urgency']} "
            f"-> deal {result['deal_id']} (flags: {flags})\n"
        )

    if not args.dry_run:
        print(f"Done. Runs logged to {RUN_LOG}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
