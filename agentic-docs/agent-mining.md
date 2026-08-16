# Anki Miner Agentic workflow

The normal MCP path is two calls: prepare one durable run, then commit one enriched selection. Anki Miner owns learner synchronization, source validation, parsing, filtering, dictionary data, media, note construction, limits, batching, provenance, and retries. The agent receives no raw note dumps or database access.

## Quick start

Give the agent terminal and filesystem access to this checkout, keep Anki open, and paste:

```text
Set up Anki Miner Agentic in this checkout. Discover the existing GUI configuration and live Anki schema instead of assuming names, reuse installed local resources, default to Japanese audio, and create no more cards than I request. Use the two-call prepare_mining_run / commit_mining_run workflow, require every configured enrichment on every selection, and report the terminal receipt and job-tag query.
```

Install the optional stdio server with `python -m pip install -e ".[mcp]"`, then launch it with:

```bash
anki_miner_agentic_mcp --config /absolute/path/to/agent.json
```

It publicly exposes only `prepare_mining_run` and `commit_mining_run`. Lower-level CLI operations remain available for setup and recovery, but are not part of normal MCP orchestration.

## Configuration

The active GUI profile is the single source of mining policy: dictionaries, filters, word lists, ranking, media, and card behavior. Recommended dictionary, frequency, and pitch resources can be installed from **Tools → Download Recommended Resources**. Agent-specific safety and learner configuration is stored outside the repository:

```json
{
  "storage_path": "/absolute/path/to/agent-mining.sqlite3",
  "agent": {
    "knowledge_sources": [{
      "deck": "<existing learner deck>",
      "note_type": "<existing note type>",
      "word_fields": ["<target expression field>"],
      "text_fields": ["<sentence field>"]
    }],
    "write_target": {
      "deck": "<existing destination deck>",
      "note_type": "<existing destination note type>",
      "enabled": false
    },
    "mature_interval_days": 21,
    "max_cards": 50,
    "review_pool_size": 300,
    "max_payload_bytes": 512000,
    "chosen_definition_field": "<optional field>",
    "sentence_translation_field": "<optional field>",
    "audio_track": "japanese"
  }
}
```

An optional top-level `runtime_overrides` object may set only executable paths: `ffmpeg_location`, `ffprobe_location`, `alass_location`, `youtube_ffmpeg_location`, and `ytdlp_location`. Mining-policy overrides belong in the active GUI profile.

Legacy `page_size` is ignored. Legacy exclusion flags are translated in memory; non-empty inline word lists and policy fields under `mining` must be moved to the GUI profile. Prepared runs and profile status include the effective policy fingerprint.

Compact CLI help is available without a config or Anki connection:

```bash
anki_miner_agentic_agent help
anki_miner_agentic_agent help settings
anki_miner_agentic_agent help workflow
anki_miner_agentic_agent help commands
```

Deck, note-type, and field names are case-sensitive and must be discovered from live Anki. A missing configured deck is an error, not an empty learner source. `write_target.enabled` is a profile-level safety switch. The user's request to mine up to a stated number is the write authorization; the normal flow has no second approval or public dry-run token.

For setup diagnostics, the JSON CLI retains:

```bash
anki_miner_agentic_agent --config agent.json profile-validate
anki_miner_agentic_agent --config agent.json profile-sync
anki_miner_agentic_agent --config agent.json profile-status
```

## Call 1: prepare

```json
{
  "inputs": [
    {
      "type": "local",
      "video_file": "/media/show/E01.mkv",
      "subtitle_file": "/media/show/E01.ja.srt",
      "subtitle_offset": -2.5
    }
  ],
  "max_cards": 10
}
```

YouTube uses `{"type":"youtube","url":"...","allow_automatic":true,"allow_asr":false}`. Manual Japanese subtitles are preferred. Automatic captions and local ASR remain explicit opt-ins and retain provenance/quality flags. Omit `audio_track` to select Japanese by language metadata; use a zero-based audio-only stream index only for incorrect metadata.

`prepare_mining_run` validates the live mapping and synchronizes the learner profile, fingerprints each unique source path, parses each subtitle into one reusable representation, applies deterministic eligibility and ranking, bounds the review pool, loads full definition options only for shortlisted candidates, and internally consumes storage pages. It returns one compact response with `run_id`, effective `max_cards`, `required_enrichments`, destination, and `shortlist`.

Candidate records contain target and sentence context, learner aggregates, quality flags, frequency/pitch signals, and bounded dictionary options. They never contain raw learner fields, review histories, or database paths.

## Selection and enrichment

Choose no more than the returned maximum. Use candidate IDs unchanged. Metadata may contain a bounded score and rationale; generated card text belongs only in `enrichments`.

If `sentence_translation` is required, every selected candidate needs a natural one-line translation of the full sentence. If `chosen_definition` is required, choose the matching prepared sense and keep the one-line meaning supported by that option. When a candidate cannot be confidently enriched, skip it and choose another. Slow generation, timeout metadata, or an empty enrichment object never authorizes an unenriched card.

## Call 2: commit

```json
{
  "run_id": "run_...",
  "selections": [{
    "candidate_id": "candidate_...",
    "metadata": {"score": 0.92, "rationale": "Clear i+1 sentence"},
    "enrichments": {
      "chosen_definition": "to eat, consume",
      "sentence_translation": "I ate sushi."
    }
  }]
}
```

`commit_mining_run` performs complete side-effect-free validation before writing, reserves a deterministic job, reuses one source fingerprint per path, preflights once, groups media/lookup/note creation by video, subtitle, and audio policy, and records exact candidate-aligned outcomes. Global mapping, schema, or Anki-connection failures stop remaining groups.

New notes merge existing configured tags with:

```text
anki_miner_agentic
anki_miner_agentic::job::<UTC timestamp>_<short job ID>
```

The timestamp and job tag are persisted at reservation and reused on retry. Duplicate-skipped pre-existing notes are not modified or tagged.

The terminal receipt reports selected, created, duplicate-skipped, and failed counts; enrichment coverage; destination; applied tags; job-tag Browser query; and per-candidate outcomes/note IDs. An unchanged retry with the same `run_id` and selections returns or resumes the same job without duplicate creation. A changed selection for an already reserved run fails; prepare a new run instead.

The equivalent JSON CLI commands are:

```bash
anki_miner_agentic_agent --config agent.json prepare-run --request prepare.json
anki_miner_agentic_agent --config agent.json commit-run --request commit.json
```
