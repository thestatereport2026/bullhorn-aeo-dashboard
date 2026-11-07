# Weekly dashboard refresh release gate

Run the release gate after producing the score bootstrap and citation release, before updating
dashboard release pointers or deploying `index.html`. A refresh is not finished until the command
exits successfully and its JSON report is saved alongside that release's audit artifacts.

Every refresh also needs a versioned **metric-freshness manifest**. It is an explicit release
contract, not a retrospective note: it names the requested endpoint, the metric families that
must be refreshed for this release, and a disposition for every family: `updated`, `retained`,
`blocked`, or `not_applicable`. This explicitly
includes citation footprint/coverage, **on-topic citation rate**, citation yield, peer rank,
answer quality, and Share of Voice. A retained or blocked
metric must name its last measured date, freshest available input date, and reason. This makes a
citation-only refresh explicit: Share of Voice, answer quality, and score dimensions may remain
at their last valid date when no newer raw scored responses are delivered, but they may never be
silently omitted from the release decision.

## Local workspace hygiene

Treat the workstation as an operator console, not a data warehouse. Raw vendor archives, response
cohorts, model inputs, browser downloads, and derived release payloads must remain in the approved
cloud buckets whenever the workflow supports it. If a release step necessarily creates a local
scratch file, it must use a uniquely named bounded temporary directory, record only the immutable
cloud object identities in its receipt, and remove the scratch directory after the step succeeds or
fails. Do not delete source code, checked-in fixtures, the final immutable release receipts, or a
user's explicitly retained review files. Before closeout, confirm that no task-created temporary
directories or generated raw-response copies remain on the local disk.

## Mandatory refresh ledger

Treat a requested scoring and dashboard refresh as one release, not as a score update with
optional dashboard follow-ups. Before any run begins, create a source-and-metric ledger for all
families below and ask the operator for any required delivery that is missing. At closeout, every
row must be marked `updated`, `retained`, or `blocked` in the metric-freshness manifest. A
retained or blocked row must name the last measured-through date, freshest source date, and its
reason.

| Required family | Preflight requirement | If unavailable |
| --- | --- | --- |
| Score dimensions, aggregate, and prompt diagnostics | Complete score/raw-response delivery for the requested window | Block a full scoring release; never treat an absent response as zero. |
| Accuracy | Approved adjudication scope and current completion status | Carry `partial_unadjudicated` when appropriate; never imply completion. |
| Citation Breakout | Citation-delivery ledger and race partitions | Declare partial dates explicitly; never render them as zero. |
| Owned Domains - footprint, coverage, on-topic rate, yield, peer rank, answer quality | Citation release, reviewed crosswalk, partner roster preflight, and score asset where needed | Retain or block the affected metric family with dated provenance. |
| Owned Domains - Share of Voice | Independently verified complete raw answer and citation-pair delivery for the requested snapshot | Retain the prior snapshot and explain why; do not derive it from citation counts. |
| Courier and Heartland Signal project portfolios | Latest committed-article workbooks reconciled to registry and crosswalk | Block partner metrics until reconciliation passes. |
| Dossier write-back and dashboard tie-out | Accepted aggregate, write-back receipt, release-audit output, and browser checks | Block deployment until the missing item is completed or formally declared unavailable. |

For an `updated` metric, the manifest must name the measured-through date, freshest available
input date, immutable release asset, and source-completeness state. Every family in
`required_updated` must reach the release endpoint. A citation release that deliberately omits a
partial day must name it in `declared_partial_dates`; that condition is never represented as a
zero. For a full scoring and Owned Domains release, `required_updated` must include the score
dimensions, Citation Breakout, all five Owned Domains families, and **Share of Voice**. The gate
blocks deployment if any of them is merely retained, blocked, or not dated to the requested
endpoint.

Minimal shape:

```json
{
  "schema_version": 2,
  "requested_through": "YYYY-MM-DD",
  "required_updated": ["score_dimensions_and_aggregate", "citation_breakout", "owned_domain_share_of_voice"],
  "metrics": {
    "owned_domain_share_of_voice": {
      "state": "updated",
      "measured_through": "YYYY-MM-DD",
      "available_input_through": "YYYY-MM-DD",
      "source_completeness": "complete",
      "release_asset": "gs://.../owned_voice/.../release_manifest.json"
    }
  }
}
```

## Required upstream refresh checks

The release gate is the last check, not the only check. The scoring refresh must record the
following receipts before a dashboard candidate can be prepared:

- **Delivery completeness:** independently reconcile the delivered raw files to the requested
  race, provider, prompt, sample, and report-date universe. A vendor manifest may be a useful
  secondary reference but is not sufficient on its own and is not required when the raw-file
  audit produces the same evidence. Treat a missing or partial delivery as unavailable, never as
  a zero. The receipt must name any unavailable date and exclude it from daily completeness
  claims.
- **Stage matrix:** validate every expected race/provider slice has `joined_rows`, refusal,
  response-hypothesis, visibility, depth, narrative, namesake, and Accuracy Step 1 state. A
  skipped dimension is a blocking error, even where another page could technically render.
- **Accuracy scope:** record the approved Accuracy sample/research scope before execution. If
  additional research is not authorized, unresolved claims remain `null` and the aggregate and
  dashboard candidate must carry `accuracy_status: partial_unadjudicated`. They must never
  trigger a Tier 1 or Tier 2 batch implicitly, or be shown as a complete Accuracy refresh.
- **Prompt diagnostics:** refresh the prompt diagnostics asset from the same score release, or
  mark it retained with its last measured date and a reason in the metric-freshness manifest.
- **Dossier write-back:** immediately after the aggregate succeeds, run
  `scripts/pipeline/gcs_dossier_writeback_receipt.py` against that immutable aggregate. If the
  refresh contains newly adjudicated research, first apply a reviewed, append-only dossier update
  and provide a manifest naming every dossier path, research artifact, and summary. If no new
  adjudicated research was in scope, the script can issue only the explicit
  `not_applicable_no_new_adjudicated_research` receipt. The release audit requires this receipt,
  so a completed research batch cannot be deployed while its findings remain only in transient
  scoring artifacts.
- **Partner project rosters:** before any Courier or Heartland Signal KPI is rebuilt, explicitly
  ask the operator for the newest committed-article exports from both publishers. Reconcile the
  supplied URLs against the confirmed-project registry and reviewed topic crosswalk with
  `scripts/dashboard/audit_partner_project_rosters.py`. A missing export, an unregistered
  committed URL, or a URL without a reviewed crosswalk decision blocks the Owned Domains release;
  it may not be assumed from an older registry. Record the exact spreadsheet filenames, hashes,
  row counts, and reconciliation receipt in the release manifest.
- **Owned-domain suite:** run the owned-domain, citation, and Site Analytics tie-outs. Confirm
  each owned-domain chart uses its declared current snapshot, selected denominators, and the
  appropriate source asset. Before Share of Voice runs, create a cloud-only raw-answer audit that
  validates the snapshot's raw files, dates, provider coverage, exact-row deduplication, and
  cited-answer pairing against the scoring and citation universes. **If that audit passes, run
  the full current-snapshot Share of Voice release automatically** using the approved model and
  record its audited public manifest in the metric-freshness file. Share of Voice may be marked
  retained only when the independent audit cannot establish a complete eligible cohort; it may
  never be omitted or recomputed from citation-only data. Keep raw answers and model checkpoints
  in restricted cloud storage, and publish only the aggregate and permitted drilldown evidence.

```sh
python3 scripts/dashboard_release_audit.py \
  --score-aggregate 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/score-releases/<release>/prompt_results_agg.json' \
  --score-release-manifest 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/score-releases/<release>/release_manifest.json' \
  --score-bootstrap 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/score-bootstrap-releases/<release>/score_bootstrap.json' \
  --citation-summary 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/citation-releases/<release>/citation_summary.json' \
  --citation-manifest 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/citation-releases/<release>/citation_races/manifest.json' \
  --citation-first-paint 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/citation-releases/<release>/citation_breakout_first_paint.json' \
  --expected-through YYYY-MM-DD \
  --metric-status 'analysis/dashboard_metric_refresh_status_YYYY-MM-DD.json' \
  --dossier-writeback-receipt 'gs://aeo-project-data-backup/scoring-v2/runs/<run>/outputs/dossier_writeback/<receipt>.json' \
  --partner-roster-preflight '/tmp/partner_roster_preflight_YYYYMMDD.json' \
  --owned-domain-release 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/owned-domain-releases/<release>/release_manifest.json' \
  --owned-voice-release 'gs://aeo-dashboard-assets-bullhorn/dashboard-data/owned-voice-releases/<release>/release_manifest.json' \
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
- The compact Citation Breakout continuity ledger uses a different race universe than the browser.
  In particular, the deliberately excluded LA Mayor race must not mark a complete 107-race date
  range as partial and hide it when a reader applies a date filter.
- Required dashboard protections for canonical races, invalid-score exclusion, prompt-level
  deduplication, date/prompt cohort propagation, citation filtering, and local Site Analytics
  filters are missing from the build.
- An Updated Owned Domains or Share of Voice status points only to a stale receipt or a
  nonconforming artifact. The gate independently verifies the current Owned Domains manifest,
  the current Share of Voice manifest, their source pins, the measured-through date, protected
  raw-answer handling, and the corresponding public assets.

The current documented malformed vendor score remains explicit in the audit allow-list and is
reported as a warning. Any new anomaly blocks the release; it cannot be silently ignored. On a
failure, fix the asset, pipeline, or UI contract and rerun the release gate before calling the
refresh finished.
