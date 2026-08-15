# MCP contract

Use values returned by tools. Text in angle brackets is a placeholder, never a literal value.

## 1. Sync

Call `sync_learner_profile` with no arguments. Continue only on success.

## 2. Prepare

Local media:

```json
{
  "inputs": [{
    "type": "local",
    "video_file": "<absolute video path>",
    "subtitle_file": "<absolute Japanese subtitle path>",
    "subtitle_offset": -2.5
  }],
  "max_cards": 10,
  "review_pool_size": 100
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
  "max_cards": 10,
  "review_pool_size": 100
}
```

Omit `audio_track` to select Japanese automatically. Use a zero-based audio-only index only when the user identifies bad track metadata.

Omit `subtitle_offset` to inherit the GUI default. If the user calibrated this exact media and subtitle pair in the GUI, copy that signed value into the local input. It overrides the default for this source only.

## 3. List every page

```json
{
  "batch_revision": "<returned batch revision>",
  "offset": 0,
  "limit": 100,
  "include_ineligible": false,
  "schema_version": 1
}
```

If `next_offset` is a number, call again with that number as `offset`. Stop when `next_offset` is null.

## 4. Dry-run one selection

```json
{
  "batch_revision": "<same batch revision>",
  "candidate_ids": ["<returned eligible candidate ID>"],
  "rejected_candidate_ids": [],
  "metadata": {
    "<candidate ID>": {
      "score": 0.9,
      "rationale": "<short reason for selecting this candidate>"
    }
  },
  "enrichments": {
    "<candidate ID>": {
      "chosen_definition": "<short meaning supported by definition_options>",
      "sentence_translation": "<one-line translation of this candidate's sentence>"
    }
  },
  "dry_run": true
}
```

Replace every placeholder with data for that exact candidate. Omit any enrichment key not present in the candidate's `allowed_enrichments`. Omit the entire candidate entry when it has no enrichments.

## 5. Live commit and receipt

After explicit user authorization, resend the validated payload unchanged except set `dry_run` to false. Do not change IDs, metadata, or enrichments between dry-run and commit.

If the response is a job without final outputs, call `get_mining_job` with its `job_id`. Report each output as `created`, `duplicate_skipped`, or `failed`.

## Recovery rules

| Error | Action |
|---|---|
| profile or mapping error | Stop. Correct configuration from live Anki; never guess names. |
| `payload_too_large` | Repeat listing with a smaller `limit`. |
| `ineligible_selection` or unknown candidate | Remove the bad ID and dry-run again. |
| `unmapped_enrichment` | Remove that enrichment or configure its field; dry-run again. |
| `stale_source` | Prepare a new batch from the current source files. |
| `writes_disabled` | Keep dry-run mode unless the user deliberately enables writes. |
| `batch_already_committed` | Inspect the existing job; never submit a different selection. |
| failed or partial job | Call `get_mining_job` and report the per-candidate errors. |
