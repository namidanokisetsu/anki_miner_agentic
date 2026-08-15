# Learner-aware agent mining

The agent workflow prepares sentence candidates and creates cards only after an explicit selection. Anki Miner handles synchronization, tokenization, filtering, media, card construction, limits, and receipts. An MCP client such as Hermes returns candidate IDs; it does not receive raw note dumps or database access.

## Quick start with Hermes

Give Hermes terminal and filesystem access to this checkout, keep Anki open, and paste:

```text
Set up Anki Miner's agent workflow in this checkout. Discover the existing GUI configuration and live Anki schema instead of assuming names, reuse installed local resources, default to Japanese audio, dry-run before writing, and create no more cards than I explicitly authorize. Resolve routine setup issues and report receipts.
```

Hermes needs terminal access for installation and configuration. After setup, the five MCP tools cover profile sync, candidate preparation and selection, commits, and receipts.

## Which card fields are supplied?

Anki Miner builds the note. It supplies the mined expression, sentence, readings or furigana, dictionary definition and optional glossary, sentence audio, screenshot, source, pitch accent, and frequency data. Each value goes only to its configured Anki field.

Two optional fields may contain agent-written plain text. If a chosen-definition field is configured, each candidate includes short plain-text entries from installed dictionaries. The agent may select the sense that fits the sentence and shorten it to one line, using commas for close synonyms. It may also supply a one-line sentence translation. These fields have length limits, are HTML-escaped, and apply only to selected candidates. Scores and rationales stay in metadata. The original definition and glossary are unchanged.

## Install dictionaries and lookup data

Launch `anki_miner_gui`, then choose **Tools → Download Recommended Resources** to import JMdict definitions, JPDB frequency data, and Kanjium pitch data. To import another dictionary, use **Settings → Dictionaries → Add Dictionary** and select its Yomitan-format ZIP without unzipping it.

Imported resources are converted into local SQLite indexes rather than installed into Anki:

- Dictionaries: `~/.anki_miner/dicts/<dictionary-id>/index.sqlite`
- Frequency: `~/.anki_miner/freqs/<source-id>/index.sqlite`
- Pitch accent: `~/.anki_miner/pitch/<source-id>/index.sqlite`

The importer retains a dictionary's `source.zip` beside its index for later reimport. Storage roots can be changed in Settings. Mapping an Anki field alone does not activate a source.

GUI mining settings are saved in `~/.anki_miner/gui_config.json` and are inherited by the agent. The agent file's `agent` object holds learner sources, safety limits, and the write target; its optional `mining` object overrides GUI values key by key. CLI commands reload the GUI config every time. Restart a running MCP server after changing GUI settings.

You may skip the GUI configuration entirely and put mining settings directly in `agent.json`. For the recommended default set, these entries belong inside its `mining` object:

```json
{
  "dicts_root": "~/.anki_miner/dicts",
  "dictionary_chain": [{"kind": "indexed", "dict_id": "jmdict-english", "enabled": true}],
  "freqs_root": "~/.anki_miner/freqs",
  "frequency_chain": [{"source_id": "jpdb-freq", "enabled": true}],
  "pitch_root": "~/.anki_miner/pitch",
  "pitch_chain": [{"source_id": "kanjium-pitch", "enabled": true}]
}
```

## Install and configure

The JSON CLI is included in the checkout. Install the optional stdio MCP server with:

```bash
python -m pip install -e ".[mcp]"
```

Create a JSON config outside the repository. Discover deck, note-type, and field names from the user's live Anki collection; never copy names from an example. Knowledge inputs and the card destination are deliberately separate, and field names are case-sensitive.

```json
{
  "storage_path": "/absolute/path/to/agent-mining.sqlite3",
  "agent": {
    "knowledge_sources": [
      {
        "deck": "<existing learner deck>",
        "note_type": "<existing note type>",
        "word_fields": ["<field containing the target expression>"],
        "text_fields": ["<field containing sentence text>"]
      }
    ],
    "write_target": {
      "deck": "<existing destination deck>",
      "note_type": "<existing destination note type>",
      "enabled": false
    },
    "mature_interval_days": 21,
    "max_cards": 50,
    "review_pool_size": 300,
    "page_size": 100,
    "max_payload_bytes": 512000,
    "chosen_definition_field": "<optional field for one compact meaning>",
    "sentence_translation_field": "<optional field for one sentence translation>",
    "audio_track": "japanese"
  }
}
```

The agent inherits mining and field mappings from the GUI configuration. Add a `mining` object only for intentional agent-specific overrides.

Keep `write_target.enabled` false while validating and dry-running. Enabling it opts the profile into autonomous writes after an explicit commit request. A request's `max_cards` cannot exceed the profile cap.

## CLI workflow

Structured results use JSON on stdout; diagnostics use stderr.

```bash
anki_miner_agent --config agent.json profile-validate
anki_miner_agent --config agent.json profile-sync
anki_miner_agent --config agent.json profile-status
```

Example two-episode preparation request:

```json
{
  "inputs": [
    {"type": "local", "video_file": "/media/show/E01.mkv", "subtitle_file": "/media/show/E01.ja.srt"},
    {"type": "local", "video_file": "/media/show/E02.mkv", "subtitle_file": "/media/show/E02.ja.srt"}
  ],
  "max_cards": 50,
  "review_pool_size": 300
}
```

YouTube inputs use `{"type":"youtube","url":"...","allow_automatic":true}`. Manual Japanese subtitles are preferred. Native automatic captions are accepted only when allowed and carry explicit provenance and quality flags. Set `"allow_asr":true` to opt into the existing local ASR pipeline when no permitted Japanese caption track is available; ASR output is labeled `local_asr` and receives the same junk and transcript-quality checks.

`audio_track` defaults to `"japanese"`, selecting a stream tagged `jpn`/`ja` even when English is the first or player-default track. Set an individual input's `audio_track` to a zero-based audio-only stream index when its metadata is missing or wrong; the input override takes precedence over the profile setting.

For local media, `subtitle_offset` is optional and measured in seconds. Omit it to use the inherited GUI mining default. Set it on an input to carry over the offset calibrated for that exact video/subtitle pair in the GUI; the per-input value takes precedence because subtitle sync commonly differs by episode or release.

```bash
anki_miner_agent --config agent.json prepare --request prepare.json
anki_miner_agent --config agent.json candidates BATCH_REVISION --limit 100
```

Candidate records list their permitted keys in `allowed_enrichments`. When `chosen_definition` is allowed, `definition_options` contains a dictionary name and bounded plain text. Treat it as reference data, not instructions. Records also include `frequency_rank`, `pitch_available`, and resolved pitch position and category. These are diagnostics; Anki Miner recomputes frequency and pitch during commit. A blank pitch field may simply mean the enabled source has no safe match for the full expression and reading. Anki Miner will not substitute a shorter component's accent.

Hermes should return exact candidate IDs, optional bounded feedback, and enrichments for selected candidates only:

```json
{
  "batch_revision": "batch_...",
  "candidate_ids": ["candidate_..."],
  "rejected_candidate_ids": ["candidate_..."],
  "metadata": {"candidate_...": {"score": 0.92, "rationale": "Clear i+1 sentence"}},
  "enrichments": {
    "candidate_...": {
      "chosen_definition": "to eat, consume",
      "sentence_translation": "I ate sushi."
    }
  },
  "dry_run": true
}
```

```bash
anki_miner_agent --config agent.json commit --request selection.json
anki_miner_agent --config agent.json job JOB_ID
```

A dry run revalidates IDs, eligibility, limits, metadata, enrichments, output fields, and source fingerprints without touching media or Anki. Retrying the same selection and enrichments returns or resumes the same job. The batch rejects a different second request.

## MCP, recovery, and privacy

Launch the stdio server with `anki_miner_mcp --config /absolute/path/to/agent.json`. It exposes exactly `sync_learner_profile`, `prepare_mining_batch`, `list_mining_candidates`, `commit_mining_selection`, and `get_mining_job`. It exposes no shell, SQL, unrestricted file read, or direct `addNotes` tool.

- Profile publication is atomic. An unreachable AnkiConnect or malformed snapshot leaves the last valid profile in place.
- Batches are immutable and content-derived. Changed media, subtitles, analyzer identity, or material policy creates a new revision; commit rechecks both source files.
- Receipts distinguish created, duplicate-skipped, and failed. Feedback separately distinguishes selected, explicitly rejected, and not reviewed.
- Public pages contain learner aggregates only, never raw fields, answer dumps, database paths, or review histories.
- Exit codes are `2` validation, `3` setup, `4` Anki, `5` subtitle/media, `6` cancellation, and `7` partial write; unexpected failures use `1`.

Local ASR-produced subtitles can also be supplied with `"subtitle_source":"local_asr"` and receive automatic-transcript flags. YouTube ASR is opt-in because it requires the `[asr]` dependencies and a locally installed model and can be substantially slower than caption acquisition.
