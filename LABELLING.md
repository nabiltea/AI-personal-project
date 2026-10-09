# Labelling rules

The rules for deciding the correct categories and urgency of a request, used
when reviewing deals on the board and when labelling the held-out test set.

The pipeline learns from corrections, so every correction has to follow the
same rules. Inconsistent labels teach the system contradictory lessons and make
accuracy look worse than it is.

## Urgency

Measured from the day the request comes in.

| Tier | Meaning                          | Examples                                  |
| ---- | -------------------------------- | ----------------------------------------- |
| 3    | Needed within a week             | "ASAP", "by Friday", "reopens in 3 days"  |
| 2    | Needed in one week to one month  | "within two weeks", "sometime this month" |
| 1    | Needed more than a month away    | "next quarter", "for next season"         |

## Categories

Each product belongs to one of: Cosmetics, Electronics, Fashion, Home, General.

- An order gets every category its products belong to, e.g. 10 bedsheets and
  4 phones is "Home, Electronics". A single-category order gets just one.
- There is no main category, so the number of units doesn't matter.

Product types decided so far:

- Personal electrical devices (toothbrushes, hair dryers, shavers): Electronics
- Kitchen appliances (kettles, toasters, air fryers): Home
- Sports equipment (yoga blocks): General

Until 9 October 2026 each order had a single category, the one with the most
units. The first 19 reviewed deals and `data/test_set.json` were labelled that
way.

## Needs review

Only used when labelling a test set. Mark a case `needs_review: true` if a
person should check it before outreach: a product could genuinely belong to two
categories, or the request is too unclear to act on. A mixed order on its own
is not a reason, as long as each product's category is clear.

## Edge cases

- No deadline mentioned: Urgency 2
- Contradictory signals ("no rush, but by tomorrow"): Choose the highest urgency
- Products that fit no category (toys, machine parts): General
