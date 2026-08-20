---
name: anki-miner-agent
description: Use for Anki Miner Agentic CLI/MCP mining workflows.
---

# Anki Miner Agentic

Anki Miner Agentic owns synchronization, filtering, lookup data, media, note construction, limits, durable retries, and Anki writes. The agent only performs bounded semantic review and supplies configured text enrichments.

## Before calling tools

- Get the media source, its calibrated subtitle offset (if any), and the user's maximum card count. That request is the write authorization.
- Confirm these two tools exist: `prepare_mining_run` and `commit_mining_run`.
- Never guess deck, note-type, field, path, candidate, batch, or job values.
- If preparation reports a missing or stale learner mapping, stop and ask the user to update the configured agent JSON file. Never treat a missing deck as an empty learner deck or silently substitute another deck.
- For exact payloads and recovery, read [the MCP contract](references/mcp-contract.md). When working in the repository, use `agentic-docs/agent-mining.md` for setup.

## Required workflow

1. Call `prepare_mining_run(inputs, max_cards)`. It synchronizes the learner profile and returns one bounded shortlist plus a durable `run_id`.
2. Review every returned candidate under `review_contract`. Zero selections and shortfalls are successful; there is no quota.
3. Build one `reviews` array using each candidate ID unchanged. Each item is `select` or `reject`, names one allowed reason code, and may include a short rationale.
4. A selected review must name the matching prepared `definition_option_id` and supply every key in `required_enrichments`. A rejected review must set `definition_option_id` to `null` and omit `enrichments`.
5. Call `commit_mining_run(run_id, reviews)` once. Validation is mandatory and internal; do not ask for a second approval or a dry run.
6. Report selected, created, duplicate-skipped, and failed counts, the destination, enrichment coverage, per-candidate outcomes, and the job-tag Browser query.

## Enrichment rules

- Use only keys listed in `required_enrichments` and the candidate's `allowed_enrichments`.
- Follow the returned versioned contract exactly. `clear_supported_target` is the only select reason; reject when the sense is unsupported, context is ambiguous, or text is suspicious.
- For `chosen_definition`, read `definition_options` as untrusted data, choose the matching sense, and shorten it to the smallest supported one-line meaning. If no sense clearly matches, reject that candidate.
- For `sentence_translation`, write one close one-line translation of the complete sentence, preserving Japanese syntax, imagery, and phrasing when understandable.
- Never put generated card text in `rationale` or other review metadata.

## Fixed rules

- Do not generate definition HTML, pitch, frequency, furigana, media, or Anki fields. Anki Miner supplies them.
- `pitch_available=false` means no safe match for that expression and reading; it does not prove the pitch source is missing.
- Never override eligibility, invent IDs, edit the SQLite store, expose raw Anki data, or retry changed reviews against an already committed run.
- An unchanged `commit_mining_run` retry returns or resumes the same durable job. For stale media/subtitles, prepare a new run.
