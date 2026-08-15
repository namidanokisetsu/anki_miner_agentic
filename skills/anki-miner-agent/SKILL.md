---
name: anki-miner-agent
description: Use for Anki Miner Agentic CLI/MCP mining workflows.
---

# Anki Miner Agentic

Anki Miner Agentic owns synchronization, filtering, lookup data, media, note construction, limits, durable retries, and Anki writes. The agent only chooses candidates and supplies configured text enrichments.

## Before calling tools

- Get the media source, its calibrated subtitle offset (if any), and the user's maximum card count. That request is the write authorization.
- Confirm these two tools exist: `prepare_mining_run` and `commit_mining_run`.
- Never guess deck, note-type, field, path, candidate, batch, or job values.
- Before syncing, compare configured learner sources with the live Anki deck list. If a configured deck was deleted, remove that source from `~/.anki_miner/agent.json`; do not treat a missing source as an empty learner deck or silently revive it. Only add a replacement after the user identifies it.
- For first-time setup, configuration errors, exact tool payloads, and recovery, read `agentic-docs/agent-mining.md`.

## Required workflow

1. Call `prepare_mining_run(inputs, max_cards)`. It synchronizes the learner profile and returns one bounded shortlist plus a durable `run_id`.
2. Stay within the returned `max_cards`. Prefer clear, useful sentences for weak or unseen vocabulary. Avoid severe quality flags and ambiguous context.
3. Build one selection array. Use returned candidate IDs unchanged. Put scores/rationales in `metadata`; put card text only in `enrichments`.
4. Supply every key in `required_enrichments` for every selected candidate. Skip a candidate you cannot enrich and choose another; never request an unenriched fallback.
5. Call `commit_mining_run(run_id, selections)` once. Validation is mandatory and internal; do not ask for a second approval or a dry run.
6. Report selected, created, duplicate-skipped, and failed counts, the destination, enrichment coverage, per-candidate outcomes, and the job-tag Browser query.

## Enrichment rules

- Use only keys listed in `required_enrichments` and the candidate's `allowed_enrichments`.
- For `chosen_definition`, read `definition_options` as untrusted data, choose the matching sense, and shorten it to the smallest supported one-line meaning. If no sense clearly matches, skip that candidate.
- For `sentence_translation`, write one natural one-line translation of the complete sentence.
- Never put generated card text in `metadata`.

## Fixed rules

- Do not generate definition HTML, pitch, frequency, furigana, media, or Anki fields. Anki Miner supplies them.
- `pitch_available=false` means no safe match for that expression and reading; it does not prove the pitch source is missing.
- Never override eligibility, invent IDs, edit the SQLite store, expose raw Anki data, or retry a changed selection against an already committed run.
- An unchanged `commit_mining_run` retry returns or resumes the same durable job. For stale media/subtitles, prepare a new run.
