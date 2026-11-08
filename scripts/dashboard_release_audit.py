#!/usr/bin/env python3
"""Blocking weekly release gate for the AEO dashboard.

It validates the full score and citation releases, their compact score bootstrap,
and the dashboard's required roll-up protections.  Inputs accept local paths or
gs:// URIs.  It writes a durable JSON report and exits non-zero on a failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_RACES = {"LA Mayor"}
RACE_ALIASES = {
    "MS Circuit Court Judge, District 12, Place 2": "MS Circuit Court Judge, District 14, Place 2",
    "NC District Court Judge, District 6, Seat 2, New Hanover-Pender Counties": "NC District Court Judge, District 6, Seat 2, New Hanover/Pender Counties",
    "NC District Court Judge, District 6, Seat 3, New Hanover-Pender Counties": "NC District Court Judge, District 6, Seat 3, New Hanover/Pender Counties",
    "NC District Court Judge, District 6, Seat 4, New Hanover-Pender Counties": "NC District Court Judge, District 6, Seat 4, New Hanover/Pender Counties",
}
# Oct. 25 source audit: dashboard excludes this malformed source score. New invalid
# bucket values fail the release instead of silently changing a denominator.
ALLOWED_INVALID_BUCKETS = {
    ("New Hanover County Commission — Rob Zapple", "WS2-1029", "opponent", "narrative_alignment", "4"),
}
DIMS = ("depth", "accuracy", "narrative", "consistency")
METRIC_NAMES = ("visibility", "favorability", "depth", "accuracy", "narrative", "consistency")
VALID_CITATION_TYPES = {"sources_cited", "sources_search", "sources_more", "sources_references", "sources_shopping"}
SNAPSHOT_DIMS = ("visibility", "favorability", "depth", "accuracy", "narrative")
SNAPSHOT_WEIGHTS = {"visibility": 0.40, "favorability": 0.15, "depth": 0.15, "accuracy": 0.15, "narrative": 0.15}
REQUIRED_METRIC_STATUS = {
    "citation_breakout",
    "owned_domain_visibility_and_coverage",
    "owned_domain_on_topic_citation_rate",
    "owned_domain_citation_yield_and_peer_rank",
    "owned_domain_answer_quality",
    "owned_domain_share_of_voice",
    "score_dimensions_and_aggregate",
    "site_analytics_search_traffic",
}
VALID_METRIC_STATES = {"updated", "retained", "blocked", "not_applicable"}
VALID_SOURCE_COMPLETENESS = {
    "complete",
    "complete_with_declared_partial_dates",
    "complete_with_approved_exclusions",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OWNED_DOMAIN_METRIC_FAMILIES = {
    "owned_domain_visibility_and_coverage",
    "owned_domain_on_topic_citation_rate",
    "owned_domain_citation_yield_and_peer_rank",
    "owned_domain_answer_quality",
    "owned_domain_share_of_voice",
}


def raw_score_dimension(dim):
    return "narrative_alignment" if dim == "narrative" else dim


class Audit:
    def __init__(self):
        self.checks, self.errors, self.warnings = [], [], []

    def check(self, name, passed, detail, blocking=True):
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail, "blocking": blocking})
        if not passed:
            (self.errors if blocking else self.warnings).append(f"{name}: {detail}")

    def payload(self):
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "passed" if not self.errors else "failed",
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def canonical_race(race):
    return RACE_ALIASES.get(race, race)


def tracked_races(races):
    return {canonical_race(race) for race in races if canonical_race(race) not in EXCLUDED_RACES}


def read_bytes(source):
    if source.startswith("gs://"):
        result = subprocess.run(["gcloud", "storage", "cat", source], capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
        return result.stdout
    return Path(source).read_bytes()


def load_json(source):
    raw = read_bytes(source)
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def column(document, name):
    source = document["data"][name]
    return [source["v"][i] for i in source["i"]] if isinstance(source, dict) else list(source)


def require_columns(document, names):
    missing = set(names) - set(document.get("columns", []))
    if missing:
        raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
    values = {name: column(document, name) for name in names}
    row_count = int(document["row_count"])
    bad = [name for name, data in values.items() if len(data) != row_count]
    if bad:
        raise ValueError("Column lengths do not match row_count: " + ", ".join(bad))
    return values


def number(value):
    if value in (None, ""):
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def bucket(value):
    return int(value) if value in (0, 1, 2, "0", "1", "2") else None


def eligible(row, side, include_refusals):
    if not include_refusals and row["is_refusal"] == "Y":
        return False
    if row["is_blind"] == "Y" or not row["target_candidate"]:
        return True
    other = row["opponent_name"] if side == "candidate" else row["candidate_name"]
    return not (other and row["target_candidate"] == other)


def expected_metrics(rows, side, include_refusals):
    visibility, favorability = {}, {}
    buckets = {dim: [0, 0] for dim in DIMS}
    for row in rows:
        prompt = row["prompt_id"]
        value = number(row[f"prompt_level_visibility_score_{side}"])
        if eligible(row, side, include_refusals) and value is not None:
            visibility[prompt] = value
        positive = number(row[f"prompt_level_favorability_pct_positive_{side}"])
        negative = number(row[f"prompt_level_favorability_pct_negative_{side}"])
        if positive is not None and negative is not None:
            favorability[prompt] = 50 + (positive - negative) / 2
        for dim in DIMS:
            value = bucket(row[f"{raw_score_dimension(dim)}_score_{side}"])
            if value is not None:
                buckets[dim][0] += 1
                buckets[dim][1] += int(value == 2)
    output = {
        "visibility": sum(visibility.values()) / len(visibility) if visibility else None,
        "favorability": sum(favorability.values()) / len(favorability) if favorability else None,
    }
    output.update({dim: 100 * good / total if total else None for dim, (total, good) in buckets.items()})
    return output


def equal(left, right):
    return (left is None and right is None) or (left is not None and right is not None and abs(left - right) <= 1e-6)


def audit_scores(audit, full, full_hash, bootstrap):
    audit.check(
        "score bootstrap has a supported browser contract",
        bootstrap.get("schema_version") == 1
        and bootstrap.get("artifact_category") in {"dashboard_score_bootstrap", "score_bootstrap_compact_metrics"}
        and isinstance(bootstrap.get("roster"), list)
        and isinstance(bootstrap.get("dates"), list)
        and isinstance(bootstrap.get("metrics"), dict)
        and isinstance(bootstrap.get("metrics_including_refusals"), dict),
        f"schema={bootstrap.get('schema_version')} category={bootstrap.get('artifact_category')}",
    )
    metric_sets = (bootstrap.get("metrics", {}), bootstrap.get("metrics_including_refusals", {}))
    daily_contract_ok = all(
        isinstance(metric.get("dailyBucketTotals"), dict)
        and all(
            isinstance(metric["dailyBucketTotals"].get(dimension), list)
            and len(metric["dailyBucketTotals"][dimension]) == len(bootstrap.get("dates", []))
            for dimension in DIMS
        )
        for metric_set in metric_sets
        for metric in metric_set.values()
    )
    audit.check(
        "score bootstrap retains daily denominators for date and kickoff views",
        daily_contract_ok,
        f"metric_sets={len(metric_sets)} dates={len(bootstrap.get('dates', []))}",
    )
    needed = [
        "race", "prompt_id", "ai_provider", "report_date", "is_blind", "is_refusal", "target_candidate",
        "candidate_name", "opponent_name", "prompt_level_visibility_score_candidate",
        "prompt_level_visibility_score_opponent", "prompt_level_favorability_pct_positive_candidate",
        "prompt_level_favorability_pct_negative_candidate", "prompt_level_favorability_pct_positive_opponent",
        "prompt_level_favorability_pct_negative_opponent",
    ] + [f"{raw_score_dimension(dim)}_score_{side}" for dim in DIMS for side in ("candidate", "opponent")]
    data = require_columns(full, needed)
    count = int(full["row_count"])
    audit.check("score release has response rows", count > 0, f"{count:,} rows")
    score_dates = sorted(set(data["report_date"]))
    audit.check("score dates equal bootstrap dates", score_dates == bootstrap.get("dates", []), f"score={len(score_dates)} bootstrap={len(bootstrap.get('dates', []))}")
    audit.check("bootstrap source hash ties to raw score release", bootstrap.get("source_aggregate_sha256") == full_hash, f"expected={bootstrap.get('source_aggregate_sha256')} actual={full_hash}")

    roster = bootstrap.get("roster", [])
    roster_races, raw_races = tracked_races(row["race"] for row in roster), tracked_races(data["race"])
    audit.check("dashboard roster remains 107 races", len(roster_races) == 107, f"{len(roster_races)} races")
    audit.check("full score race universe ties to roster", raw_races == roster_races, f"score={len(raw_races)} roster={len(roster_races)}")

    # The dashboard averages prompt-level metrics once per prompt/date. Verify vendor fields are
    # actually invariant across response samples/providers before relying on that contract.
    for side in ("candidate", "opponent"):
        cells, conflicts = {}, 0
        for i in range(count):
            value = number(data[f"prompt_level_visibility_score_{side}"][i])
            if value is None:
                continue
            key = (data["prompt_id"][i], data["report_date"][i])
            prior = cells.setdefault(key, value)
            conflicts += int(abs(prior - value) > 1e-9)
        audit.check(f"{side} prompt-level visibility is sample-stable", conflicts == 0, f"cells={len(cells):,} conflicts={conflicts}")

    unexpected, allowed = [], []
    for i in range(count):
        race = canonical_race(data["race"][i])
        for side in ("candidate", "opponent"):
            for dim in DIMS:
                raw_dim = raw_score_dimension(dim)
                value = data[f"{raw_dim}_score_{side}"][i]
                if value in (None, "") or bucket(value) is not None:
                    continue
                signature = (race, data["prompt_id"][i], side, raw_dim, str(value))
                (allowed if signature in ALLOWED_INVALID_BUCKETS else unexpected).append(" | ".join(signature))
    audit.check("only documented malformed score buckets occur", not unexpected, f"unexpected={len(unexpected)}" + (f" first={unexpected[0]}" if unexpected else ""))
    audit.check("known malformed bucket is explicitly excluded", len(allowed) == len(ALLOWED_INVALID_BUCKETS), f"documented={len(allowed)}", blocking=False)

    by_race = defaultdict(list)
    for i in range(count):
        race = canonical_race(data["race"][i])
        if race not in EXCLUDED_RACES:
            by_race[race].append({field: values[i] for field, values in data.items()})
    expected_keys = set()
    for mode, metrics in (("standard", bootstrap.get("metrics", {})), ("include_refusals", bootstrap.get("metrics_including_refusals", {}))):
        mismatches = []
        for source_index, roster_row in enumerate(roster):
            race = roster_row["race"]
            if race in EXCLUDED_RACES:
                continue
            for side, prefix in (("candidate", "c"), ("opponent", "o")):
                if side == "opponent" and not roster_row.get("opponent_name"):
                    continue
                key = prefix + str(source_index)
                expected_keys.add(key)
                actual = metrics.get(key, {}).get("base", {})
                expected = expected_metrics(by_race[race], side, mode == "include_refusals")
                mismatches.extend(f"{key}:{field}" for field in METRIC_NAMES if not equal(expected[field], actual.get(field)))
        audit.check(f"{mode} bootstrap has all visible entities", expected_keys <= set(metrics), f"missing={len(expected_keys - set(metrics))}")
        audit.check(f"{mode} base metrics exactly tie to raw", not mismatches, f"checked={len(expected_keys) * len(METRIC_NAMES)} mismatches={len(mismatches)}" + (f" first={mismatches[0]}" if mismatches else ""))
    return roster_races, score_dates


def audit_citations(audit, citation, manifest, roster_races, score_dates, expected_through):
    data = require_columns(citation, ["report_date", "race", "source_type", "source_count", "aeo_site_category", "prompt_category", "topic"])
    count = int(citation["row_count"])
    citation_dates = sorted(set(data["report_date"]))
    audit.check("citation release has rows", count > 0, f"{count:,} rows")
    audit.check("citation race universe ties to roster", tracked_races(data["race"]) == roster_races, f"citation={len(tracked_races(data['race']))} roster={len(roster_races)}")
    # Citation observations and scored responses are separate vendor deliveries. They often share
    # a calendar but need not have identical sample days (for example, a citation-only Sep. 12
    # snapshot is valid). The release gate therefore validates each endpoint independently rather
    # than falsely failing a legitimate asynchronous delivery cadence.
    audit.check("citation dates are ordered and usable", citation_dates == sorted(citation_dates) and bool(citation_dates), f"citation={len(citation_dates)} score={len(score_dates)}")
    if expected_through:
        audit.check("refresh reaches requested endpoint", bool(citation_dates) and citation_dates[-1] == expected_through, f"latest={citation_dates[-1] if citation_dates else 'none'} expected={expected_through}")
    invalid_counts = sum(number(value) is None or number(value) < 0 for value in data["source_count"])
    audit.check("citation counts are non-negative", invalid_counts == 0, f"invalid={invalid_counts}")
    unexpected_types = sorted(set(data["source_type"]) - VALID_CITATION_TYPES)
    audit.check("all citation types have dashboard controls", not unexpected_types, "unexpected=" + ", ".join(unexpected_types))
    if manifest is not None:
        entries = manifest.get("race_files", [])
        manifest_races = tracked_races(entry.get("race", "") for entry in entries)
        audit.check("citation manifest ties to roster", manifest_races == roster_races, f"manifest={len(manifest_races)} roster={len(roster_races)}")
        invalid_entries = [entry for entry in entries if not entry.get("file") or int(entry.get("row_count", 0)) < 0]
        audit.check("citation manifest entries are usable", not invalid_entries, f"invalid={len(invalid_entries)}")


def audit_citation_first_paint(audit, citation, first_paint):
    """Ensure the compact Citation Breakout asset uses the 107-race browser universe.

    The browser hides a date range when this ledger calls it partial.  Checking the
    derived ledger against the full citation release prevents a non-tracked race
    from silently suppressing an otherwise complete range.
    """
    audit.check(
        "citation first-paint asset has expected category",
        first_paint.get("artifact_category") == "citation_breakout_first_paint_global",
        f"category={first_paint.get('artifact_category')!r}",
    )
    data = require_columns(citation, ["report_date", "race"])
    observed = defaultdict(set)
    first_seen = {}
    for date, race in zip(data["report_date"], data["race"]):
        race = canonical_race(race)
        if race in EXCLUDED_RACES:
            continue
        observed[date].add(race)
        if race not in first_seen or date < first_seen[race]:
            first_seen[race] = date
    expected_ledger = []
    for date in sorted(observed):
        expected = {race for race, first_date in first_seen.items() if first_date <= date}
        present = observed[date]
        missing = len(expected - present)
        expected_ledger.append({
            "date": date,
            "status": "partial" if missing else "complete",
            "observed_race_count": len(present),
            "expected_race_count": len(expected),
            "missing_race_count": missing,
        })
    actual_ledger = first_paint.get("race_continuity")
    audit.check(
        "citation first-paint continuity ledger ties to 107 tracked races",
        actual_ledger == expected_ledger,
        f"expected_dates={len(expected_ledger)} actual_dates={len(actual_ledger) if isinstance(actual_ledger, list) else 'invalid'}",
    )


def snapshot_share(entries, dimension):
    buckets, total = defaultdict(float), 0.0
    for metric, day_indexes in entries:
        for day_index in day_indexes:
            cell = metric["dailyBucketTotals"][dimension][day_index]
            total += cell["total"]
            for key, value in cell["buckets"].items():
                buckets[key] += value
    if not total:
        return None
    if dimension == "favorability":
        return 50 + 50 * (buckets["pos"] - buckets["neg"]) / total
    return 100 * buckets["1" if dimension == "visibility" else "2"] / total


def snapshot_summary(entries):
    values = {dimension: snapshot_share(entries, dimension) for dimension in SNAPSHOT_DIMS}
    known = [(values[dimension], SNAPSHOT_WEIGHTS[dimension]) for dimension in SNAPSHOT_DIMS if values[dimension] is not None]
    values["composite"] = sum(value * weight for value, weight in known) / sum(weight for _, weight in known) if known else None
    return values


def format_snapshot(values):
    return ", ".join(f"{key}={values[key]:.1f}" for key in (*SNAPSHOT_DIMS, "composite") if values[key] is not None)


def audit_cross_workstream_snapshot_views(audit, bootstrap):
    """Validate the Aggregate chart's three reference snapshots from its compact input.

    The page's kickoff mode intentionally combines the first observed checkpoint per race; it is
    not a global Aug. 31 filter. These checks keep that distinction durable in every refresh.
    """
    dates, roster, metrics = bootstrap.get("dates", []), bootstrap.get("roster", []), bootstrap.get("metrics", {})
    date_index = {date: index for index, date in enumerate(dates)}
    if not dates or not roster:
        audit.check("cross-workstream snapshot inputs are available", False, "missing dates or roster")
        return

    entities = []
    for index, row in enumerate(roster):
        if canonical_race(row.get("race", "")) in EXCLUDED_RACES:
            continue
        for side, party_field, key_prefix in (("candidate", "candidate_party_affiliation", "c"), ("opponent", "opponent_party_affiliation", "o")):
            metric = metrics.get(f"{key_prefix}{index}")
            if metric:
                entities.append((side, row.get(party_field), metric))
    candidate_metrics = [metric for side, _party, metric in entities if side == "candidate"]
    kickoff_days = []
    for metric in candidate_metrics:
        history = metric.get("history", {}).get("visibility", [])
        first = next((index for index, value in enumerate(history) if value is not None), None)
        if first is not None:
            kickoff_days.append(first)
    audit.check("cross-workstream kickoff covers 107 tracked races", len(kickoff_days) == 107, f"races={len(kickoff_days)}")
    audit.check(
        "cross-workstream kickoff uses only observed first checkpoints",
        bool(kickoff_days) and all(index < len(dates) for index in kickoff_days),
        "dates=" + ", ".join(sorted({dates[index] for index in kickoff_days})),
    )

    cohorts = {
        "kickoff": lambda metric: [next(index for index, value in enumerate(metric["history"]["visibility"]) if value is not None)],
        "2026-10-11": lambda metric: [date_index["2026-10-11"]],
        "2026-10-05 through 2026-10-11": lambda metric: list(range(date_index["2026-10-05"], date_index["2026-10-11"] + 1)),
    }
    required_dates = {"2026-10-05", "2026-10-11"}
    audit.check("reference date snapshots are present", required_dates <= set(date_index), f"missing={sorted(required_dates - set(date_index))}")
    for label, select_days in cohorts.items():
        if label != "kickoff" and not required_dates <= set(date_index):
            continue
        summaries = {}
        for party, expected in (("Democrats", "Democrat"), ("Republicans", "Republican")):
            entries = [(metric, select_days(metric)) for _side, entity_party, metric in entities if entity_party == expected]
            summaries[party] = snapshot_summary(entries)
        audit.check(
            f"Aggregate {label} snapshot has all five party dimensions",
            all(summary[dimension] is not None for summary in summaries.values() for dimension in SNAPSHOT_DIMS),
            "; ".join(f"{party}: {format_snapshot(summary)}" for party, summary in summaries.items()),
        )


def audit_dashboard_contract(audit, dashboard):
    if not dashboard:
        return
    text = Path(dashboard).read_text(encoding="utf-8")
    checks = {
        "score bootstrap contract supports published release labels": "score_bootstrap_compact_metrics" in text and "supportedBootstrapCategories" in text,
        "Aggregate supports an explicit cross-workstream kickoff": "setOverviewSnapshotMode" in text and "All-race kickoff" in text and "overviewKickoffDayIdx" in text,
        "107-race exclusion is implemented": "EXCLUDED_SCORE_RACES" in text and "LA Mayor" in text,
        "race aliases are canonicalized": "canonicalScoreRace" in text and "New Hanover-Pender" in text,
        "invalid buckets are rejected": "isValidBucketScore" in text,
        "citation controls use one filter function": "function filterCitationEvents(events)" in text,
        "citation first paint requests the published release asset": "citation_breakout_first_paint.json" in text and "citation_breakout_first_paint_v2.json" not in text,
        "dimension comparison passes date cohort": "const cmpRange = dateRangeIdx(key+'-cmp')" in text,
        "prompt-level rollups de-duplicate": "seenPromptLevel" in text,
        "Site Analytics chart controls stay isolated": "sa-chart-filter" in text,
    }
    for name, passed in checks.items():
        audit.check(name, passed, "present" if passed else "required marker missing")


def _is_iso_date(value):
    return isinstance(value, str) and bool(DATE_RE.fullmatch(value))


def audit_metric_refresh_status(audit, source, *, expected_through=None):
    """Require an explicit disposition for every dashboard metric family.

    A citation-only delivery must never be described as a full dashboard refresh by
    omission.  Metrics that cannot refresh (for example, Share of Voice without
    new raw responses) are valid only when the release records their last measured
    date, the freshest available input date, and the concrete reason.
    """
    raw, _ = load_json(source)
    audit.check(
        "metric freshness manifest uses the explicit release contract",
        isinstance(raw, dict) and raw.get("schema_version") == 2,
        f"schema_version={raw.get('schema_version') if isinstance(raw, dict) else None}",
    )
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        return raw
    requested_through = raw.get("requested_through")
    audit.check(
        "metric freshness manifest declares requested through date",
        _is_iso_date(requested_through),
        f"requested_through={requested_through!r}",
    )
    if expected_through:
        audit.check(
            "metric freshness target matches release endpoint",
            requested_through == expected_through,
            f"manifest={requested_through!r}; release={expected_through!r}",
        )
    required_updated = raw.get("required_updated")
    valid_required_updated = (
        isinstance(required_updated, list)
        and bool(required_updated)
        and all(isinstance(name, str) for name in required_updated)
        and len(required_updated) == len(set(required_updated))
        and set(required_updated) <= REQUIRED_METRIC_STATUS
    )
    audit.check(
        "metric freshness manifest declares required refreshed families",
        valid_required_updated,
        f"required_updated={required_updated!r}",
    )
    metrics = raw.get("metrics") if isinstance(raw, dict) else None
    audit.check("metric freshness manifest has a metrics registry", isinstance(metrics, dict), f"source={source}")
    if not isinstance(metrics, dict):
        return raw
    missing = sorted(REQUIRED_METRIC_STATUS - set(metrics))
    extra = sorted(set(metrics) - REQUIRED_METRIC_STATUS)
    audit.check("metric freshness manifest covers every dashboard metric family", not missing, f"missing={missing}; extra={extra}")
    for name in sorted(REQUIRED_METRIC_STATUS & set(metrics)):
        row = metrics[name]
        valid = isinstance(row, dict) and row.get("state") in VALID_METRIC_STATES
        audit.check(f"metric freshness status is valid: {name}", valid, f"state={row.get('state') if isinstance(row, dict) else None}")
        if not valid:
            continue
        if valid_required_updated and name in required_updated:
            audit.check(
                f"required metric is explicitly refreshed: {name}",
                row["state"] == "updated",
                f"state={row['state']!r}; requested={requested_through}",
            )
        if row["state"] == "updated":
            complete = all(
                isinstance(row.get(field), str) and row.get(field)
                for field in ("measured_through", "available_input_through", "release_asset", "source_completeness")
            )
            complete = complete and row.get("source_completeness") in VALID_SOURCE_COMPLETENESS
            if row.get("source_completeness") == "complete_with_declared_partial_dates":
                partial_dates = row.get("declared_partial_dates")
                complete = complete and isinstance(partial_dates, list) and bool(partial_dates) and all(
                    _is_iso_date(value) for value in partial_dates
                )
            if row.get("source_completeness") == "complete_with_approved_exclusions":
                exclusions = row.get("approved_exclusions")
                complete = complete and (
                    isinstance(exclusions, dict)
                    and exclusions.get("reason") == "candidate_scope_validation_failure"
                    and isinstance(exclusions.get("count"), int)
                    and 1 <= exclusions["count"] <= 3
                    and isinstance(exclusions.get("cohort_pairs_before"), int)
                    and isinstance(exclusions.get("cohort_pairs_after"), int)
                    and exclusions["cohort_pairs_after"] + exclusions["count"] == exclusions["cohort_pairs_before"]
                )
            audit.check(
                f"updated metric has dated evidence: {name}", complete,
                f"measured={row.get('measured_through')}; available={row.get('available_input_through')}; "
                f"asset={row.get('release_asset')!r}; completeness={row.get('source_completeness')!r}",
            )
            if valid_required_updated and name in required_updated:
                audit.check(
                    f"required metric reaches release endpoint: {name}",
                    row.get("measured_through") == requested_through,
                    f"measured={row.get('measured_through')}; requested={requested_through}",
                )
        if row["state"] in {"retained", "blocked"}:
            complete = all(isinstance(row.get(field), str) and row.get(field) for field in ("reason", "last_measured_through", "available_input_through"))
            audit.check(
                f"stale metric has an explicit exception: {name}", complete,
                f"last={row.get('last_measured_through')}; available={row.get('available_input_through')}; reason={row.get('reason')!r}",
            )
    return raw


def owned_domains_are_refreshed(metric_status):
    """Whether this release actually rebuilt one or more owned-domain KPIs."""
    metrics = metric_status.get("metrics", {}) if isinstance(metric_status, dict) else {}
    return any(
        isinstance(metrics.get(name), dict) and metrics[name].get("state") == "updated"
        for name in OWNED_DOMAIN_METRIC_FAMILIES
    )


def audit_partner_roster_preflight(audit, source, *, required):
    """Block an owned-domain refresh without the newly supplied partner rosters.

    The roster audit is deliberately separate from the release asset: it proves
    that the current Courier and Heartland committed-article exports were supplied
    and reconciled before project denominators or topic coverage were rebuilt.
    """
    if not required:
        audit.check(
            "partner roster preflight is present when Owned Domains refreshes",
            source is None or isinstance(source, str),
            "not required because every Owned Domains metric is retained, blocked, or not applicable",
            blocking=False,
        )
        return
    audit.check(
        "partner roster preflight supplied for Owned Domains refresh",
        bool(source),
        "missing --partner-roster-preflight",
    )
    if not source:
        return
    raw, _ = load_json(source)
    valid_shape = isinstance(raw, dict) and raw.get("status") == "pass" and isinstance(raw.get("inputs"), dict)
    audit.check(
        "partner roster preflight passed",
        valid_shape,
        f"status={raw.get('status') if isinstance(raw, dict) else None!r}",
    )
    if not valid_shape:
        return
    inputs = raw["inputs"]
    supplied = all(isinstance(inputs.get(field), str) and len(inputs[field]) == 64 for field in ("courier_sha256", "heartland_sha256"))
    audit.check(
        "partner roster preflight records immutable workbook fingerprints",
        supplied,
        f"courier_sha256={inputs.get('courier_sha256')!r}; heartland_sha256={inputs.get('heartland_sha256')!r}",
    )
    publishers = raw.get("publishers")
    by_name = {row.get("publisher"): row for row in publishers} if isinstance(publishers, list) and all(isinstance(row, dict) for row in publishers) else {}
    audit.check(
        "partner roster preflight covers Courier and Heartland Signal",
        set(by_name) == {"Courier", "Heartland Signal"},
        f"publishers={sorted(by_name)}",
    )
    for publisher in ("Courier", "Heartland Signal"):
        row = by_name.get(publisher, {})
        linked, registered = row.get("linked_project_rows"), row.get("registry_rows")
        audit.check(
            f"partner roster rows reconcile: {publisher}",
            isinstance(linked, int) and linked > 0 and linked == registered,
            f"linked={linked}; registry={registered}",
        )


def audit_dossier_writeback(audit, source):
    """Require an explicit dossier disposition for every score release.

    A score refresh may legitimately contain no newly adjudicated provider research,
    but that is a recorded decision, not a reason to silently omit the dossier
    stage. Applied updates carry their review provenance; otherwise, only the
    narrowly defined no-new-research disposition can pass.
    """
    audit.check(
        "dossier write-back receipt supplied",
        bool(source),
        "missing --dossier-writeback-receipt",
    )
    if not source:
        return
    raw, _ = load_json(source)
    valid_shape = (
        isinstance(raw, dict)
        and raw.get("schema_version") == 1
        and raw.get("artifact_category") == "dossier_writeback_receipt"
        and raw.get("status") in {"applied", "not_applicable_no_new_adjudicated_research"}
        and raw.get("local_output_writes") is False
        and isinstance(raw.get("source"), dict)
        and isinstance(raw["source"].get("aggregate_manifest"), dict)
        and isinstance(raw["source"]["aggregate_manifest"].get("object_name"), str)
        and bool(raw["source"]["aggregate_manifest"]["object_name"])
    )
    audit.check(
        "dossier write-back receipt has a supported release contract",
        valid_shape,
        f"status={raw.get('status') if isinstance(raw, dict) else None!r}",
    )
    if not valid_shape:
        return
    entries = raw.get("entries")
    if raw["status"] == "applied":
        valid_entries = (
            isinstance(entries, list)
            and bool(entries)
            and all(
                isinstance(entry, dict)
                and all(isinstance(entry.get(field), str) and entry[field].strip()
                        for field in ("dossier_path", "research_artifact", "summary"))
                for entry in entries
            )
        )
        audit.check(
            "applied dossier write-back records source-backed entries",
            valid_entries,
            f"entries={len(entries) if isinstance(entries, list) else None}",
        )
    else:
        valid_no_research = (
            entries == []
            and raw.get("accuracy_status") == "partial_unadjudicated"
            and isinstance(raw.get("reason"), str)
            and bool(raw["reason"].strip())
        )
        audit.check(
            "no-research dossier disposition is explicit and bounded",
            valid_no_research,
            f"accuracy_status={raw.get('accuracy_status')!r}; entries={entries!r}",
        )


def audit_score_release_availability(audit, source):
    """Check that a partial Accuracy refresh cannot masquerade as complete in the release gate."""

    raw, _ = load_json(source)
    audit.check(
        "score release manifest has a supported availability contract",
        isinstance(raw, dict) and raw.get("artifact_category") == "dashboard_score_release",
        f"source={source}",
    )
    if not isinstance(raw, dict) or raw.get("artifact_category") != "dashboard_score_release":
        return
    status = raw.get("accuracy_status")
    audit.check(
        "score release declares Accuracy availability",
        status in {"complete", "partial_unadjudicated"},
        f"accuracy_status={status!r}",
    )
    if status == "partial_unadjudicated":
        unadjudicated = raw.get("accuracy_unadjudicated")
        audit.check(
            "partial Accuracy release retains unadjudicated accounting",
            isinstance(unadjudicated, dict) and bool(unadjudicated),
            f"races={len(unadjudicated) if isinstance(unadjudicated, dict) else 0}",
        )


def _record_contract(record):
    """Return whether an immutable public-object record has the required pin fields."""

    return (
        isinstance(record, dict)
        and isinstance(record.get("object_name"), str)
        and bool(record["object_name"])
        and isinstance(record.get("generation"), (str, int))
        and isinstance(record.get("bytes"), int)
        and record["bytes"] >= 0
        and isinstance(record.get("sha256"), str)
        and len(record["sha256"]) == 64
    )


def audit_owned_domains_release(audit, source, *, required, expected_through):
    """Ensure every refreshed Owned Domains KPI has one pinned source release.

    Metric-freshness declarations alone are not enough: this checks the actual
    public release manifest, its complete-snapshot boundary, and the four
    browser assets that supply coverage, on-topic, yield/rank, and quality.
    """

    audit.check(
        "Owned Domains release supplied when its metrics are refreshed",
        bool(source) if required else source is None or isinstance(source, str),
        "missing --owned-domain-release" if required else "not required",
    )
    if not source:
        return
    raw, _ = load_json(source)
    valid = (
        isinstance(raw, dict)
        and raw.get("artifact_category") == "owned_domains_dashboard_release_manifest"
        and isinstance(raw.get("sources"), dict)
        and isinstance(raw.get("records"), dict)
    )
    audit.check(
        "Owned Domains release manifest has a supported contract",
        valid,
        f"artifact_category={raw.get('artifact_category') if isinstance(raw, dict) else None!r}",
    )
    if not valid:
        return
    complete = raw.get("latest_complete_citation_snapshot")
    audit.check(
        "Owned Domains release reaches the requested complete snapshot",
        isinstance(complete, str) and (not expected_through or complete == expected_through),
        f"latest_complete={complete!r}; expected={expected_through!r}",
    )
    source_records = raw["sources"]
    audit.check(
        "Owned Domains release pins score, citation, and citation-detail sources",
        all(_record_contract(source_records.get(name)) for name in ("citation_release", "score_release", "citation_detail_release", "score_aggregate")),
        f"sources={sorted(source_records)}",
    )
    records = raw["records"]
    required_assets = {
        "owned_domains_release.json",
        "owned_domains_detail.json",
        "owned_domains_quality.json",
        "owned_domains_audit.json",
    }
    audit.check(
        "Owned Domains release includes every browser and audit asset",
        required_assets <= set(records) and all(_record_contract(records.get(name)) for name in required_assets),
        f"missing={sorted(required_assets - set(records))}",
    )


def audit_owned_voice_release(audit, source, *, required, expected_through):
    """Block a deployment that marks Share of Voice refreshed without its release."""

    audit.check(
        "Share of Voice release supplied when Share of Voice is refreshed",
        bool(source) if required else source is None or isinstance(source, str),
        "missing --owned-voice-release" if required else "not required",
    )
    if not source:
        return
    raw, _ = load_json(source)
    valid = (
        isinstance(raw, dict)
        and raw.get("artifact_category") == "owned_voice_release_manifest"
        and raw.get("not_accuracy_or_causality") is True
        and raw.get("raw_answer_storage") == "restricted_source_bucket_only"
        and _record_contract(raw.get("owned_release"))
        and isinstance(raw.get("public"), dict)
        and isinstance(raw.get("private_audit"), dict)
    )
    audit.check(
        "Share of Voice release manifest has a supported scope contract",
        valid,
        f"artifact_category={raw.get('artifact_category') if isinstance(raw, dict) else None!r}",
    )
    if not valid:
        return
    audit.check(
        "Share of Voice release reaches the requested date",
        isinstance(raw.get("date"), str) and (not expected_through or raw["date"] == expected_through),
        f"date={raw.get('date')!r}; expected={expected_through!r}",
    )
    public = raw["public"]
    audit.check(
        "Share of Voice release includes pinned summary and detail assets",
        all(_record_contract(public.get(name)) for name in ("summary", "detail")),
        f"public={sorted(public)}",
    )
    if _record_contract(public.get("summary")):
        summary_source = "gs://aeo-dashboard-assets-bullhorn/" + public["summary"]["object_name"]
        summary, _ = load_json(summary_source)
        exclusions = (
            summary.get("source", {}).get("candidate_scope_exclusions")
            if isinstance(summary, dict) and isinstance(summary.get("source"), dict)
            else None
        )
        if exclusions is not None:
            valid_exclusions = (
                isinstance(exclusions, dict)
                and exclusions.get("reason") == "candidate_scope_validation_failure"
                and isinstance(exclusions.get("excluded_pair_count"), int)
                and 1 <= exclusions["excluded_pair_count"] <= 3
                and isinstance(exclusions.get("cohort_pairs_before"), int)
                and isinstance(exclusions.get("cohort_pairs_after"), int)
                and exclusions["cohort_pairs_after"] + exclusions["excluded_pair_count"] == exclusions["cohort_pairs_before"]
            )
            audit.check(
                "Share of Voice approved exclusions are bounded and declared",
                valid_exclusions,
                f"exclusions={exclusions!r}",
            )
    private = raw["private_audit"]
    required_audits = {"raw_answer_evidence.json", "candidate_scope.json", "source_scope.json", "openai_support.json", "structural_audit.json", "chart_audit.json"}
    audit.check(
        "Share of Voice release retains the required private audit trail",
        required_audits <= set(private) and all(_record_contract(private.get(name)) for name in required_audits),
        f"missing={sorted(required_audits - set(private))}",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-aggregate", required=True)
    parser.add_argument("--score-bootstrap", required=True)
    parser.add_argument("--citation-summary", required=True)
    parser.add_argument("--citation-manifest")
    parser.add_argument("--citation-first-paint", help="Compact all-race Citation Breakout asset; validates its continuity ledger against the 107 tracked races.")
    parser.add_argument("--score-release-manifest", help="Immutable dashboard score-release manifest; makes Accuracy availability explicit in the release audit.")
    parser.add_argument("--expected-through")
    parser.add_argument("--metric-status", required=True, help="Versioned metric-freshness manifest. Every metric family must be marked updated, retained, blocked, or not applicable.")
    parser.add_argument("--partner-roster-preflight", help="Passed Courier/Heartland committed-article reconciliation. Required whenever an Owned Domains metric is refreshed.")
    parser.add_argument("--dossier-writeback-receipt", required=True, help="Required post-aggregate disposition for source-backed dossier write-back.")
    parser.add_argument("--owned-domain-release", help="Immutable Owned Domains public release manifest. Required whenever an Owned Domains metric is refreshed.")
    parser.add_argument("--owned-voice-release", help="Immutable public Share of Voice release manifest. Required when Share of Voice is refreshed.")
    parser.add_argument("--dashboard", default="index.html")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    audit = Audit()
    try:
        score, score_hash = load_json(args.score_aggregate)
        bootstrap, _ = load_json(args.score_bootstrap)
        citation, _ = load_json(args.citation_summary)
        manifest = load_json(args.citation_manifest)[0] if args.citation_manifest else None
        citation_first_paint = load_json(args.citation_first_paint)[0] if args.citation_first_paint else None
        races, dates = audit_scores(audit, score, score_hash, bootstrap)
        audit_cross_workstream_snapshot_views(audit, bootstrap)
        audit_citations(audit, citation, manifest, races, dates, args.expected_through)
        if citation_first_paint is not None:
            audit_citation_first_paint(audit, citation, citation_first_paint)
        if args.score_release_manifest:
            audit_score_release_availability(audit, args.score_release_manifest)
        audit_dashboard_contract(audit, args.dashboard)
        metric_status = audit_metric_refresh_status(audit, args.metric_status, expected_through=args.expected_through)
        audit_dossier_writeback(audit, args.dossier_writeback_receipt)
        audit_partner_roster_preflight(
            audit,
            args.partner_roster_preflight,
            required=owned_domains_are_refreshed(metric_status),
        )
        audit_owned_domains_release(
            audit,
            args.owned_domain_release,
            required=owned_domains_are_refreshed(metric_status),
            expected_through=args.expected_through,
        )
        voice_updated = (
            isinstance(metric_status.get("metrics"), dict)
            and isinstance(metric_status["metrics"].get("owned_domain_share_of_voice"), dict)
            and metric_status["metrics"]["owned_domain_share_of_voice"].get("state") == "updated"
        )
        audit_owned_voice_release(
            audit,
            args.owned_voice_release,
            required=voice_updated,
            expected_through=args.expected_through,
        )
    except Exception as exc:
        audit.check("audit execution", False, f"{type(exc).__name__}: {exc}")
    report = audit.payload()
    destination = Path(args.report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Dashboard release audit: {report['status']} — {len(audit.checks)} checks; {len(audit.errors)} blocking failures; {len(audit.warnings)} warnings")
    for error in audit.errors:
        print("FAIL:", error)
    print("Report:", destination)
    return 1 if audit.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
