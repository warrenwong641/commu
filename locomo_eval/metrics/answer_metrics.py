from __future__ import annotations

from collections import Counter
import re

from rouge_score import rouge_scorer


_ARTICLES = re.compile(r"\b(a|an|the)\b", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s:/-]")
_DATE_PATTERN = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)\b",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(r"\b\d+(?:[.,:]\d+)?\b")
_ENTITY_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9'-]*(?:\s+[A-Z][A-Za-z0-9'-]*)*\b")


def normalize_answer(text: str) -> str:
    text = text.lower()
    text = _PUNCT.sub(" ", text)
    text = _ARTICLES.sub(" ", text)
    return " ".join(text.split())


def legacy_token_f1(prediction: str, reference: str) -> float:
    pred_tokens = prediction.lower().split()
    ref_tokens = reference.lower().split()
    return _token_f1_from_tokens(pred_tokens, ref_tokens)


def _token_f1_from_tokens(pred_tokens: list[str], ref_tokens: list[str]) -> float:
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(ref_tokens)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def token_f1(prediction: str, reference: str) -> float:
    """SQuAD-style token overlap after punctuation/article normalization."""
    pred_tokens = normalize_answer(prediction).split()
    ref_tokens = normalize_answer(reference).split()
    return _token_f1_from_tokens(pred_tokens, ref_tokens)


def rouge_l(prediction: str, reference: str) -> float:
    scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)
    return float(scorer.score(reference, prediction)["rougeL"].fmeasure)


def exact_match(prediction: str, reference: str) -> float:
    return float(normalize_answer(prediction) == normalize_answer(reference))


def _extract_dates(text: str) -> set[str]:
    return {normalize_answer(match.group(0)) for match in _DATE_PATTERN.finditer(text)}


def _extract_numbers(text: str) -> set[str]:
    return {match.group(0).replace(",", "") for match in _NUMBER_PATTERN.finditer(text)}


def _extract_entities(text: str) -> set[str]:
    return {normalize_answer(match.group(0)) for match in _ENTITY_PATTERN.finditer(text)}


def set_f1(prediction_items: set[str], reference_items: set[str]) -> float | None:
    if not prediction_items and not reference_items:
        return None
    if not prediction_items or not reference_items:
        return 0.0
    overlap = len(prediction_items & reference_items)
    if overlap == 0:
        return 0.0
    precision = overlap / len(prediction_items)
    recall = overlap / len(reference_items)
    return 2 * precision * recall / (precision + recall)


def score_answer(prediction: str, reference: str) -> dict[str, float | None]:
    prediction_dates = _extract_dates(prediction)
    reference_dates = _extract_dates(reference)
    prediction_numbers = _extract_numbers(prediction)
    reference_numbers = _extract_numbers(reference)
    prediction_entities = _extract_entities(prediction)
    reference_entities = _extract_entities(reference)
    return {
        "token_f1": token_f1(prediction, reference),
        "legacy_token_f1": legacy_token_f1(prediction, reference),
        "rouge_l": rouge_l(prediction, reference),
        "exact_match": exact_match(prediction, reference),
        "date_f1": set_f1(prediction_dates, reference_dates),
        "number_f1": set_f1(prediction_numbers, reference_numbers),
        "entity_f1": set_f1(prediction_entities, reference_entities),
    }
