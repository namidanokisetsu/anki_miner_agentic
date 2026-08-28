# Anki Miner Agentic

A fork of [Anki Miner](https://github.com/0xzerolight/anki_miner) that lets an AI review candidates before cards are written. See the [upstream README](https://github.com/0xzerolight/anki_miner#readme) for the desktop app and [`agentic-docs/`](agentic-docs/) for setup.

## How it works

1. Choose media and a maximum card count (the most cards the app may create; the final count may be lower). If omitted, the agent recommends about 10 cards per 24 minutes and asks first.
2. The app reads your learner deck and subtitles, applies your settings, then filters and ranks candidates.
3. The AI accepts or rejects each candidate based on its sentence and dictionary meaning.
4. The app validates the review, creates media, skips duplicates, writes selected cards, and returns a receipt.

## Install from source

You need Python 3.11+, Anki with [AnkiConnect](https://ankiweb.net/shared/info/2055492159), and ffmpeg. Keep Anki open during setup.

```bash
git clone https://github.com/namidanokisetsu/anki_miner_agentic.git
cd anki_miner_agentic
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[mcp]"
anki_miner_agentic_gui
```

Use a dedicated virtual environment. This fork and upstream `anki-miner` both use the `anki_miner` Python package, so this fork must not be installed into the same environment as `anki-miner`.

When an agent is working in an existing checkout: **Reuse the active virtual environment; do not reinstall the package.**

## Set up an agent

Give your agent access to this checkout, then paste:

```text
Configure Anki Miner Agentic in this checkout. Read agentic-docs/agent-mining.md and skills/anki-miner-agent/SKILL.md. Reuse the active virtual environment and the GUI settings. Read deck, note-type, and field names from Anki instead of guessing them. Keep `write_target.enabled` false during setup. Validate and sync the learner profile, then register the two-tool MCP server.
```

`~/.anki_miner/agentic-agent.json` stores only learner mappings, the write target and enable switch, learner maturity, audio policy, storage path, and allowlisted enrichment-field mappings. Card count belongs to each request; mining policy and executable paths remain owned by the active GUI profile.

Use MCP for normal conversations with an agent (`prepare_mining_run`, then `commit_mining_run`). The JSON CLI's `prepare-run` and `commit-run` commands expose that same workflow for terminal use, scripts, or clients without MCP. Older low-level CLI commands remain only as a compatibility and recovery surface.

For manual setup, read the [agent mining guide](agentic-docs/agent-mining.md). Exact tool payloads are in the [MCP contract](skills/anki-miner-agent/references/mcp-contract.md).

## Tabs

The desktop tabs and settings follow upstream Anki Miner. See the [upstream README](https://github.com/0xzerolight/anki_miner#readme) for the full GUI guide.

## Configure it

Set up dictionaries, frequency and pitch sources, Anki fields, filters, and media in the GUI. The agent inherits those settings.

## Troubleshooting

| Issue | What to do |
| --- | --- |
| Fresh install has no definitions | Run `Tools -> Setup Wizard or Tools -> Download Recommended Resources`, then confirm a dictionary is enabled in Settings. |
| Add Dictionary stalls or fails | Retry while recording the last visible stage. Report the dictionary ZIP name, source, and size, and keep the Yomitan ZIP intact (do not unzip it). |
| Where are the logs? | Open `~/.anki_miner/anki_miner.log` on macOS/Linux or `%USERPROFILE%\.anki_miner\anki_miner.log` on Windows. Rotated logs use `.1` through `.5` suffixes. |
| Spotlight launcher does nothing on macOS | A development install under Desktop, Documents, or Downloads is blocked by macOS privacy controls when launched from Spotlight. Move it outside those folders or use the packaged application. |

`Help → Export Diagnostics…` creates a support archive. Review it before uploading because it contains file paths and file names from your computer. For temporary verbose logging, launch with `ANKI_MINER_LOG_LEVEL=DEBUG`.

## Agentic roadmap

Keep the public agent contract narrow: prepare candidates, review them, then commit a validated selection. Larger agent features should remain isolated from upstream GUI internals so upstream updates stay mergeable.

## License

[GPL-3.0](LICENSE)
