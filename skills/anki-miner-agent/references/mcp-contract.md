# MCP contract

The normal workflow is exactly two calls. Use returned IDs unchanged and never invent paths, candidates, fields, or run values.

## 1. Prepare one run

`max_cards` is explicit write authorization. If the user omits it, inspect total media duration without preparing a run, recommend `round(total_minutes × 10 / 24)` cards (minimum 1), and wait for the user to accept or replace that number.

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

`prepare_mining_run` synchronizes the learner profile, validates live mappings, resolves and fingerprints sources, parses and ranks candidates, bounds dictionary options, and returns:

- `run_id` and the request's single `max_cards` value;
- one compact `shortlist` containing up to `max_cards` eligible candidates, projected to candidate ID, target, sentence text, bounded definition options, allowed enrichments, and relevant quality flags;
- immutable review-batch IDs/formula and a versioned `review_contract`;
- `required_enrichments`;
- destination deck and note type.

Omit `audio_track` to select Japanese automatically. Use a zero-based audio-only stream index only when the user identifies incorrect metadata. Copy a GUI-calibrated `subtitle_offset` only for that exact local video/subtitle pair.

## 2. Review, enrich selected candidates, and commit

```json
{
  "run_id": "<returned run ID>",
  "reviews": [{
    "candidate_id": "<candidate ID from this run>",
    "decision": "select",
    "definition_option_id": "<prepared option ID>",
    "reason_code": "clear_supported_target",
    "rationale": "<optional short diagnostic>",
    "enrichments": {
      "chosen_definition": "<short contextual meaning of the selected definition option>",
      "sentence_translation": "<close, readable translation preserving Japanese phrasing>"
    }
  }]
}
```

Review the returned batch without a quota. Every selected candidate must use `clear_supported_target`, name one prepared definition option, and supply every returned `required_enrichments` key. Reject the rest with one allowed reject reason, `definition_option_id: null`, and no `enrichments` key. Validation failure performs no Anki write and never triggers a weaker or unenriched fallback.

`commit_mining_run` reserves a deterministic job before writing, groups selected candidates by source and audio policy, and returns a compact terminal receipt with review/write counts, enrichment coverage, destination, applied tags, Browser query, and exact selected-candidate outcomes/note IDs/errors. Media hashes and stored review state remain durable but are not repeated in this response. An unchanged retry resumes or returns the same job and reuses its timestamp/tag. Changed reviews for the same run are rejected.

## Recovery

| Error | Action |
|---|---|
| profile, mapping, or Anki connection error | Stop and correct setup from live Anki; never guess names. |
| `invalid_limit` or `max_cards_exceeded` | Supply one positive integer `max_cards` and keep the selection within it. |
| `candidate_not_in_run`, `unknown_candidate`, or ineligible review | Use only eligible IDs from this run's shortlist. |
| `missing_required_enrichment` | Supply the missing enrichment or change that candidate to an explicit rejection before retrying. |
| `invalid_review` or `missing_candidate_reviews` | Follow the returned review fields and reason-code allowlist; review every returned candidate. |
| `stale_source` | Prepare a new run from the current source files. |
| `writes_disabled` | Deliberately enable the configured target before attempting the authorized write. |
| `run_selection_changed` | Do not alter a reserved run; prepare a new run for different reviews. |
| interrupted or partial commit | Retry `commit_mining_run` with the unchanged `run_id` and reviews. |
