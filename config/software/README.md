# Software taxonomy

`taxonomy.yaml` is the only runtime source for software-sector client fields.
It combines semantic mapping metadata, output presentation, and calculations.

Each field defines:

- `fid`: stable client field identifier.
- `name`: client-facing display name.
- `statement`: income, balance, cashflow, or segment.
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
- top-level `mapping_policy`: strict unit/time/granularity, preserved source
  sign, rejected ambiguity, and proposal-only model authority. These universal
  rules are declared once instead of repeated in all 410 definitions.
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
- `evidence`: `client_mapping` when a client-supplied mapping supports the
  field, or `template_inferred` when the field comes from the client template
  and accounting context.

The two count guards have deliberately explicit names:

- `expected_unique_field_count`: number of unique FIDs in the sector taxonomy.
- `expected_scope_assignment_count`: number of output placements after counting
  a field once for standalone and once for consolidated when it applies to both.

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

The sector-level mapping grammar is declarative too:

- `location_vocabulary` defines the only location labels fields may use.
- `statement_sections` identifies printed section headings and explicitly
  assigns each heading a mapping `location`; the engine never infers location
  from the heading or section name.
- `identities` defines reported-value cross-checks entirely by FID. Each check
  has a stable `id`, applicable `scopes`, one `result_fid`, and signed
  `terms`. A term's `presence` is either `required` or `optional`; a check is
  skipped if a required printed value is absent.
  - `coefficient: 1` adds the field; `coefficient: -1` subtracts it.
  - `presence: required` means the check needs that reported field.
    `presence: optional` means use the field only when the report prints it.

`src/engine/client_map.py` interprets this contract. It contains no
software-sector captions or software-sector FIDs.

Only exact reviewed aliases/rules can populate reported facts. Semantic model
output is written separately under `output/client/.proposals/` with
`authority: proposal_only` and `status: unreviewed`; it cannot mutate mapped
facts, formulas, verification, the workbook, or the canonical report cache.
