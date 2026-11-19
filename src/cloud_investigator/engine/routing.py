"""Question → scenario routing — deterministic, local, no model call.

This is the step that makes the natural-language question load-bearing. The
router scores the question against the vocabulary each recorded scenario
actually contains (service, resources, fault class, finding titles, runbook
names). A unique, sufficiently specific match selects the scenario; anything
else is *unresolved*, and the investigation abstains instead of silently
answering a canned scenario.

Deliberately not a keyword-to-scenario lookup table: the question must overlap
the scenario's own structured vocabulary. This is still deterministic routing,
not model inference — a real planner replaces it behind the same seam.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Words that carry no scenario signal, plus vocabulary every scenario shares.
STOPWORDS = frozenset(
    [
        "about",
        "above",
        "after",
        "again",
        "against",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "cannot",
        "could",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "from",
        "further",
        "have",
        "having",
        "here",
        "into",
        "itself",
        "just",
        "more",
        "most",
        "other",
        "over",
        "same",
        "should",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "until",
        "very",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
        "your",
        "error",
        "errors",
        "fail",
        "fails",
        "failing",
        "failed",
        "failure",
        "issue",
        "problem",
        "incident",
        "since",
        "start",
        "started",
        "morning",
        "today",
        "nightly",
        "every",
        "please",
        "show",
        "find",
        "check",
        "logs",
        "log",
        "service",
        "services",
        "system",
        "environment",
        "production",
        "staging",
        "dev",
        "qa",
    ]
)

#: Specificity weight for a multi-word term found verbatim in the question.
PHRASE_WEIGHT = 2

#: Minimum score for a unique best match to be trusted.
MIN_SCORE = 2


@dataclass(frozen=True)
class ScenarioTerms:
    """Searchable vocabulary extracted from one cassette's demo script."""

    scenario: str
    service: str
    phrases: tuple[str, ...]
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioMatch:
    """Routing outcome. ``scenario`` is None when the question is unresolved."""

    scenario: str | None
    score: int
    runner_up: int
    matched_terms: tuple[str, ...]
    considered: tuple[str, ...]
    reason: str


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if len(t) >= 4 and t not in STOPWORDS}


def _phrases(text: str) -> set[str]:
    """Multi-word identifiers worth matching verbatim (hyphenated names, ARNs)."""
    out: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9:/._-]{5,}", text):
        candidate = _normalize(raw)
        if len(candidate.split()) >= 2:
            out.add(candidate)
    return out


def _collect(texts: list[str]) -> tuple[set[str], set[str]]:
    phrases: set[str] = set()
    tokens: set[str] = set()
    for text in texts:
        if not text:
            continue
        phrases |= _phrases(text)
        tokens |= _tokens(text)
    return phrases, tokens


def scenario_terms(scenario: str, cassette: dict) -> ScenarioTerms:
    """Vocabulary of one scenario: service, resources, causes, findings, runbook."""
    script = cassette.get("demo_script") or {}
    texts: list[str] = [script.get("service", ""), scenario.replace("_", " ")]

    for resource in script.get("resources", []):
        texts.extend(
            [
                resource.get("name", ""),
                resource.get("resource_id", ""),
                resource.get("resource_type", ""),
                resource.get("service", ""),
            ]
        )

    for hypothesis in script.get("hypotheses", []):
        texts.append(hypothesis.get("statement", ""))

    for finding in script.get("findings", []):
        texts.extend([finding.get("cause_token", ""), finding.get("title", "")])
        resource = finding.get("resource") or {}
        texts.extend(
            [resource.get("name", ""), resource.get("resource_id", ""), resource.get("service", "")]
        )

    for runbook in script.get("runbooks") or []:
        texts.append(runbook)

    # The plan rationale names the sources and signals a human would look for.
    texts.append((script.get("plan") or {}).get("rationale", ""))

    phrases, tokens = _collect(texts)
    return ScenarioTerms(
        scenario=scenario,
        service=script.get("service", ""),
        phrases=tuple(sorted(phrases)),
        tokens=tuple(sorted(tokens)),
    )


def score_terms(question: str, terms: ScenarioTerms) -> tuple[int, tuple[str, ...]]:
    """Score one scenario against the question; return (score, matched terms)."""
    question_norm = _normalize(question)
    question_tokens = set(question_norm.split())
    matched: list[str] = []
    score = 0

    for phrase in terms.phrases:
        if phrase and phrase in question_norm:
            matched.append(phrase)
            score += PHRASE_WEIGHT

    for token in terms.tokens:
        if token in question_tokens and token not in matched:
            matched.append(token)
            score += 1

    return score, tuple(matched)


def resolve_scenario(question: str, cassettes: dict[str, dict]) -> ScenarioMatch:
    """Pick the scenario the question names, or report why it is unresolved."""
    # Adversarial variants (poisoned replays of a base scenario) share their
    # base's vocabulary by construction; they are selected explicitly, never
    # by routing, or every base-scenario question would be ambiguous.
    routable = {name: data for name, data in cassettes.items() if not data.get("variant_of")}
    considered = tuple(sorted(routable))
    if not question.strip():
        return ScenarioMatch(None, 0, 0, (), considered, "the question is empty")

    scored = sorted(
        (
            (score, terms.scenario, matched)
            for terms in (scenario_terms(name, data) for name, data in routable.items())
            for score, matched in [score_terms(question, terms)]
        ),
        key=lambda row: (-row[0], row[1]),
    )
    best_score, best_scenario, best_matched = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0

    if best_score < MIN_SCORE:
        return ScenarioMatch(
            None,
            best_score,
            runner_up,
            best_matched,
            considered,
            "no known service, resource, or fault vocabulary appears in the question",
        )
    if best_score == runner_up:
        return ScenarioMatch(
            None,
            best_score,
            runner_up,
            best_matched,
            considered,
            f"the question matches {best_scenario} and another scenario equally"
            " — name the service to disambiguate",
        )
    return ScenarioMatch(
        best_scenario,
        best_score,
        runner_up,
        best_matched,
        considered,
        f"matched {', '.join(best_matched[:6])}",
    )
