"""Text analysis for the lexical half of hybrid retrieval.

``tokenize`` is the surface tokenizer.  It keeps the tokens this corpus turns
on -- "$350", "99.9%", "net-30", "2-year" -- because splitting them loses
exactly the terms that separate one rate-table row from another.

``analyze`` is what actually goes into the BM25 index, and it does two more
things that a naive tokenizer does not:

**Morphological normalisation.**  Without it, "who has to approve this
discount" does not match a table whose cells read "Required Approver",
"self-approve" and "approval" -- three surface forms of the same word, none of
them equal to the query term.  That was a measured failure: the discount
approval table scored zero on BM25 for a question that was *about* discount
approval, and only the surrounding prose was retrieved.  A compact suffix
stemmer collapses all four forms to "approv".

**Compound splitting.**  "self-approve" is indexed as itself *and* as "self" +
"approve", so a hyphenated cell is reachable by either half without losing the
exact-match advantage of the whole.

Azure AI Search gets both of these from its ``en.microsoft`` language analyzer;
this module is the local backend's equivalent, so the two backends rank
comparably.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-/%$][a-z0-9]+)*|\$[\d,.]+|\d+%")
_COMPOUND_SPLIT = re.compile(r"[-/.]")

MIN_STEM = 3

# Longest suffix first; each entry is (suffix, replacement).
_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("ization", "ize"),
    ("isation", "ize"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("ations", "ate"),
    ("ation", "ate"),
    ("ements", ""),
    ("ement", ""),
    ("ments", ""),
    ("ment", ""),
    ("ingly", ""),
    ("edly", ""),
    ("ities", "ity"),
    ("ies", "y"),
    ("ing", ""),
    ("ers", ""),
    ("ors", ""),
    ("est", ""),
    ("ed", ""),
    ("er", ""),
    ("or", ""),
    ("es", ""),
    ("ly", ""),
    # "approval" -> "approv", "renewal" -> "renew". Over-stems a few words
    # ("annual" -> "annu"), which is harmless because the query is stemmed the
    # same way.
    ("al", ""),
    ("s", ""),
)

# Inflectional (plural) suffixes: after stripping one of these a derivational
# suffix may still remain, so a second pass is allowed.
_PLURAL_SUFFIXES = frozenset({"s", "es", "ies"})


def stem(token: str) -> str:
    """Compact suffix stemmer.

    Deliberately not linguistically correct -- "annual" becomes "annu" -- which
    is harmless because the same transformation is applied to the query.  What
    matters is that surface variants of one word collapse to one term, and that
    the rules are stable enough to reason about when debugging a ranking.
    Numbers and money tokens are returned untouched.
    """
    if len(token) <= MIN_STEM or any(ch.isdigit() for ch in token):
        return token

    # Plurals are stripped first, then at most one derivational suffix -- the
    # same ordering Porter uses.  Letting a second derivational pass run would
    # take "reimbursement" to "reimbur" while "reimburse" stops at "reimburs",
    # which defeats the whole point.
    for _ in range(2):
        for suffix, replacement in _SUFFIX_RULES:
            if not token.endswith(suffix):
                continue
            if len(token) - len(suffix) < MIN_STEM:
                continue
            # A final "ss" is part of the word, not a plural: stripping it makes
            # "business" and "businesses" stem differently, which is worse than
            # not stemming at all.
            if suffix in ("s", "es") and token.endswith("ss"):
                continue
            token = token[: -len(suffix)] + replacement
            inflectional = suffix in _PLURAL_SUFFIXES
            break
        else:
            break
        if not inflectional:
            break

    # "approve" -> "approv" so that it meets "approval" and "approver", and
    # "sale" -> "sal" so that it meets "sales".
    if len(token) > MIN_STEM and token.endswith("e"):
        token = token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Surface tokens, lowercased, with money and percentages kept whole."""
    return _TOKEN_RE.findall(text.lower())


def analyze(text: str) -> list[str]:
    """Index/query terms: stemmed, with compound tokens also split."""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        terms.append(stem(token))
        if _COMPOUND_SPLIT.search(token):
            for part in _COMPOUND_SPLIT.split(token):
                if len(part) > 1:
                    terms.append(stem(part))
    return terms
