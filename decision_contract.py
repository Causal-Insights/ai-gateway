"""TypeSafe System One wire contract; no generative or media fallback."""
from __future__ import annotations

import json
import math

MODEL = "jev-1.13.0"
PROVIDER_URL = "https://api.typesafe.ai/v1/systemone"
ROUTE = "decisions"
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class DecisionError(ValueError):
    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _invalid(message):
    raise DecisionError("INVALID_DECISION_REQUEST", message)


def _structured(value):
    return isinstance(value, (str, dict, list))


def validate_request(payload):
    if not isinstance(payload, dict) or set(payload) != {"model", "state", "questions"}:
        _invalid("Use exactly model, state, and questions.")
    if payload["model"] != MODEL:
        raise DecisionError("UNSUPPORTED_DECISION_MODEL", "Use the pinned Jev decision model.")
    if not _structured(payload["state"]):
        _invalid("state must be a string, JSON object, or array.")
    questions = payload["questions"]
    if not isinstance(questions, dict) or not questions:
        _invalid("questions must contain at least one typed question.")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        _invalid("The request must contain finite JSON values.")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or not isinstance(question, dict):
            _invalid("Each question needs a nonempty id and a typed object.")
        if set(question) - {"type", "instructions", "criteria"}:
            _invalid("Questions support only type, instructions, and criteria.")
        if not _structured(question.get("instructions")):
            _invalid("Question instructions must be text, a JSON object, or an array.")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "noul":
            if "criteria" in question and (not isinstance(criteria, dict) or
                    set(criteria) - {"true", "false"} or not all(_structured(v) for v in criteria.values())):
                _invalid("Noul criteria describe true and/or false.")
        elif kind == "choice":
            if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255 or any(
                    not isinstance(k, str) or not k or (v is not None and not _structured(v))
                    for k, v in criteria.items()):
                _invalid("Choice requires 1 to 255 named text/JSON/null criteria.")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or not all(_structured(v) for v in criteria):
                _invalid("Score requires 2 to 10 ordered text/JSON criteria.")
        else:
            _invalid("Question type must be noul, choice, or score.")
    return payload


def _number(value, low, high):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high and math.isfinite(value)


def _hundredth_rounded(values):
    return all(abs(value * 100 - round(value * 100)) < 0.000001 for value in values)


def _probability_total_valid(values):
    total = sum(values)
    if abs(total - 1) <= 0.001:
        return True
    # Native Jev responses round each probability to hundredths. A 65-option
    # live response totalled .99; validate its possible unrounded interval.
    return total > 0 and _hundredth_rounded(values) and (
        sum(max(0, value - 0.005) for value in values) <= 1.000001 and
        sum(min(1, value + 0.005) for value in values) >= 0.999999)


def _score_tolerance(probabilities):
    if not _hundredth_rounded(probabilities.values()):
        return 0.001
    return 0.005 + sum(int(key) * 0.005 for key in probabilities) + 0.000001


def validate_response(payload, request):
    def invalid(reason):
        error = DecisionError("INVALID_DECISION_RESPONSE", "The decision provider returned an invalid typed answer.", 502)
        error.validation_reason = reason
        raise error

    if not isinstance(payload, dict) or payload.get("model") != MODEL:
        invalid("served_model")
    usage = payload.get("usage")
    if not isinstance(usage, dict) or any(
            not isinstance(usage.get(k), int) or isinstance(usage.get(k), bool) or usage[k] < 0
            for k in ("input_tokens", "output_tokens")):
        invalid("numeric_usage")
    answers = payload.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(request["questions"]):
        invalid("answer_keys")
    result = {}
    for key, question in request["questions"].items():
        answer, kind = answers[key], question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            invalid("answer_type")
        if kind == "noul":
            if not _number(answer.get("noul"), 0, 1):
                invalid("noul_range")
            result[key] = {"type": kind, "noul": answer["noul"]}
            continue
        expected = set(question["criteria"]) if kind == "choice" else {str(i) for i in range(len(question["criteria"]))}
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != expected:
            invalid("probability_keys")
        if not all(_number(v, 0, 1) for v in probabilities.values()):
            invalid("probability_range")
        if not _probability_total_valid(list(probabilities.values())):
            invalid("probability_sum")
        if not _number(answer.get("confidence"), 0, 1):
            invalid("confidence_range")
        clean = {"type": kind, "probabilities": probabilities, "confidence": answer["confidence"]}
        if kind == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in expected or probabilities[choice] + 0.000001 < max(probabilities.values()):
                invalid("choice_membership_or_maximum")
            clean["choice"] = choice
        else:
            legend = answer.get("legend")
            weighted = sum(int(k) * v for k, v in probabilities.items())
            if (not _number(answer.get("score"), 0, len(expected) - 1) or
                    abs(answer["score"] - weighted) > _score_tolerance(probabilities) or
                    not isinstance(legend, dict) or set(legend) != expected or
                    not all(isinstance(v, str) for v in legend.values())):
                invalid("score_consistency")
            clean.update(score=answer["score"], legend=legend)
        result[key] = clean
    return {"model": MODEL, "answers": result,
            "usage": {key: usage[key] for key in ("input_tokens", "output_tokens")}}
