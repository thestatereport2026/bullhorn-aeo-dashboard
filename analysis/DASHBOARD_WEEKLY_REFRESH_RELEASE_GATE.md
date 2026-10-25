# Weekly dashboard refresh release gate

Run the release gate after producing the score bootstrap and citation release, before updating
dashboard release pointers or deploying `index.html`. A refresh is not finished until the command
exits successfully and its JSON report is saved alongside that release's audit artifacts.

```sh
python3 scripts/dashboard_release_audit.py \
  --score-aggregate 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/score-releases/<release>/prompt_results_agg.json' \
  --score-bootstrap 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/score-bootstrap-releases/<release>/score_bootstrap.json' \
  --citation-summary 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/citation-releases/<release>/citations.json' \
  --citation-manifest 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/citation-releases/<release>/citation_races/manifest.json' \
  --expected-through YYYY-MM-DD \
  --dashboard index.html \
  --report 'analysis/dashboard_refresh_audits/YYYY-MM-DD_<release>.json'
```

The gate blocks a release when any of these fail:

- The intended 107-race universe is not identical in the roster, full score release, citation
  summary, and citation manifest; LA Mayor must remain excluded.
- A known vendor race-label alias is not reconciled, or a score/citation race becomes orphaned.
- Score/bootstrap date coverage differs, citation dates are malformed, or either delivery does not
  reach its requested endpoint. Citation and score sampling days are checked independently because
  they are separate deliveries and need not be identical.
- The compact bootstrap is not cryptographically tied to the full raw score release.
- Any candidate/opponent base metric differs from the full score corpus in either dashboard mode:
  standard visibility or **include refusals** visibility.
- Prompt-level visibility is inconsistent across samples/providers within a prompt-date cell.
- A new malformed score bucket appears, a citation count is invalid, a citation source type lacks a
  dashboard control, or a citation manifest entry is unusable.
- Required dashboard protections for canonical races, invalid-score exclusion, prompt-level
  deduplication, date/prompt cohort propagation, citation filtering, and local Site Analytics
  filters are missing from the build.

The current documented malformed vendor score remains explicit in the audit allow-list and is
reported as a warning. Any new anomaly blocks the release; it cannot be silently ignored. On a
failure, fix the asset, pipeline, or UI contract and rerun the release gate before calling the
refresh finished.
