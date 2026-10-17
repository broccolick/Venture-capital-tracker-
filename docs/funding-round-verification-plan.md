# Funding Round Resolution: Implemented Behavior and Verification

## Status

The funding-resolution changes in this branch are implemented and regression-tested. The last GitHub Actions run on the implementation commit completed with **122 Python tests passing**, and both discovery-engine and website jobs were green. CI will be rerun against the documentation update as well.

This document describes shipped behavior, not a proposal. The matcher remains a deterministic heuristic; it cannot guarantee that reports refer to the same financing event or that sources are truly independent.

## Implemented behavior

### Source observations and same-URL revisions

NewsArticle remains the latest view used by existing code. The new source_observations table records each material version of an article-like source record: revision number, source URL, title, short summary, source metadata, publication and retrieval timestamps, fingerprint, origin group, and source role. Full article bodies are not retained.

- Unchanged metadata updates the last-checked timestamp without creating another observation or citation.
- A material change at the same canonical URL creates a new observation and is reprocessed even if its title matches another article's fingerprint.
- Citations and discovery signals tied to the old version remain in the database but are flagged non-current and excluded from current profile lists.
- Funding observations are linked to source-observation versions. If a source version stops supporting a funding claim and no other current claim sustains it, the old round is excluded from current totals. If another source still supports the conflicting value, both claims remain visible as a conflict instead of silently overwriting one another.

### Funding matching, conflicts, and totals

The matcher compares company, round type, available date basis, amount, and currency. The event-matching window remains 60 days and the amount tolerance remains 10% when values can be compared. Missing or incompatible values are not treated as agreement; ambiguous matches remain separate and are marked for review.

Publication date is recorded as an explicit proxy when a reliable funding-event date is unavailable. Retrieval time is not substituted for an unknown date. Comparable conflicting amounts remain separate current rows with conflict metadata. While unresolved funding conflicts exist, the company-level total is suppressed instead of summing potentially contradictory or duplicate amounts. Current rounds and historical/superseded rounds are serialized separately for API and static-data consumers.

The numeric window and tolerance are heuristics, not proof of event identity. Manual review and better historical test fixtures remain important.

### Source-origin corroboration

Funding observations carry an origin group and source role where configured. Publisher labels remain attribution only; counting distinct publisher strings is no longer enough to promote a funding round to confirmed. The implemented policy uses distinct configured independent-reporting origins, does not count a same-URL revision as a second source, and allows a company/press-release statement plus a distinct independent reporting origin to qualify as corroboration. Profile-confidence corroboration only rewards multiple known origins supporting the same normalized field/value pair.

**Limitations:** origin and role classifications are manually configured for only part of the source registry and have not received a comprehensive audit. Unknown lineage remains unknown and receives no independent-source credit. Configured labels do not machine-verify corporate ownership, republication, or editorial independence; an article about a company statement is not automatic independent verification of that underlying metric.

### SEC Form D amendments

SEC filing records remain preserved. An amendment is linked to an existing current regulatory funding record only when there is exactly one candidate. When multiple candidates exist, the filing is preserved separately and marked needs_review with the candidate IDs rather than arbitrarily overwriting the newest row. An amendment's securities-sold amount remains regulatory filing evidence, not proof of a priced venture round, valuation, or named institutional investor participation; Form D records are excluded from ordinary raised-funding totals.

### Field-level enrichment provenance

Headline-inferred industry and location values are marked as estimates and cited with an explanation. Hacker News tagline descriptions are cited as community-sourced. Form D enrichment emits citations for applicable legal name, CIK, incorporation-year proxy, location, and broad industry mapping. Wikidata's existing fill-only enrichment adds citations when it populates fields.

This is not yet a complete evidence graph for every field throughout the product. The implementation covers the audited ingestion paths; other data sources and manual edits still need equivalent review.

## Regression coverage

The suite includes tests for unchanged re-fetches; headline and summary corrections at the same URL; evidence-history retention; revisions conflicting with another still-current report; withdrawal of stale latest-funding values; unknown origin metadata; corroboration of matching versus unrelated field/value claims; inferred location and industry citations; Hacker News description citations; Form D amendments with both unique and ambiguous targets; and existing funding, SEC, API, export, and website behavior.

## Known limits and next work

- Source-origin metadata needs a full audit; the current labels are policy inputs, not independently verified facts.
- Citation value text is a field/value evidence record, not a typed claim ontology. There is no comprehensive predicate registry, unit normalization, or canonical claim-assessment history.
- Confidence is deterministic and uncalibrated, not a probability of correctness.
- Corrections are detected when a changed source record is fetched. There is no general retraction-monitoring service or guarantee that deleted/inaccessible pages can be recovered.
- Form D amendment matching may conservatively leave valid amendments for manual review; it does not represent every issuer/offering relationship across all amendment scenarios.
- Historical funding observations are present in API/static data, but the public static interface does not yet provide a complete reviewer-facing history and conflict-resolution workflow.
- The SQL file at discovery-engine/docs/schema.sql is a schema snapshot. Runtime init-db creates missing tables and applies additive column migrations for existing databases; this is not a general-purpose migration/versioning framework.

## Acceptance standard

Continue to prefer an explicit conflict, unknown value, or reviewable candidate over silent destructive merges. Any change to this area should keep regression coverage for these failure cases and rerun discovery-engine and website CI before the implementation is considered complete.