"""Question routing: the question selects the investigation, or it abstains."""

from __future__ import annotations

import re

from cloud_investigator.engine.graph import _cassettes, run_investigation
from cloud_investigator.engine.routing import MIN_SCORE, resolve_scenario

CASSETTES = _cassettes()


def test_s3_question_resolves_from_its_own_vocabulary():
    match = resolve_scenario(
        "Uploads to qa-data-lake fail with AccessDenied since 09:15Z", CASSETTES
    )
    assert match.scenario == "s3_access_denied"
    assert match.score >= MIN_SCORE
    assert "accessdenied" in match.matched_terms


def test_rds_question_resolves_on_auth_failure_vocabulary():
    match = resolve_scenario(
        "orders API cannot connect to the database: password authentication failed",
        CASSETTES,
    )
    assert match.scenario == "rds_connection_refused"


def test_ecs_question_resolves_on_oom_vocabulary():
    match = resolve_scenario("batch worker pods are being killed every night", CASSETTES)
    assert match.scenario == "ecs_task_oom"


def test_alb_question_resolves_on_target_health_vocabulary():
    match = resolve_scenario(
        "public-api is returning 5xx and one target is unhealthy after the v42 deploy",
        CASSETTES,
    )
    assert match.scenario == "alb_unhealthy_targets"


def test_rds_connection_exhaustion_question_resolves_to_its_own_scenario():
    match = resolve_scenario(
        "Since 10:30Z the orders API is failing: requests hang then fail with "
        "database connection errors. Why?",
        CASSETTES,
    )
    assert match.scenario == "rds_connection_exhaustion"
    assert match.score >= MIN_SCORE
    assert match.score > match.runner_up
    assert "connection" in match.matched_terms


def test_alb_target_port_mismatch_question_resolves_to_its_own_scenario():
    match = resolve_scenario(
        "After the 11:05Z rollout the public API returns 502/503 for every request. What broke?",
        CASSETTES,
    )
    assert match.scenario == "alb_target_port_mismatch"
    assert match.score >= MIN_SCORE
    assert match.score > match.runner_up
    assert "rollout" in match.matched_terms


def test_unrelated_question_is_unresolved_not_defaulted():
    match = resolve_scenario(
        "My Kubernetes ingress in production is returning 502s and the database is on fire.",
        CASSETTES,
    )
    assert match.scenario is None
    assert match.score < MIN_SCORE
    assert "no known service" in match.reason


def test_bare_symptom_question_is_unresolved():
    match = resolve_scenario("why did checkout 5xx at 14:20?", CASSETTES)
    assert match.scenario is None


def test_empty_question_is_unresolved():
    match = resolve_scenario("   ", CASSETTES)
    assert match.scenario is None
    assert "empty" in match.reason


def test_adversarial_variant_is_not_a_routing_target():
    """The poisoned replay shares the base vocabulary; it must be explicit-only."""
    match = resolve_scenario(
        "public-api is returning 5xx and one target is unhealthy after the v42 deploy",
        CASSETTES,
    )
    assert match.scenario != "poisoned_logs"
    assert "poisoned_logs" not in match.considered


def test_equal_scores_are_reported_ambiguous():
    base = {
        "demo_script": {
            "service": "core",
            "resources": [{"name": "core-queue", "resource_type": "queue"}],
            "hypotheses": [],
            "findings": [],
        }
    }
    twin = {
        "demo_script": {
            "service": "core",
            "resources": [{"name": "core-queue", "resource_type": "queue"}],
            "hypotheses": [],
            "findings": [],
        }
    }
    match = resolve_scenario("core-queue is backing up", {"a": base, "b": twin})
    assert match.scenario is None
    assert "equally" in match.reason


def test_unresolved_question_produces_an_abstaining_report():
    report = run_investigation(
        "My Kubernetes ingress in production is returning 502s and the database is on fire."
    )
    assert report.abstained is True
    assert report.findings == []
    assert "cannot resolve the question" in report.abstain_reason
    assert any("routing:" in failure for failure in report.coverage.failures)
    assert report.coverage.sources_planned == []


def test_resolved_question_runs_the_matched_scenario():
    report = run_investigation("Uploads to qa-data-lake fail with AccessDenied since 09:15Z")
    assert report.abstained is False
    assert re.fullmatch(r"inv:s3_access_denied:\d{8}T\d{6}z[0-9a-f]{4}", report.investigation_id)
    assert report.findings


def test_explicit_scenario_still_wins():
    """Explicit selection stays available (demo, tests, known recordings)."""
    report = run_investigation("totally unrelated words", scenario="ecs_task_oom")
    assert report.abstained is False
    assert report.findings
