"""General-purpose similarity scoring for names in a bounded directory."""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Mapping


MATCH_THRESHOLD = 0.84
AMBIGUITY_MARGIN = 0.035
_PHONETIC_GROUPS = {
    **{letter: "1" for letter in "bfpv"},
    **{letter: "2" for letter in "cgjkqsxz"},
    **{letter: "3" for letter in "dt"},
    "l": "4",
    **{letter: "5" for letter in "mn"},
    "r": "6",
}


def name_tokens(value: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    normalized = "".join(
        character if character.isalnum() else " "
        for character in without_marks
    )
    return tuple(normalized.split())


def phonetic_key(token: str) -> str:
    decomposed = unicodedata.normalize("NFKD", token.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    if not without_marks or any(character < "a" or character > "z" for character in without_marks):
        return ""
    first = without_marks[0]
    codes: list[str] = []
    previous: str | None = _PHONETIC_GROUPS.get(first)
    for character in without_marks[1:]:
        code = _PHONETIC_GROUPS.get(character)
        if code is None:
            previous = None
            continue
        if code != previous:
            codes.append(code)
        previous = code
    return (first.upper() + "".join(codes) + "000")[:4]


def rank_phonetic_candidates(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    query_keys = {phonetic_key(token) for token in name_tokens(query)}
    query_keys.discard("")
    if not query_keys:
        return ()
    matches = []
    for candidate in candidates:
        saved_keys = {
            phonetic_key(token)
            for token in name_tokens(candidate["normalized_alias"])
        }
        saved_keys.discard("")
        if query_keys & saved_keys:
            matches.append(dict(candidate))
    matches.sort(key=lambda item: item["display_name"].casefold())
    return tuple(matches)


def _jaro_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0

    window = max(0, max(len(left), len(right)) // 2 - 1)
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    match_count = 0

    for left_index, character in enumerate(left):
        start = max(0, left_index - window)
        stop = min(left_index + window + 1, len(right))
        for right_index in range(start, stop):
            if right_matches[right_index] or character != right[right_index]:
                continue
            left_matches[left_index] = True
            right_matches[right_index] = True
            match_count += 1
            break

    if match_count == 0:
        return 0.0

    matched_left = [
        character
        for character, matched in zip(left, left_matches)
        if matched
    ]
    matched_right = [
        character
        for character, matched in zip(right, right_matches)
        if matched
    ]
    transpositions = sum(
        left_character != right_character
        for left_character, right_character in zip(matched_left, matched_right)
    ) / 2
    return (
        match_count / len(left)
        + match_count / len(right)
        + (match_count - transpositions) / match_count
    ) / 3


def _jaro_winkler(left: str, right: str) -> float:
    similarity = _jaro_similarity(left, right)
    if similarity <= 0.7:
        return similarity
    prefix_length = 0
    for left_character, right_character in zip(left, right):
        if left_character != right_character or prefix_length == 4:
            break
        prefix_length += 1
    return similarity + prefix_length * 0.1 * (1 - similarity)


def _damerau_levenshtein(left: str, right: str) -> int:
    distances = [list(range(len(right) + 1))]
    for left_index in range(1, len(left) + 1):
        row = [left_index] + [0] * len(right)
        distances.append(row)
        for right_index in range(1, len(right) + 1):
            substitution_cost = left[left_index - 1] != right[right_index - 1]
            row[right_index] = min(
                distances[left_index - 1][right_index] + 1,
                row[right_index - 1] + 1,
                distances[left_index - 1][right_index - 1]
                + substitution_cost,
            )
            if (
                left_index > 1
                and right_index > 1
                and left[left_index - 1] == right[right_index - 2]
                and left[left_index - 2] == right[right_index - 1]
            ):
                row[right_index] = min(
                    row[right_index],
                    distances[left_index - 2][right_index - 2] + 1,
                )
    return distances[-1][-1]


def _token_similarity(left: str, right: str) -> float:
    similarity = _jaro_winkler(left, right)
    if min(len(left), len(right)) >= 3 and _damerau_levenshtein(left, right) <= 1:
        return max(0.9, similarity)
    return similarity


def name_similarity(query: str, saved_name: str) -> float:
    query_tokens = name_tokens(query)
    saved_tokens = name_tokens(saved_name)
    if not query_tokens or not saved_tokens:
        return 0.0

    if len(query_tokens) == 1:
        return max(
            _token_similarity(query_tokens[0], saved_token)
            for saved_token in saved_tokens
        )

    if len(query_tokens) == len(saved_tokens):
        return sum(
            _token_similarity(query_token, saved_token)
            for query_token, saved_token in zip(query_tokens, saved_tokens)
        ) / len(query_tokens)
    return _jaro_winkler(" ".join(query_tokens), " ".join(saved_tokens))


def rank_name_candidates(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    scored = sorted(
        (
            (name_similarity(query, candidate["normalized_alias"]), dict(candidate))
            for candidate in candidates
        ),
        key=lambda item: (-item[0], item[1]["display_name"].casefold()),
    )
    if not scored or scored[0][0] < MATCH_THRESHOLD:
        return ()
    floor = max(MATCH_THRESHOLD, scored[0][0] - AMBIGUITY_MARGIN)
    return tuple(candidate for score, candidate in scored if score >= floor)
