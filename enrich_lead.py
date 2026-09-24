"""
Inbound wholesale lead enrichment pipeline.

Takes a raw wholesale inquiry (free-text product request plus contact details),
uses Gemini to extract structured fields from the free text, then writes a
contact and an associated deal into HubSpot.

Run:
    python enrich_lead.py sample_submission.json
    python enrich_lead.py sample_submission.json --dry-run   # no API calls
"""

import argparse
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
HUBSPOT_BASE = "https://api.hubapi.com"

# Deal pipeline/stage the new deal lands in. "default" is the pipeline every
# HubSpot account starts with; replace the stage id with your own if you have
# renamed your stages (see README).
DEAL_PIPELINE = "default"
DEAL_STAGE = "appointmentscheduled"

CATEGORIES = ["Cosmetics", "Electronics", "General", "Fashion", "Home"]

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


def build_prompt(product_request: str) -> str:
    return PROMPT_TEMPLATE.format(
        product_request=product_request,
        categories="[" + ", ".join(CATEGORIES) + "]",
    )


def strip_code_fences(text: str) -> str:
    """Gemini often wraps JSON in ```json ... ``` despite being told not to.

    This was the single most common failure in the original no-code version of
    this pipeline, so it gets handled explicitly rather than hoped away.
    """
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    return cleaned.replace("```", "").strip()


def validate_enrichment(data: dict) -> dict:
    """Coerce the model's output into something safe to write to a CRM.

    An LLM will occasionally return urgency as "2" or "high", or invent a
    category outside the allowed list. Writing that straight into HubSpot gives
    you dirty data that is painful to clean up later, so it is normalised here.
    """
    summary = str(data.get("summary", "")).strip()

    raw_urgency = str(data.get("urgency", "")).strip()
    urgency_match = re.search(r"[123]", raw_urgency)
    urgency = urgency_match.group(0) if urgency_match else "2"

    category = str(data.get("category", "")).strip()
    match = next((c for c in CATEGORIES if c.lower() == category.lower()), None)
    category = match or "General"

    return {"summary": summary, "urgency": urgency, "category": category}


def enrich_with_gemini(product_request: str) -> dict:
    """Send the free-text request to Gemini and return validated fields."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set. See .env.example.")

    response = requests.post(
        GEMINI_URL,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        json={
            "contents": [
                {"parts": [{"text": build_prompt(product_request)}]}
            ],
            # temperature 0 keeps classifications stable across identical
            # submissions, which matters more here than variety.
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        },
        timeout=30,
    )
    response.raise_for_status()

    raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(strip_code_fences(raw_text))
    return validate_enrichment(parsed)


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
                "description": enrichment["summary"],
                DEAL_PROPERTIES["summary"]: enrichment["summary"],
                DEAL_PROPERTIES["category"]: enrichment["category"],
                DEAL_PROPERTIES["urgency"]: enrichment["urgency"],
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
    parser.add_argument("submission", help="Path to a submission JSON file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent to HubSpot without calling any API",
    )
    args = parser.parse_args()

    submission = load_submission(args.submission)
    print(f"Processing inquiry from {submission['company_name']}...\n")

    if args.dry_run:
        print("DRY RUN - no API calls made.\n")
        print("Prompt that would be sent to Gemini:")
        print("-" * 60)
        print(build_prompt(submission["product_request"]))
        print("-" * 60)
        return 0

    enrichment = enrich_with_gemini(submission["product_request"])
    print("Gemini returned:")
    print(json.dumps(enrichment, indent=2))
    print()

    contact_id = upsert_contact(submission, enrichment)
    print(f"Contact upserted: {contact_id}")

    deal_id = create_deal(submission, enrichment)
    print(f"Deal created:     {deal_id}")

    associate_deal_with_contact(deal_id, contact_id)
    print(f"Deal {deal_id} associated with contact {contact_id}")
    print("\nDone. Check your HubSpot deal board.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
