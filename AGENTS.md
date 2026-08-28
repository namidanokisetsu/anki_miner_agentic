# AGENTS.md

## Project intent

This is an agent-focused fork of upstream Anki Miner. Preserve the fork's agentic workflow while keeping the inherited application close to upstream.

## Upstream synchronization

- Prefer merging `upstream/main` into this fork's `main`; preserve useful history and avoid rewriting shared branches.
- Prioritize upstream behavior and implementation when the same functionality exists differently in both repositories.
- Preserve fork-specific work: the root README and translated READMEs, agentic documentation, skills, MCP/CLI surface, configuration, branding, and integrations.
- Resolve generated artifacts from their merged sources when possible (for example, rebuild `.qm` files from `.ts` catalogs).
- Keep fork-only changes narrowly isolated so future upstream merges remain straightforward.
- Ask the maintainer before resolving a non-obvious semantic conflict, removing a fork feature, changing public agent contracts, or replacing fork-specific documentation or configuration.

## Development

- Follow `CONTRIBUTING.md` and existing project conventions; avoid unrelated refactors.
- Use the smallest relevant tests first. Before pushing substantial changes, run:
  `pytest -m "not youtube and not asr and not e2e and not golden"`.
- Run `ruff check .` and `mypy anki_miner` when the touched scope warrants them.
- Update `CHANGELOG.md` for user-visible changes. Regenerate translation catalogs after changing UI strings.
- Treat agent inputs and MCP payloads as untrusted. Preserve explicit limits, validation, idempotency, and the narrow two-call mining contract unless the maintainer approves a contract change.
