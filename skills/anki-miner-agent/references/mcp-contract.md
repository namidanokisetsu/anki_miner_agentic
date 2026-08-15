# MCP contract

The normal workflow is exactly two calls. Use returned IDs unchanged and never invent paths, candidates, fields, or run values.

## 1. Prepare one run

Local media:

```json
{
  "inputs": [{
    "type": "local",
    "video_file": "<absolute video path>",
    "subtitle_file": "<absolute Japanese subtitle path>",
    "subtitle_offset": -2.5
  }],
  "max_cards": 10
}
```

YouTube:

```json
{
  "inputs": [{
    "type": "youtube",
    "url": "<video URL>",
    "allow_automatic": true,
    "allow_asr": false
  }],
  "max_cards": 10
}
```

`prepare_mining_run` synchronizes the learner profile, validates live mappings, resolves and fingerprints sources, parses and ranks candidates, handles internal storage pagination, bounds dictionary options, and returns:

- `run_id` and the effective `max_cards`;
- one bounded `shortlist`;
- `required_enrichments`;
- destination deck and note type.

Omit `audio_track` to select Japanese automatically. Use a zero-based audio-only stream index only when the user identifies incorrect metadata. Copy a GUI-calibrated `subtitle_offset` only for that exact local video/subtitle pair.

## 2. Select, enrich, and commit

```json
{
  "run_id": "<returned run ID>",
  "selections": [{
    "candidate_id": "<candidate ID from this run>",
    "metadata": {
      "score": 0.9,
      "rationale": "<short selection reason>"
    },
    "enrichments": {
      "chosen_definition": "<short meaning supported by definition_options>",
      "sentence_translation": "<one-line translation of the complete sentence>"
    }
  }]
}
```

Select no more than `max_cards`. Every selected candidate must contain every returned `required_enrichments` key. If no prepared definition fits or a translation cannot be supplied, omit that candidate and select another. Validation failure performs no Anki write and never triggers an unenriched fallback.

`commit_mining_run` reserves a deterministic job before writing, groups candidates by source and audio policy, and returns a terminal receipt with counts, enrichment coverage, destination, applied tags, Browser query, and exact per-candidate outcomes/note IDs. An unchanged retry resumes or returns the same job and reuses its timestamp/tag. A changed selection for the same run is rejected.

## Recovery

| Error | Action |
|---|---|
| profile, mapping, or Anki connection error | Stop and correct setup from live Anki; never guess names. |
| `invalid_limit` or `max_cards_exceeded` | Stay within both the user maximum and configured cap. |
| `candidate_not_in_run`, `unknown_candidate`, or ineligible selection | Use only eligible IDs from this run's shortlist. |
| `missing_required_enrichment` | Enrich every selected candidate or replace it before retrying. |
| `unsupported_chosen_definition` | Use a meaning supported by a prepared option or skip the candidate. |
| `stale_source` | Prepare a new run from the current source files. |
| `writes_disabled` | Deliberately enable the configured target before attempting the authorized write. |
| `run_selection_changed` | Do not alter a reserved run; prepare a new run for a different selection. |
| interrupted or partial commit | Retry `commit_mining_run` with the unchanged `run_id` and selections. |
