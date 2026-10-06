"""
The learning step: find past reviewed requests that resemble a new one and
format them as examples for the prompt.

The example pool is data/corrections.jsonl: every deal a sales rep has
reviewed, both the ones they corrected and the ones they confirmed. Showing
only corrections would push the model in one direction (they all say "less
urgent"); the confirmed ones keep it balanced.
"""

import json

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

EXAMPLES_PER_REQUEST = 3

ALL_FIELDS = ("category", "urgency")


def load_pool(path) -> list:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def find_similar(product_request: str, pool: list, k: int = EXAMPLES_PER_REQUEST) -> list:
    """Return the k pool records whose request text is most like this one.

    TF-IDF turns each text into a vector of word weights, where words that are
    rare across the pool ("Christmas", "quarter") weigh more than words that
    appear everywhere ("need", "units"). Cosine similarity then scores how
    closely two vectors point the same way, from 0 (nothing shared) to 1.

    No stop-word list: the standard English one drops "within", "next", "by"
    and "no", which are exactly the words that carry a deadline. Word pairs
    (ngram_range=(1, 2)) let phrases like "two weeks" or "no rush" match as
    phrases.
    """
    vectorizer = TfidfVectorizer(ngram_range=(1, 2))
    pool_vectors = vectorizer.fit_transform(r["product_request"] for r in pool)
    request_vector = vectorizer.transform([product_request])
    scores = cosine_similarity(request_vector, pool_vectors)[0]

    ranked = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)
    return [pool[i] for i in ranked[:k]]


def format_examples(examples: list, fields: tuple = ALL_FIELDS) -> str:
    """Render examples for the prompt, showing the AI's first answer and the
    rep's verdict, so the model can see which way its mistakes tend to go.

    fields limits what the examples show. With ("urgency",) the model never
    sees past categories, so it cannot copy one from a request that merely
    shares a word with the new one (as "yoga mats" -> Fashion did for
    "yoga blocks"). The reviewed deals held no category corrections, so the
    examples had nothing to teach about category anyway.
    """
    if fields == ALL_FIELDS:
        intro = (
            "Here are similar past requests. For each, you can see the AI's "
            "first answer and the sales rep's final verdict. Follow the rep's "
            "judgement."
        )
    else:
        intro = (
            f"Here are similar past requests. For each, you can see the "
            f"{' and '.join(fields)} the AI first gave and the sales rep's final "
            f"verdict. Use them only to judge {' and '.join(fields)}."
        )

    def describe(answer):
        return ", ".join(f"{f} {answer[f]}" for f in fields)

    lines = [intro]
    for example in examples:
        lines.append(f'\nRequest: "{example["product_request"]}"')
        lines.append(f"AI answered: {describe(example['ai'])}")
        if any(f in example["corrected_fields"] for f in fields):
            lines.append(f"Rep corrected it to: {describe(example['human'])}")
        else:
            lines.append("Rep confirmed this was correct.")
    return "\n".join(lines)
