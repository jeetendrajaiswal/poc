# Software taxonomy

`taxonomy.yaml` is a small deterministic manifest. It declares the ordered
field files plus the statement-structure and identity files used by the
software sector:

- `fields/income.yaml`
- `fields/balance.yaml`
- `fields/cashflow.yaml`
- `fields/segment.yaml`
- `statement_structure.yaml`
- `identities.yaml`

Every field file declares one `statement`; its items inherit that statement.
The loader rejects missing references, paths outside this sector directory,
duplicate statement files, embedded per-item statement overrides, and
inconsistent statement names.

Each field defines:

- `fid`: stable client field identifier.
- `name`: client-facing display name.
- statement membership: inherited from the containing field file.
- `value_type`: amount, percentage, count, per_share, or text.
- `unit`: statement_currency, percent, shares/count, currency_per_share, or
  text; validated against `value_type`.
- `time_nature`: point_in_time, duration, period_average, or
  context_dependent.
- `definition`: a machine-validated concept contract:
  - `meaning`: one concise economic meaning;
  - `includes` and `excludes`: explicit semantic boundaries;
  - `mapping_notes`: field-specific interpretation notes;
  - `distinguish_from`: FIDs with overlapping reviewed vocabulary;
- `mapping.aliases`: reviewed report labels for deterministic matching.
- `mapping.locations`: statement locations in which those labels are valid,
  such as current assets, current liabilities, operating, investing, or
  financing. Asset and liability locations are never collapsed into a generic
  current/non-current label.
- `mapping.match_name`: explicit for every field; `false` prevents a
  legacy/computed display name from being treated as a report-line alias.
- `mapping.rules`: reviewed disambiguation rules for captions whose meaning
  depends on scope, location, or occurrence. Every rule has a stable `id`,
  `status: reviewed`, aliases, and optional `scopes`, `locations`,
  `occurrence`, `min_occurrences`, `locations_present`, or
  `locations_absent`, `labels_present`, or `labels_absent`.
- `scopes`: standalone and/or consolidated presentation.
- `scopes.<scope>.position`: stable client output order.
- `scopes.<scope>.calculation`: one of:
  - `reported`: sourced from a printed report line;
  - `sum`: signed `terms`, each referencing another FID;
  - `ratio`: FID-based numerator, denominator, and scale.
- `evidence`: `client_mapping` for a reviewed mapping or `template_inferred`
  for a field derived from the template and accounting context.

`mapping.locations` is never empty. `STATEMENT-WIDE` explicitly means that the
caption is valid anywhere within that statement; otherwise the field lists its
allowed section(s). Locations use `location_source: declared`: they were
reviewed against the field’s FID-based formula hierarchy and are now declared
directly in this single source of truth. `mapping.match_name` is also
explicit for every field:
`true` makes the canonical display name an accepted reviewed caption and
`false` prevents legacy/computed display names from becoming aliases.
`mapping.mode` makes an empty alias list intentional: it states whether the
field uses its canonical name plus aliases, aliases only, reviewed rules only,
or is disabled from direct report-line matching.

Empty `includes`, `excludes`, or `mapping_notes` lists are intentional when the
field's `meaning` already provides a complete boundary. They are not filled
with generic boilerplate, because vague text reduces rather than improves
mapping precision.

Calculations never use Excel row addresses. The runtime validates field counts,
scope positions, formula references, calculation types, and taxonomy structure
before processing a report.

`statement_structure.yaml` contains the sector-level mapping grammar:

- `location_vocabulary` defines the only location labels fields may use.
- `statement_sections` identifies printed section headings and explicitly
  assigns each heading a mapping `location`; the engine never infers location
  from the heading or section name.
`identities.yaml` defines reported-value cross-checks entirely by FID. Each check
  has a stable `id`, applicable `scopes`, one `result_fid`, and signed
  `terms`. A term's `presence` is either `required` or `optional`; a check is
  skipped if a required printed value is absent.
  - `coefficient: 1` adds the field; `coefficient: -1` subtracts it.
  - `presence: required` means the check needs that reported field.
    `presence: optional` means use the field only when the report prints it.

`src/engine/client_map.py` merges and interprets this manifest. It contains no
software-sector captions, software-sector FIDs, or hardcoded field filenames.

`extraction.yaml` is deliberately separate from field taxonomy. It contains
only reviewed `statement_exclusions`, each with a stable ID, a short
description, and header-only regex patterns. The generic extraction engine
uses those exclusions during page selection, source reconciliation, and
completeness recovery; it contains no software-sector framework vocabulary.

Only exact reviewed aliases/rules can populate reported facts. Semantic model
output is written separately under `output/client/.proposals/` with
`authority: proposal_only` and `status: unreviewed`; it cannot mutate mapped
facts, formulas, verification, the workbook, or the canonical report cache.
