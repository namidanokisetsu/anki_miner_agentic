# Anki Miner Agentic

An agent-first Japanese sentence-mining application with guarded Anki writes. It builds a learner profile from explicitly mapped Anki fields, prepares bounded candidates from local media or YouTube, and lets an agent select useful cards through a JSON CLI or five-tool MCP server.

The agent never receives a generic Anki write tool. Anki Miner Agentic retains control of filtering, dictionaries, media, note construction, limits, and receipts. Live commits require both an enabled write target and a validation token proving that the exact selection passed a dry run.

This project is an independent fork of [Anki Miner](https://github.com/0xzerolight/anki_miner). The original project and its contributors built the GUI and mining pipeline on which this agent-first product is based. This distribution is maintained and released separately and is not an official upstream Anki Miner release.

## Install from source

Python 3.11 or newer, Anki with [AnkiConnect](https://ankiweb.net/shared/info/2055492159), and ffmpeg are required. Keep Anki open during setup.

```bash
git clone https://github.com/namidanokisetsu/anki_miner_agentic.git
cd anki_miner_agentic
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[mcp]"
anki_miner_agentic_gui
```

Use a dedicated virtual environment. This fork has a distinct distribution name, but still inherits the internal `anki_miner` Python namespace from upstream and must not be installed into the same environment as `anki-miner`.

Use **Tools → Download Recommended Resources** once to install the default dictionary, frequency, and pitch data. The current headless configuration can reuse those resources, but does not install them by itself.

## Set up an agent

Give your agent terminal and filesystem access to this checkout, then paste:

```text
Set up Anki Miner Agentic in this checkout. Read agentic-docs/agent-mining.md and skills/anki-miner-agent/SKILL.md, then install it with `python -m pip install -e ".[mcp]"`.

Use the existing GUI config, installed dictionaries, mining settings, and live Anki schema. Do not guess deck, note-type, or field names. Put the agent config outside the repo and leave `write_target.enabled` false so setup cannot create cards. Run `anki_miner_agentic_agent --config <config> profile-validate`, `profile-sync`, and `profile-status`.

Register `anki_miner_agentic_mcp --config <absolute-config-path>` as a stdio MCP server and verify its five tools. Fix routine setup errors, but never create cards until I approve the exact count after a dry run. Preserve the returned validation token and use it only with the unchanged live selection. Enabling the write target is a separate opt-in and does not replace explicit approval for a commit.
```

See the [agent mining guide](https://github.com/namidanokisetsu/anki_miner_agentic/blob/main/agentic-docs/agent-mining.md) for manual setup and the [MCP contract](https://github.com/namidanokisetsu/anki_miner_agentic/blob/main/skills/anki-miner-agent/references/mcp-contract.md) for tool payloads.

## Mining demo

![Anki Miner Showcase](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/demo.gif)

<p align="center">⬇️ <a href="https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/demo.mp4">Full demo with sound (MP4)</a></p>

### Example cards

| ![ホント](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/ホント.gif) | ![いちゃいちゃ](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/いちゃいちゃ.gif) | ![代](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/代.gif) |
|:--:|:--:|:--:|
| ⬇️ [MP4 (sound)](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/ホント.mp4) | ⬇️ [MP4 (sound)](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/いちゃいちゃ.mp4) | ⬇️ [MP4 (sound)](https://raw.githubusercontent.com/0xzerolight/anki_miner/main/gifs/代.mp4) |

## Tabs

- **Video** - mine a single video/subtitle pair, a batch folder, or YouTube URLs.
- **Deck Builder** - mine a whole series into one frequency-ranked deck.
- **Audiobooks** - mine audiobooks, podcasts, radio, songs (audio + subtitle/transcript pairs).
- **Reading** - mine manga (mokuro), novels (`.epub`, `.txt`; single book or a whole folder), standalone subtitle files, or pasted Japanese text.
- **Analytics** - mining history, difficulty rankings, milestones, undo.
- **Utilities** - generate subtitles (local Whisper), retime subtitles (alass), condense media to dialogue-only audio, copy the worth-learning part of a premade deck into a new one, and backfill fields on existing cards.
- **Settings** - everything configurable.

## Other Features

- Word Curator - review every candidate word before cards are made, with its scene, manga page, and dictionary entry side by side.
- Extensive filtering: i+1, frequency rank range, blacklist, regex, wordsets, and more.
- Offline Yomitan dictionary import - definitions, pitch accent, frequency - chained by priority.
- Multiple frequency lists chained by priority.
- Word audio on cards from local audio packs, JapanesePod101, or Google TTS.
- Sentence audio on Reading cards from Google Translate TTS or Naver Papago (off by default).
- Per-dictionary glossary styling, Yomitan-style.
- Embedded libmpv video preview - play a word's scene while curating, or nudge subtitle timing with live playback.
- Animated screenshots (see example cards above).
- Settings profiles - save named configurations and switch between them from the header.
- Restyle Mined Cards - re-apply your current card styling to cards you already made (Tools menu).

<details>
<summary><strong>Built-in themes (29)</strong></summary>

- **Ayu** - Light, Mirage, Dark
- **Catppuccin** - Latte (light); Frappé, Macchiato, Mocha (dark)
- **Dracula** - Dracula, Alucard
- **Everforest** - Light, Dark
- **GitHub** - Light; Dark, Dark Dimmed
- **Gruvbox** - Light Medium, Dark Medium
- **Kanagawa** - Lotus (light), Wave (dark)
- **Rosé Pine** - Dawn (light); Main, Moon (dark)
- **Solarized** - Light, Dark
- **Standalone** - Light, Dark, Sakura, Nord, One Dark, Tokyo Night

Theme licenses: [LICENSE-THEMES.md](LICENSE-THEMES.md). 
Want another theme added? Suggest in a GitHub Issue.

</details>

<details>
<summary><strong>How It Works</strong></summary>

1. **Read the subtitles** and split Japanese into individual words.
2. **Filter** to content words you don't already know - optionally reviewing the list yourself in the Word Curator.
3. **Grab a screenshot and audio clip** from the video for each line.
4. **Look up definitions** in your configured offline dictionaries, optionally falling back to Jisho online if enabled (slower, rate-limited).
5. **Send the finished cards to Anki.**

</details>

## Recommended Resources

| Type | Resource | Download | Add via |
|------|----------|----------|---------|
| Dictionary | [JMdict](https://github.com/yomidevs/jmdict-yomitan) | [Yomitan zip](https://github.com/yomidevs/jmdict-yomitan/releases/latest/download/JMdict_english.zip) | Add Dictionary… |
| Dictionary | [Jitendex](https://jitendex.org/) | [Yomitan zip](https://github.com/stephenmk/stephenmk.github.io/releases/latest/download/jitendex-yomitan.zip) | Add Dictionary… |
| Dictionary | [Bee's Character Dictionary](https://characterdictionary.tokyo/) | Generated on site | Add Dictionary… |
| Pitch | [Kanjium](https://github.com/mifunetoshiro/kanjium) | [TSV](https://raw.githubusercontent.com/mifunetoshiro/kanjium/master/data/source_files/raw/accents.txt) | Pitch Accent -> Add pitch source… |
| Pitch | [アクセント辞典v2](https://learnjapanese.moe/yomichan/#dictionaries) | [Drive](https://drive.google.com/drive/folders/1tTdLppnqMfVC5otPlX_cs4ixlIgjv_lH) | Pitch Accent -> Add pitch source… |
| Frequency | [JPDB v2.2 Kana](https://github.com/Kuuuube/yomitan-dictionaries) | [Yomitan zip](https://github.com/Kuuuube/yomitan-dictionaries/raw/main/dictionaries/JPDB_v2.2_Frequency_Kana_2024-10-13.zip) | Frequency -> Add frequency source… |
| Frequency | [BCCWJ SUW+LUW](https://github.com/Kuuuube/yomitan-dictionaries) | [Yomitan zip](https://github.com/Kuuuube/yomitan-dictionaries/raw/main/dictionaries/BCCWJ_SUW_LUW_combined.zip) | Frequency -> Add frequency source… |


<details>
<summary><strong>JMnedict License</strong></summary>

Uses bundled name wordsets derived from [JMnedict](https://www.edrdg.org/enamdict/enamdict_doc.html) (JMdict/EDICT project, EDRDG, CC BY-SA 4.0).

</details>

## Troubleshooting

| Issue                    | Solution                                                                         |
|--------------------------|----------------------------------------------------------------------------------|
| "Cannot connect to Anki" | Start Anki and ensure AnkiConnect is installed.                                  |
| "Deck not found"         | Pick an existing deck in Settings -> Cards & Anki. Decks are not created for you; make it in Anki first if you need a new one. |
| "Note type not found"    | Configure your note type's field names in Settings -> Cards & Anki.               |
| "ffmpeg not found"       | Install ffmpeg and add it to PATH.                                               |
| No definitions found     | Add a Yomitan dictionary in Settings -> Add Dictionary… (recommended), or enable the Jisho fallback (slower, rate-limited). |
| Windows installer will not open / SmartScreen warning | See [First-run notes](#first-run-notes-unsigned-builds): select **More info** -> **Run anyway**; restore Defender false positives from **Protection history**. |
| Fresh install has no definitions | Run Tools -> Setup Wizard or Tools -> Download Recommended Resources. For manual import, keep the Yomitan ZIP intact (do not unzip it). |
| Add Dictionary stalls or fails | Note the last visible stage and attach logs (see "Where are the logs?" below). Include the dictionary ZIP name, source, and size in the report. |
| Where are the logs?      | Use Help -> Open Log Folder, or open `%USERPROFILE%\.anki_miner\anki_miner.log` on Windows or `~/.anki_miner/anki_miner.log` on macOS/Linux. Rotated logs use the `.1` through `.5` suffixes. |
| Reporting a bug          | Help → Export Diagnostics… writes a ZIP with logs and system details to a location you choose. Review it before uploading because it contains file paths and file names from your computer. Nothing is uploaded automatically. |
| More diagnostic logging | Set `ANKI_MINER_LOG_LEVEL=DEBUG` before starting Anki Miner to capture third-party yt-dlp, urllib3, and fugashi details. The default is `WARNING`; Anki Miner logs remain at DEBUG. |
| Audio is wrong language  | The tool tries Japanese audio tracks first, then falls back to the default.      |
| Subtitles out of sync    | Use the subtitle offset control in the GUI (range ±300 seconds).                 |

## Roadmap

List of ideas for future versions of Anki Miner. Not in priority order. Feature requests take precedence.
- Suggest a feature in the [Agentic issue tracker](https://github.com/namidanokisetsu/anki_miner_agentic/issues).
- Discuss the roadmap in [Agentic discussions](https://github.com/namidanokisetsu/anki_miner_agentic/discussions).

- **Features**:
  - [x] UI language selection.
  - [x] Local subtitle creation tab: Opt-in tab to locally generate subtitles.
  - [x] Reading tab: Mine manga and books.
  - [x] Backfill tool.
  - [ ] Media library: Expand Analytics tab to display local media library across all media forms.
  - [ ] Automatic subtitle downloading.

- **Long-term**:
  - [x] Android port -- https://github.com/0xzerolight/anki_miner_android
  - [ ] Beyond Japanese: Mining other languages.
  - [ ] Anki Miner browser extension.


## Contributing

Contributions of any kind are welcome.
If you want to support the project, please share it with others who may benefit from it.

- New here? Start with [CONTRIBUTING.md](CONTRIBUTING.md).
- Architecture overview: [ARCHITECTURE.md](ARCHITECTURE.md).
- Testing strategy: [TESTING.md](TESTING.md).
- Code of Conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Security: [SECURITY.md](SECURITY.md).

Bug reports and feature requests → [Agentic issues](https://github.com/namidanokisetsu/anki_miner_agentic/issues).
General questions and discussion → [Agentic discussions](https://github.com/namidanokisetsu/anki_miner_agentic/discussions). For questions about the original application, use the [upstream project](https://github.com/0xzerolight/anki_miner).

## Special Thanks

Sincere thanks to people who made exceptional contributions to the project:

★ **[StyraxBenzoin](https://github.com/StyraxBenzoin)** - Brilliant feature suggestions, new release testing, community building

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for everyone who has made any kind of contribution to the project.


## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
