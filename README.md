# Inbound Lead Enrichment Pipeline

A buyer fills in a form and describes what they want in their own words. This
project reads that text with Gemini, works out the product category and how
urgent the order is, and creates a contact and a deal in HubSpot, so a sales rep
opens the deal board to a triaged request instead of a paragraph of prose.

The second half of the project is about what happens after that. A rep checks
every deal and fixes whatever the model got wrong, and those fixes are fed back
into the prompt as examples. An evaluation on a hand-labelled test set decides
whether a new prompt version is allowed to replace the one that's running.

## The basic pipeline

```
Google Form ──▶ responses sheet ──▶ Gemini 2.5 Flash ──▶ checks and flags ──▶ HubSpot
                                                                    (contact + deal in "Rep Review Required")
```

For each new row in the responses sheet, `process_sheet.py`:

1. sends the free-text request to Gemini, which returns JSON with a one-line
   summary, an urgency tier from 1 to 3 and a category from a fixed list
2. checks that output and records a flag for anything it had to repair
3. creates or updates the contact in HubSpot, creates a deal and links the two
4. writes the deal id back into the sheet so the row is never processed twice
5. appends the whole run to `data/runs.jsonl`

`enrich_lead.py` runs exactly the same steps, but takes submissions saved as
JSON files instead of reading them from the sheet. That's how I ran the 20 test
submissions in `submissions/train/` without typing each one into the form.

## Learning from the rep's corrections

Every new deal lands in a "Rep Review Required" stage. The deal description
holds the buyer's original text, so the rep can read it next to the category and
urgency the model picked. If either is wrong they change it, and then they move
the deal on to the next stage whether they changed anything or not. The rep
doesn't fill in anything extra. The edit they make in the CRM is the feedback.

From there:

- `capture_corrections.py` looks at every deal that has left the review stage,
  compares HubSpot's final values with what the model originally said (taken
  from the run log) and writes one record per deal to `data/corrections.jsonl`.
  Deals the model got right are kept as well. If a reviewed field has been left
  empty, the deal is moved back to review instead of being counted as a
  correction.
- `examples.py` finds the three past requests most similar to a new one, using
  TF-IDF over the request text, and formats them for the prompt with what the
  AI first said and what the rep decided.
- `evaluate.py` scores a prompt version against 10 test cases that I labelled by
  hand and kept out of the example pool. Each answer is saved as soon as it
  comes back, so running out of quota halfway through doesn't lose anything.
- `evaluate.py --gate` compares a candidate version with the one that's live
  and only says PROMOTE if the candidate gets at least as many test cases fully
  right. Promoting means changing `LIVE_MODE` in `enrich_lead.py`.

The rules for what counts as the right answer (what urgency 3 means, what to do
with mixed orders and so on) are in [LABELLING.md](LABELLING.md). I wrote them
before reviewing anything, because inconsistent corrections would teach the
model contradictory things.

### Why every deal goes to review for now

The original plan was for new deals to land in Stage 1 (New Inbound Request)
and only the doubtful ones to go to Rep Review. For now everything goes to
review, for two reasons.

Accuracy can only be measured on deals someone has checked. If only flagged
deals were checked, I'd only ever find out about the mistakes the flags already
catch, and the learning loop would never see the rest.

The flags also aren't good enough yet. Of the four deals I had to correct, only
one had a flag on it, and three of the four flagged deals turned out to be fine.
The flags catch formatting problems, but the real mistakes were about deadlines.
Routing on flags alone would have let three of the four mistakes straight
through.

Once there's a signal that actually predicts mistakes (for example a deadline
close to the one-week or one-month boundary), the plan is to send only those
deals to review and the rest to Stage 1, along with a small random share of the
confident ones so there's always a check on what the flags miss.

## Results

When I reviewed the first 19 deals, the model had the category right every time
(19/19) but the urgency right only 15 times. All four urgency mistakes went the
same way: it rated the order one level more urgent than it was.

I then tried four prompt versions on the test set:

| Version | What's in the prompt                                       | Category | Urgency | Both right |
| ------- | ---------------------------------------------------------- | -------- | ------- | ---------- |
| v1      | the original prompt                                        | 9/10     | 7/10    | 6/10       |
| v2      | v1 plus 3 similar past corrections                         | 8/10     | 6/10    | 5/10       |
| v3      | v1 plus the date the request came in and the urgency rules | 8/10     | 10/10   | 8/10       |
| v4      | v3 plus 3 similar past corrections, urgency only           | 9/10     | 10/10   | 9/10       |

Adding past corrections on their own (v2) made things slightly worse. One test
case asked for 25 yoga blocks, and one of the examples it was shown was an order
for yoga mats and leggings. That order counts as Fashion under my rules because
there were more leggings than mats. The model took away "yoga means Fashion" and
got the new one wrong. My correction was fine; it was retrieved for the wrong
reason. The gate rejected v2, which is what I'd want it to do.

Most of the urgency mistakes turned out to be about dates. The prompt never said
what day it was, so the model had no way of turning "by mid-November" or "for
Black Friday" into a number of days. v3 adds the date the request was received
and the thresholds from LABELLING.md, and that fixed every urgency mistake in
the test set. So the biggest improvement came from telling the model something
it couldn't have known, rather than from the feedback loop.

v4 brings the corrections back but only shows their urgency, so the model can't
copy a category from a loosely related request. It scored 9/10, passed the gate
and is what the pipeline runs now. Its one-point lead over v3 comes down to the
yoga blocks case, which gave a different answer under every prompt version, so
I wouldn't read much into that point.

Ten test cases is a small set. One case moves accuracy by ten points, and I
designed v3 after looking at where v1 and v2 went wrong, so the test set isn't
as unseen as it was at the start. v3 only uses rules I had written before the
test set existed, but the proper next step is to check v4 on a fresh and bigger set.

## Decisions along the way

The prompt asks for a fixed JSON shape, a category from a fixed list and an
urgency tier with each level defined. Free-form answers would leave the CRM
fields impossible to filter on.

Gemini often wraps its JSON in markdown fences even when told not to, so they're
stripped before parsing. If it gets something wrong in a way that can be
repaired, like an urgency of "high" or a category that isn't on the list, the
value falls back to a default and a flag goes into the deal's AI Flags property.
My first version repaired these silently, which made a bad answer look just as
confident as a good one.

Contacts are matched by email, because people submit the form twice. Sheet
columns are found by their header text, so reordering the form's questions
doesn't shift every field along by one. A row that fails isn't marked as
processed, so the next run picks it up again.

Temperature, the setting that controls how much randomness goes into the
model's answer, is 0. Two identical requests shouldn't be triaged differently,
and it keeps the evaluation fair: when two prompt versions score differently,
the difference comes from the prompt and not from chance.

Every run is logged with the raw model output and the prompt version. Without
that log there'd be nothing to compare against, since HubSpot only keeps the
current value once a rep edits a deal. The corrections file works the other way
round: it's rebuilt from scratch on every run, because HubSpot holds the rep's
latest decision and they might change their mind.

For similarity I used TF-IDF without a stop-word list. scikit-learn's English
stop words include "within", "next", "by" and "no", which are exactly the words
that describe a deadline.

I also tried a version that ignored numbers, since "25 yoga blocks" was being
matched with "25 hair dryers" purely because of the 25. The only way to judge
whether that was better was to look at which examples the test cases got, and
adjusting the system until the test cases look good makes the test score
meaningless. So I kept the original and left the retrieval alone.

Each test case stores the date it was labelled against. If the evaluation used
today's date, the right answers would quietly change over time ("before the
20th" is urgency 2 on 5 October and urgency 3 a week later), and scores from
different days couldn't be compared. In the live pipeline the date comes from
the form's timestamp.

The free Gemini tier allows 20 requests per day per model, and that shaped a lot
of this. The script waits and retries when it hits the per-minute limit, and
stops straight away when the daily one runs out. The evaluation skips cases it
has already scored, so no request is ever spent twice on the same answer.

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
- `SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` and `SHEET_NAME`, only if you
  use the Google Sheets path

### HubSpot

The script writes custom properties that a new HubSpot account won't have.
Create them under Settings → Properties. Contacts and deals use different
internal names because I created them at different times; both mappings are at
the top of `enrich_lead.py`.

Contact properties:

| Internal name          | Type            |
| ---------------------- | --------------- |
| `ai_research_summary`  | Multi-line text |
| `product_category_fit` | Dropdown select |
| `urgency_tier`         | Dropdown select |

Deal properties:

| Internal name       | Type             |
| ------------------- | ---------------- |
| `order_summary`     | Multi-line text  |
| `product_category`  | Dropdown select  |
| `urgency_tier`      | Dropdown select  |
| `ai_flags`          | Single-line text |
| `ai_prompt_version` | Single-line text |

The category dropdowns need the options listed in `CATEGORIES`, including
`General`, and the urgency dropdowns need the internal values `1`, `2` and `3`.
The buyer's original text goes into the standard deal description field.

New deals go to the stage in `DEAL_STAGE`. HubSpot keeps a stage's original
internal id when you rename it (my "Rep Review Required" stage is
`presentationscheduled`), so look yours up with:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" https://api.hubapi.com/crm/v3/pipelines/deals
```

### Google Sheets

`process_sheet.py` needs a Google service account with the Sheets API enabled.
Save its key as `service_account.json` in the project folder (it's gitignored)
and share the responses sheet with the service account's email address as an
Editor. Add a column headed `Processed` after the form's own columns; the script
writes the deal id there.

The form's timestamp is read as day/month/year, which is what a sheet with a UK
locale produces. If your sheet uses a different locale, change
`TIMESTAMP_FORMAT` in `process_sheet.py`.

### Commands

```bash
# New form responses, or submissions saved as JSON files
python process_sheet.py --dry-run
python process_sheet.py
python enrich_lead.py submissions/train/*.json

# After reviewing deals on the board
python capture_corrections.py

# Score a prompt version, compare all versions, decide on promotion
python evaluate.py --mode rules-fewshot
python evaluate.py --report
python evaluate.py --gate rules-fewshot
```

`--dry-run` on `enrich_lead.py` and `evaluate.py` prints the prompts without
calling Gemini, which helps when you're down to your last few requests of the
day.

### What's in the repo

| Path                       | Purpose                                                    |
| -------------------------- | ---------------------------------------------------------- |
| `enrich_lead.py`           | Prompts, the Gemini call, checks, HubSpot writes, run log  |
| `process_sheet.py`         | Reads new rows from the form's responses sheet             |
| `capture_corrections.py`   | Turns reviewed deals into correction records               |
| `examples.py`              | Finds and formats similar past corrections                 |
| `evaluate.py`              | Test set scoring, the comparison report and the gate       |
| `LABELLING.md`             | The rules behind every correction and test label           |
| `submissions/train/`       | 20 deliberately messy submissions used for the first round |
| `data/runs.jsonl`          | Every pipeline run                                         |
| `data/corrections.jsonl`   | Every reviewed deal, the model's answer next to the rep's  |
| `data/test_set.json`       | The 10 hand-labelled test cases                            |
| `data/eval_results.jsonl`  | Every answer from every evaluation run                     |

## Limitations and next steps

- The test set is small and I've now looked at it several times. A new set of
  20 to 30 cases would show whether v4 really holds up.
- I haven't yet tested the gate against deliberately bad corrections. The plan
  is to add a few wrong ones to a copy of the example pool and check that
  accuracy drops and the gate rejects it.
- TF-IDF only matches words, so it can't tell that "Black Friday" and
  "Christmas" are related. Embeddings would, and a minimum similarity score
  would stop it padding the prompt with examples that aren't really similar.
- Every deal still goes to review. Routing only the doubtful ones there needs a
  signal that predicts mistakes better than the current flags (see above).
- My labelling rules were written for a made-up catalogue with five broad
  categories. A real distributor knows its whole product range, with categories
  and subcategories, which products belong where and what its usual lead times
  are. Putting that into LABELLING.md, and from there into the prompt, would
  remove most of the guesswork I had with things like hair dryers, kettles or
  yoga blocks.
- A few category rules, like kitchen appliances counting as Home, only exist in
  the test labels so far. They belong in LABELLING.md, and in the prompt once
  there's a fresh test set to measure them on.
- Failed runs aren't logged, so when Gemini returns broken JSON its reply is
  lost. Those are the runs I'd most like to look at.
- There are no automated tests yet. The checks and the comparison logic are
  plain functions and would be easy to cover.
- One submission at a time is fine at this volume, but HubSpot's batch
  endpoints would cut the number of API calls a lot.

## Background

The first version was a Zapier flow connecting Google Forms, Gemini and HubSpot.
I rewrote it in Python to drop the paid Zapier steps and to make the error
handling visible, then built the review and evaluation loop on top of it.
