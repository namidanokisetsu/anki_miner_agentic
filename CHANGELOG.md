# Changelog

## 0.2.0

- Use upstream's normal parsing, filtering and word-curation pipeline to export candidates.
  Remove the duplicate tokenizer, vocabulary reader and their dependencies.
- Preserve upstream's chosen subtitle cue and merge, including offset-clamped cues.
- Require the same upstream Python installation for preparation and card creation;
  refuse new writes if its version or profile settings changed since preparation.
- Call the workflow and skill Anki Miner. Existing schema-1 runs must be prepared again.

## 0.1.0

- Standalone Japanese candidate preparation and selected-word mining through upstream's public API.
- Optional later enrichment; no AI definition or translation requirement during mining.
- Compact file-backed shortlists and receipts, exclusive run locking, and no automatic write replay.
- Replaces the inherited desktop fork on main; upstream is now a separately installed application.
