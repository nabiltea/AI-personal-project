# Inbound Lead Triage Pipeline

Wholesale buyers describe what they want in their own words: "We're kitting out
a staff flat, 10 bedsheet sets, 10 towels and 4 smartphones, within two weeks."
Someone then has to read every one of these, work out which product lines it
touches, decide how urgent it is and type it into the CRM. This project does
that step. It reads each request with Gemini, splits the order into products,
assigns every product a category, works out the urgency from the buyer's
deadline, and creates the contact and deal in HubSpot.

What makes it more than a form-to-CRM script is what happens next. The model
is told to say when it isn't sure, and only those deals go to a sales rep for
review. The rep's corrections are captured automatically and used to improve
the prompt. Every change, whether a new prompt version or a new batch of
corrections, has to prove itself on a hand-labelled test set before it reaches
live deals, and any change can be rolled back.

On the most recent test set of 18 deliberately awkward requests, the current
version got every category right, 16 of 18 requests fully right, and flagged
both of its mistakes for review, while sending 39% of deals to review instead
of all of them.

## What it does

```
Google Form ──▶ responses sheet ──▶ Gemini ──▶ checks and flags ──▶ HubSpot deal
                                                                    Stage 1: New Inbound Request
                                                                         │
                                              ┌──────────────────────────┴──────────────────────┐
                                  flagged, or picked for a spot-check                   everything else
                                              │                                                 │
                                  Stage 2: Rep Review Required                   Stage 3: Active Outreach
```

For each new form response, the pipeline:

1. sends the buyer's text to Gemini along with the date the request came in,
   the urgency rules and a few similar past reviews as examples
2. gets back a one-line summary, an urgency tier from 1 to 3, and a list of the
   products in the order, each with a category and a note on whether it could
   reasonably belong to another one
3. checks the answer, repairs anything malformed and records a flag for
   anything doubtful
4. creates or updates the contact, creates the deal in Stage 1 and links the two
5. moves the deal to review if it has a flag (plus a random 20% of the rest as
   spot-checks), or straight to outreach if not
6. logs the whole run, including the model's exact reply

An order can span several categories. The deal's Product Category field is a
multiple-checkbox property, so "10 bedsheet sets, 10 towels and 4 smartphones"
becomes Home and Electronics. The buyer's original text goes into the deal
description, so the rep can always see what was actually asked for.

### What sends a deal to review

| Flag | Example | Why |
| --- | --- | --- |
| Ambiguous product | `ambiguous:air fryers (Electronics or Home)` | The product could reasonably belong to two categories |
| Vague timing | `ambiguous_urgency:2 or 1` | "For a corporate event next month" could fit two urgency tiers |
| Repaired answer | `category_fallback:drones: Gadgets` | The model's answer had to be fixed before it could go into the CRM |
| Catch-all or thin request | `general_category`, `short_request` | Usually means the model had little to go on |
| Spot-check | `spot_check` | Picked at random so mistakes the flags miss still get found |

The flags name the product or the two tiers in question, so the rep knows
exactly what to decide instead of rereading the whole order.

## How it gets better over time

```
reps correct deals in HubSpot
        │
        ▼
capture_corrections.py ──▶ candidate example pool (data/corrections.jsonl)
                                     │
                                     ▼
              evaluate.py scores it on a hand-labelled test set
                                     │
                                     ▼
          the gate compares it with the live version ──▶ promote or reject
                                     │
                                     ▼
         live version + frozen copy of the pool it was tested with
```

When a rep fixes a category or an urgency and moves the deal on, nothing extra
is asked of them. The edit itself is the feedback. `capture_corrections.py`
compares what the model originally said (from the run log) with what the rep
kept (from HubSpot) and records both, whether the model was right or not. If a
field was left empty by accident, the deal goes back to review instead of being
counted as a correction.

Those records become the examples shown to the model for similar requests, but
not straight away. New corrections collect in a candidate pool, and they only
reach live deals once an evaluation run with them has passed the gate. A
careless batch of reviews can't quietly make the system worse. The live
version and its frozen pool are recorded in `data/live.json`, every promotion
is logged in `data/promotions.jsonl`, and `--rollback` undoes the last one.
At any point you can say which prompt and which corrections were live, and why.

The gate's rule is simple: a candidate is promoted if it gets at least as many
test cases fully right as the live version. On a tie it also has to be at
least as good at flagging the cases that should be reviewed.

## How I tested it

The rules for what counts as the right answer are written down in
[LABELLING.md](LABELLING.md): the urgency thresholds, what to do with mixed
orders, which products are known to be borderline, and when a case should be
reviewed. I wrote them before reviewing anything, and every label in the
project follows them.

I first pushed 20 deliberately messy submissions through the pipeline (typos,
Dutch, vague deadlines, a two-word request, a prompt-injection attempt) and
reviewed the resulting deals myself. That gave 19 reviewed deals to learn
from. The model had the category right on all 19 but the urgency right on only
15, and every urgency mistake went the same way: one level too urgent.

I then labelled two test sets by hand and kept them away from the examples the
model sees:

- Test set 1, 10 single-category requests, used to compare v1 to v5
- Test set 2, 18 harder requests with mixed orders and ambiguous products,
  each also labelled with whether a person should review it

### Results

Test set 1:

| Version | What changed | Categories | Urgency | Fully right |
| --- | --- | --- | --- | --- |
| v1 | the original prompt | 9/10 | 7/10 | 6/10 |
| v2 | plus the 3 most similar past reviews as examples | 8/10 | 6/10 | 5/10 |
| v3 | v1 plus the date and the written urgency rules | 8/10 | 10/10 | 8/10 |
| v4 | v3 plus past reviews, showing only their urgency | 9/10 | 10/10 | 9/10 |
| v5 | v4 plus "could this be another category?" | 10/10 | 10/10 | 10/10 |

Test set 2:

| Version | What changed | Categories | Urgency | Fully right | Sent to review | Mistakes flagged |
| --- | --- | --- | --- | --- | --- | --- |
| v6 | orders split into products, each with its own category and ambiguity check | 12/13 | 12/13 | 11/13 | 77% | 1 of 2 |
| v7 | General only as a genuine doubt, "no rush" means urgency 1, a check for vague timing | 18/18 | 16/18 | 16/18 | 39% | 2 of 2 |

v6 was only scored on 13 of the 18 cases. The free Gemini tier allows 20
requests a day, and an outage on Google's side took out part of that day's run.

### What the results taught me

The biggest gain came from information the model was missing. Most early
urgency mistakes were about dates. The prompt never said what day it was, so
"by mid-November" or "for Black Friday" couldn't be turned into a number of
days. Adding the date and the urgency rules (v3) fixed every urgency mistake on
test set 1. The learning loop only helped once that was in place.

Good feedback can still teach the wrong lesson. In v2, a request for 25 yoga
blocks was shown a past order of yoga mats and leggings, which was Fashion
because there were more leggings than mats. The model took away "yoga means
Fashion" and got it wrong. The correction was right, it was just retrieved for
the wrong reason. Since v4 the examples only show urgency, so a category can't
be copied from a loosely related request. The gate rejected v2, which is
exactly what it's there for.

A catch-all category creates noise. v6 suggested "General" as a possible
second category for most products, because technically anything could be
General. That sent 77% of deals to review. v7 asks for General only when the
model genuinely can't place a product, and review load dropped to 39%.

Flagging is a trade-off between workload and caution. v6 caught every case I
wanted reviewed but buried them in false alarms. v7 caught both of its own
mistakes and 5 of the 8 cases I'd marked for review, at half the workload. The
three it missed (towels twice, and candles with reed diffusers) were all put in
the right category, so nothing went wrong, but the doubt wasn't raised. Where
exactly to draw that line is a business decision, which is why
[LABELLING.md](LABELLING.md) keeps a list of known borderline products.

The same request doesn't always get the same answer. Even at temperature 0,
`gemini-2.5-flash` classified one mixed order as Fashion on one run and
Cosmetics on the next. Both runs flagged it, so it reached review either way,
but it's a reminder that one good run doesn't prove a version is reliable.

## Why I think it's useful

A distributor using this gets triaged, structured deals instead of a pile of
emails, and their reps only need to look closely at the deals the system isn't
sure about. On the latest test set that was fewer than half, and every mistake
was among them. The reps don't have to fill in anything extra: correcting the
CRM, which they'd do anyway, is what teaches the system.

It also doesn't depend on the business having a perfect product catalogue.
New or unusual products get flagged as ambiguous rather than guessed silently,
which suits smaller companies whose range changes all the time. A company that
does have a detailed catalogue can still benefit, because some products
genuinely belong to more than one category however carefully the categories
are defined.

Most importantly, it's built so that improving it can't quietly make it worse.
Every change is measured on the same hand-labelled cases, compared with what's
live, and kept or rejected on that basis, with a record of every decision.

## Decisions along the way

The prompt asks for a fixed JSON shape, categories from a fixed list and an
urgency tier with each level defined. Free-form answers would make the CRM
fields impossible to filter on. Gemini often wraps its JSON in markdown fences
even when told not to, so those are stripped before parsing. If it returns
something that can be repaired, like an urgency of "high" or a category that
isn't on the list, the value falls back to a safe default and a flag records
it. My first version repaired these silently, which made a bad answer look just
as confident as a good one.

Each version asks the model concrete questions ("could this product also be
another category?", "could this timing fit another tier?") rather than asking
how confident it is. Models are poor at putting a number on their own
confidence, but answer the concrete version well, and the answer tells the rep
what to check.

Every deal is created in Stage 1 and then moved on, rather than created
straight in Stage 2 or 3. HubSpot keeps each deal's stage history, so it's
always visible that a deal went through intake first. The spot-check rate
starts at 20%. While there's little evidence that unflagged deals are reliable,
it's worth checking more of them. The rate is one setting and can come down
once spot-checks keep coming back clean.

Contacts are matched by email, because people submit the form twice. Sheet
columns are found by their header text, so reordering the form's questions
doesn't shift every field along by one. A row that fails isn't marked as
processed, so the next run picks it up again. Temperature is 0 to keep answers
as stable as possible, although it reduces variation rather than removing it.

Similar past reviews are found with TF-IDF over the request text, without a
stop-word list: scikit-learn's English stop words include "within", "next", "by"
and "no", which are exactly the words that describe a deadline. I also tried
ignoring numbers, since "25 yoga blocks" was being matched with "25 hair
dryers" purely because of the 25. The only way to judge that change was to look
at which examples the test cases got, and tuning a system until its test cases
look good makes the test meaningless, so I left it alone.

The evaluation is built to be repeatable. Each test case stores the date it was
labelled against, because "before the 20th" is urgency 2 on 5 October and
urgency 3 a week later. Example pools are named by a fingerprint of their
contents, so a run against yesterday's corrections can't be mixed up with one
against today's. Every new version changes as little as possible, and the
earlier versions' prompts are checked to stay identical, so a change in score
can be traced to the exact sentence that caused it.

The free Gemini tier allows 20 requests a day per model, which shaped a lot of
this. The pipeline waits and retries when it hits the per-minute limit or when
Google's servers are briefly unavailable, and stops straight away when the
daily limit runs out. The evaluation saves each answer the moment it arrives,
skips cases it has already scored, and doesn't count an outage on Google's side
as a wrong answer.

## Running it

You'll need Python 3.10 or newer.

```bash
git clone https://github.com/nabiltea/AI-personal-project.git
cd AI-personal-project
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your keys
```

`.env` needs:

- `GEMINI_API_KEY` from [Google AI Studio](https://aistudio.google.com/apikey)
- `HUBSPOT_ACCESS_TOKEN`, a HubSpot service key or private app token with read
  and write access to contacts and deals (`crm.objects.contacts.read`,
  `crm.objects.contacts.write`, `crm.objects.deals.read`,
  `crm.objects.deals.write`)
- `SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` and `SHEET_NAME`, if you use
  the Google Sheets path

### HubSpot

Create these custom properties under Settings → Properties. Contacts and deals
use different internal names because I created them at different times; both
mappings are at the top of `enrich_lead.py`.

Contact properties:

| Internal name          | Type                |
| ---------------------- | ------------------- |
| `ai_research_summary`  | Multi-line text     |
| `product_category_fit` | Multiple checkboxes |
| `urgency_tier`         | Dropdown select     |

Deal properties:

| Internal name       | Type                |
| ------------------- | ------------------- |
| `order_summary`     | Multi-line text     |
| `product_category`  | Multiple checkboxes |
| `urgency_tier`      | Dropdown select     |
| `ai_flags`          | Single-line text    |
| `ai_prompt_version` | Single-line text    |

The category properties need the options listed in `CATEGORIES` (Cosmetics,
Electronics, General, Fashion, Home), and the urgency dropdowns need the
internal values `1`, `2` and `3`.

The pipeline uses three stages of the default deal pipeline, set at the top of
`enrich_lead.py` as `STAGE_NEW`, `STAGE_REVIEW` and `STAGE_OUTREACH`. HubSpot
keeps a stage's original internal id when you rename it (my "Rep Review
Required" stage is `presentationscheduled`), so look yours up with:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://api.hubapi.com/crm/v3/pipelines/deals
```

### Google Sheets

`process_sheet.py` needs a Google service account with the Sheets API enabled.
Save its key as `service_account.json` in the project folder (it's gitignored)
and share the responses sheet with the service account's email address as an
Editor. Add a column headed `Processed` after the form's own columns; the
script writes the deal id there. The form's timestamp is read as
day/month/year, which is what a sheet with a UK locale produces; change
`TIMESTAMP_FORMAT` in `process_sheet.py` if yours differs.

### Commands

```bash
# Process new form responses, or submissions saved as JSON files
python process_sheet.py --dry-run
python process_sheet.py
python enrich_lead.py submissions/train/*.json

# After reviewing deals on the board
python capture_corrections.py

# Score a version, compare everything scored so far
python evaluate.py --mode per-product-careful
python evaluate.py --mode per-product-careful --test-set data/test_set_2.json
python evaluate.py --report --test-set data/test_set_2.json

# Let the gate decide, and undo if needed
python evaluate.py --gate per-product-careful --promote
python evaluate.py --rollback
```

`--dry-run` on `enrich_lead.py` and `evaluate.py` prints the prompts without
calling Gemini, which helps when you're down to your last few requests of the
day.

### What's in the repo

| Path                       | Purpose                                                        |
| -------------------------- | -------------------------------------------------------------- |
| `enrich_lead.py`           | Prompt versions, the Gemini call, checks, flags, routing, HubSpot writes, run log |
| `process_sheet.py`         | Reads new rows from the form's responses sheet                 |
| `capture_corrections.py`   | Turns reviewed deals into correction records                   |
| `examples.py`              | Finds and formats similar past reviews                         |
| `evaluate.py`              | Test set scoring, the comparison report, the gate and rollback |
| `LABELLING.md`             | The rules behind every correction and test label               |
| `submissions/train/`       | 20 deliberately messy submissions used for the first reviews   |
| `data/runs.jsonl`          | Every pipeline run                                             |
| `data/corrections.jsonl`   | The candidate example pool: every reviewed deal                |
| `data/pools/`              | Frozen copies of pools that have passed the gate               |
| `data/live.json`           | The version and pool the pipeline runs                         |
| `data/promotions.jsonl`    | Every promotion and rollback                                   |
| `data/test_set.json`       | Test set 1: 10 single-category cases                           |
| `data/test_set_2.json`     | Test set 2: 18 mixed and ambiguous cases                       |
| `data/eval_results.jsonl`  | Every answer from every evaluation run                         |

## Where it stands, and what I'd do next

The live pipeline still runs v5. v7 is the strongest version on test set 2,
and it got the first 4 cases of test set 1 right before I ran out of requests
for the day. The gate won't promote it until it has been scored on all 10, so
that final check, two commands, is the last thing between v7 and live deals.
I'd rather leave it there than skip my own gate.

Beyond that:

- Bigger, fresher test sets. Both sets are small, so one case moves a score
  by 5 to 10 points, and v7's changes were shaped by looking at v6's results
  on test set 2. A new set of 30 or more cases would show how v7 really holds
  up.
- Use the business's own knowledge of its products. The known-borderline
  list in LABELLING.md could flag products like towels every time, without
  asking the model, and a real catalogue could go straight into the prompt.
  Both need fresh test data to measure fairly.
- Test the gate against deliberately bad feedback. The pool gate is in
  place, but I haven't yet fed it a batch of wrong corrections to show it
  rejecting them.
- Let the spot-check rate adapt to how often spot-checks find mistakes.
- Run on a paid tier. 20 requests a day kept every evaluation small. A paid
  tier would allow larger test sets, and repeated runs to smooth out the
  variation between identical requests.
- Log failed runs in the pipeline itself, so a broken reply from Gemini can
  be looked at later.
- Add automated tests. The checks and comparisons are plain functions, and
  I tested them by hand throughout, but they deserve a proper test suite.

## Background

The first version was a no-code Zapier flow connecting Google Forms, Gemini and
HubSpot. I rewrote it in Python to drop the paid Zapier steps and make the
error handling visible, and then spent the second half of the project on the
part I found most interesting: making it notice its own doubts, learn from the
people correcting it, and prove each improvement before trusting it.
