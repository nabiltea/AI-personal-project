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


def format_examples(examples: list) -> str:
    """Render examples for the prompt, showing the AI's first answer and the
    rep's verdict, so the model can see which way its mistakes tend to go."""
    lines = [
        "Here are similar past requests. For each, you can see the AI's first "
        "answer and the sales rep's final verdict. Follow the rep's judgement."
    ]
    for example in examples:
        ai, human = example["ai"], example["human"]
        lines.append(f'\nRequest: "{example["product_request"]}"')
        lines.append(f"AI answered: category {ai['category']}, urgency {ai['urgency']}")
        if example["corrected_fields"]:
            lines.append(
                f"Rep corrected it to: category {human['category']}, "
                f"urgency {human['urgency']}"
            )
        else:
            lines.append("Rep confirmed this was correct.")
    return "\n".join(lines)
