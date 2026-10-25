#!/usr/bin/env python3
"""Lightweight regression guard for dashboard filter propagation.

This does not replace browser QA. It guards the easy-to-reintroduce class of
defect where a filter is rendered and listens for changes but its value is not
passed into the aggregation path.
"""

from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "index.html"


def section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def require(haystack: str, needle: str, label: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"{label}: missing {needle!r}")


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    filters = section(source, "function dimFilters", "function nearestDateIdx")
    require(filters, "blind:value('blind')", "dimension filter reader")

    lazy_load = section(source, "async function ensureDimensionPromptTypeData", "async function renderDimensionPage")
    require(lazy_load, "section+'-blind'", "prompt-type detailed-data gate")
    require(lazy_load, "await ensureFullScoreData()", "prompt-type detailed-data gate")

    bucket = section(source, "function renderBucket3Charts", "function renderDimTable")
    for token in (
        "[cmpI0,cmpI1],fCmp.blind",
        "[dayIdx,dayIdx], fTrend.blind",
        "dailyBucketShare(e.id,key,i0+ix,m,fTrend.blind)",
        "dailyBucketShare(o.id,key,i0+ix,null,fTrend.blind)",
    ):
        require(bucket, token, "bucket comparison/trend propagation")

    insights = section(source, "function bucketAndCategoryInsights", "function computeAccuracyIssueRows")
    require(insights, "[i0,i1],f.blind", "dimension insight scope")

    accuracy = section(source, "function computeAccuracyIssueRows", "function buildAccuracyInsights")
    require(accuracy, "dateRangeIdx('accuracy-cmp')", "accuracy detail date scope")
    require(accuracy, "p.is_blind!==f.blind", "accuracy detail prompt-type scope")

    citations = section(source, "function filterCitationEvents", "function selectedCitationRaceName")
    for token in ("citationEventWorkstream(e)", "e.raceLevel", "e.office", "e.state", "e.promptCategory", "e.topic", "e.date"):
        require(citations, token, "citation breakout filter propagation")

    owned = section(source, "function odFilteredPrompts", "function odFilteredCoverage")
    for token in ("row.workstream", "row.race", "row.candidate_id", "row.topic_group", "row.prompt_id"):
        require(owned, token, "owned-domain prompt filter propagation")

    print("Dashboard filter contract audit passed.")


if __name__ == "__main__":
    main()
