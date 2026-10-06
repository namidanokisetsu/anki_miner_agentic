# Anki Miner

A local CLI companion to upstream Anki Miner. Read `README.md` for setup and
`skills/anki-miner/SKILL.md` when mining. Do not scan the whole repository for routine mining.

Keep upstream separately installed. Use its public `--api` contract for card creation;
isolate candidate-export integration in `candidates.py`. Use upstream's actual parsing,
filters and knowledge sources; do not reimplement them. Keep agent input compact.
No MCP server, copied GUI, separate learner database or mandatory AI enrichment.
Preserve explicit limits, selected-only writes, and no replay
after an uncertain write. Run `python -m unittest discover -s tests` after code changes.
Never use a live Anki collection for development tests.
