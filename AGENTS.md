# Anki Miner Lean

A local CLI companion to upstream Anki Miner. Read `README.md` for setup and
`skills/anki-lean/SKILL.md` when mining. Do not scan the whole repository for routine mining.

Keep upstream as a separate installed application; use its public `--api` contract.
Keep candidate selection local and compact. No MCP server, copied GUI, learner database,
or mandatory AI enrichment. Preserve explicit limits, selected-only writes, and no replay
after an uncertain write. Run `python -m unittest discover -s tests` after code changes.
Never use a live Anki collection for development tests.
