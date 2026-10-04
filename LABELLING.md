# Labelling rules

The rules for deciding the correct category and urgency of a request, used
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

## Category

One of: Cosmetics, Electronics, Fashion, Home, General.

- If an order mixes categories, pick the one that makes up most of the order.

## Edge cases

- No deadline mentioned: Urgency 2
- Contradictory signals ("no rush, but by tomorrow"): Choose the highest urgency
- "Most of the order" means: Greater number of units
- Products that fit no category (toys, machine parts): General
