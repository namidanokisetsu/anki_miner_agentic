# Anki Miner Agentic workflow

The normal MCP path is two calls: prepare one durable run, then commit one reviewed batch. Anki Miner owns learner synchronization, source validation, deterministic filtering/ranking, dictionary data, media, note construction, limits, batching, provenance, and retries. The agent only reviews the bounded shortlist and enriches selected candidates; it receives no raw note dumps or database access.

This guide documents only the fork's agentic layer. See the [upstream Anki Miner documentation](https://github.com/0xzerolight/anki_miner#readme) for the inherited desktop application. The fork boundary and shared code changes are summarized in the root README.

## Quick start

Give the agent terminal and filesystem access to this checkout, keep Anki open, and paste:

```text
Set up Anki Miner Agentic in this checkout. Discover the existing GUI configuration and live Anki schema instead of assuming names, reuse installed local resources, default to Japanese audio, and create no more cards than I request. Use the two-call prepare_mining_run / commit_mining_run workflow, require every configured enrichment on every selected review, and report the terminal receipt and job-tag query.
```

Install the optional stdio server with `python -m pip install -e ".[mcp]"`, then launch it with:

```bash
anki_miner_agentic_mcp --config "$HOME/.anki_miner/agentic-agent.json"
```

It publicly exposes only `prepare_mining_run` and `commit_mining_run`. The CLI's matching `prepare-run` and `commit-run` commands use the same contract. Older low-level `prepare`, `candidates`, `commit`, and `job` commands remain for compatibility and recovery; they are not the supported agent orchestration surface.

## Configuration

The active GUI profile is the single source of mining policy: dictionaries, filters, word lists, ranking, media, and card behavior. Recommended dictionary, frequency, and pitch resources can be installed from **Tools → Download Recommended Resources**. Agent-specific safety and learner configuration is stored outside the repository at `~/.anki_miner/agentic-agent.json`:

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
    "max_payload_bytes": 512000,
    "chosen_definition_field": "<optional field>",
    "sentence_translation_field": "<optional field>",
    "audio_track": "japanese"
  }
}
```

Executable paths and deterministic mining policy belong exclusively to the active GUI profile. Agent-side `runtime_overrides`, `mining`, `review_pool_size`, `page_size`, exclusion flags, and inline word lists are rejected with `unsupported_agent_config_key`, even when their value is false or empty. Prepared runs and profile status include the effective policy fingerprint and setting provenance.

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
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" profile-validate
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" profile-sync
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" profile-status
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" policy-status
```

`policy-status` is read-only. It shows the next-run fingerprint, each bounded effective value and owner, derived safety values, and any stale live Anki mappings.

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

`prepare_mining_run` validates the live mapping and synchronizes the learner profile, fingerprints each unique source path, parses each subtitle into one reusable representation, applies deterministic eligibility and ranking, bounds review internally, loads full definition options only for shortlisted candidates, and internally consumes storage pages. It returns one compact response with `run_id`, separately labeled safety/run/review maxima, immutable review-batch metadata, a versioned review contract, paging completeness, required enrichments, destination, and `shortlist`. Zero cards and shortfalls are successful outcomes.

Candidate records contain target and sentence context, learner aggregates, quality flags, frequency/pitch signals, and bounded dictionary options. They never contain raw learner fields, review histories, or database paths.

## Review and enrichment

Review every candidate in the returned review batch using the exact returned contract. Each review is `select` or `reject`, has one allowlisted `reason_code`, and may include a short rationale. A selection must use `clear_supported_target` and name the matching prepared `definition_option_id`; a rejection sets `definition_option_id` to `null` and omits `enrichments`. Generated card text belongs only in selected reviews' `enrichments`.

If `sentence_translation` is required, every selected candidate needs a close one-line translation of the full sentence that preserves Japanese syntax, imagery, and phrasing when understandable. If `chosen_definition` is required, set `definition_option_id` to the matching prepared option and keep the one-line meaning supported by it. When a candidate cannot be confidently supported or enriched, reject it with the applicable reason; the system never fills the shortfall.

## Call 2: commit

```json
{
  "run_id": "run_...",
  "reviews": [{
    "candidate_id": "candidate_...",
    "decision": "select",
    "definition_option_id": "definition_2",
    "reason_code": "clear_supported_target",
    "rationale": "The prepared sense clearly matches this use.",
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

The terminal receipt reports reviewed/rejected counts plus selected, created, duplicate-skipped, and failed counts; enrichment coverage; destination; applied tags; job-tag Browser query; and selected-candidate outcomes/note IDs. An unchanged retry with the same `run_id` and reviews returns or resumes the same job without duplicate creation. Changed reviews for an already reserved run fail; prepare a new run instead.

The equivalent JSON CLI commands are:

```bash
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" prepare-run --request prepare.json
anki_miner_agentic_agent --config "$HOME/.anki_miner/agentic-agent.json" commit-run --request commit.json
```
