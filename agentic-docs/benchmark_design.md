# End-to-End Benchmark: Hermes Agentic vs. Vanilla Anki Miner

## Study design

Test the products as they are actually used. Do not turn Hermes into a manually controlled candidate-selection experiment.

| Variable | Fixed value |
| --- | --- |
| Methods | Vanilla Frequency, Vanilla i+1, Hermes Agentic |
| Corpora | Clean anime subtitles, noisy YouTube ASR subtitles |
| Source duration | 120 minutes per corpus |
| Runs | 6: 3 methods × 2 corpora |
| Requested cards | Exactly 50 per run |
| Agentic review pool | 300, used internally by the existing integration |
| Run order | Anime first, YouTube ASR second, for every method |
| Judges | ChatGPT, Gemini, DeepSeek |
| Judge passes | One independent pass per judge and corpus |
| Primary metric | Mean overall card-quality score, 1–5 |

This produces up to 300 final cards and six judge submissions: two blinded datasets sent once to each of three judges.

## What each method means

### Vanilla Frequency

Use the normal Anki Miner GUI. Turn i+1 filtering off, sort eligible targets by ascending frequency rank, select exactly the first 50, and create cards using normal vanilla settings.

### Vanilla i+1

Use the normal Anki Miner GUI. Turn the built-in i+1 filter on, sort the remaining targets by ascending frequency rank, select exactly the first 50, and create cards using normal vanilla settings.

### Hermes Agentic

Use Hermes exactly as currently integrated:

- the current Hermes model and system configuration;
- the registered two-tool MCP server;
- the current Anki Miner Agent skill;
- the real learner `knowledge_sources`;
- the current configured enrichments;
- the normal `prepare_mining_run` → agent choice → `commit_mining_run` flow.

Do not inspect, reorder, or manually select candidates. Hermes must operate autonomously. The internal deterministic preparation is part of the product, but the experimenter does not intervene in it.

## Two corpora

Testing both corpora is worthwhile because it measures two different capabilities:

- **Anime:** selection quality when subtitle text and timing are reliable.
- **YouTube ASR:** robustness to transcription mistakes, missing punctuation, bad segmentation, and noisy timing.

Build two fixed corpora before any run:

1. **Anime:** accurate Japanese subtitles totaling 120 minutes, normally about five episodes.
2. **YouTube ASR:** frozen ASR subtitles totaling 120 minutes. Use enough videos to reach the duration target.

Download and freeze the YouTube video and ASR subtitle files. Do not fetch changing live captions separately for each method.

Create `source-manifest.csv`:

```csv
corpus,source_id,video_file,video_sha256,subtitle_file,subtitle_sha256,subtitle_offset,audio_policy,duration_minutes
anime,A01,/path/A01.mkv,<sha256>,/path/A01.ja.srt,<sha256>,0.0,japanese,24
youtube_asr,Y01,/path/Y01.mkv,<sha256>,/path/Y01.asr.srt,<sha256>,0.0,japanese,18
```

The manifest is only a frozen input list. It ensures all methods receive identical media, subtitles, offsets, and audio policy.

## Learner knowledge

Hermes should keep using the learner's real deck or decks as `knowledge_sources`. Discover the exact deck, note-type, `word_fields`, and `text_fields` from live Anki.

The benchmark output decks must not be `knowledge_sources`.

For the vanilla methods, keep the current GUI known-word and filtering configuration unchanged. Do not retrofit the agent learner profile into vanilla. This is an end-to-end product comparison, so each method uses the learner-awareness capability it normally has.

Give each judge the same short learner brief:

```markdown
- Target language: Japanese
- Approximate level:
- Main learning goal:
- Typical media:
- Mature Anki cards:
- Young/learning Anki cards:
```

The judges assess final-card quality and broad learner suitability. They do not receive the raw learner deck.

## Register all parameters

Create `registration.md` before running anything:

```markdown
# Benchmark registration

## Software

- Repository commit:
- Anki Miner version:
- Anki version:
- AnkiConnect version:
- Hermes version:
- Hermes model and exact label:
- Anki Miner skill commit/hash:
- MCP command and config path:
- Run date:

## Inputs

- Source-manifest SHA-256:
- Anime duration: 120 minutes
- YouTube ASR duration: 120 minutes
- Subtitle offsets verified: yes/no

## Learner

- Knowledge-source deck(s):
- Note type(s):
- word_fields:
- text_fields:
- Learner-profile revision:
- Mature interval: 21 days

## Fixed run parameters

- Cards requested per run: 50
- Hermes max_cards: 50
- Hermes review_pool_size: 300
- Audio policy: japanese
- Run order: Anime, then YouTube ASR
- Hermes intervention: none

## GUI policy

- Exported GUI-settings file SHA-256:
- Frequency arm i+1 filter: off
- i+1 arm i+1 filter: on
- All other GUI settings unchanged: yes/no

## Judges

- ChatGPT exact model/date:
- Gemini exact model/date:
- DeepSeek exact model/date:
- Judge prompt: exact prompt in this protocol
- Randomization seeds: ChatGPT 101, Gemini 202, DeepSeek 303
```

Export the complete GUI settings once and retain the file. This registers dictionaries, frequency sources, word lists, filters, note mappings, media settings, and card behavior without transcribing every setting.

## Create three isolated Anki profiles

1. Export the starting learner collection as one complete `.colpkg` with scheduling and media.
2. Import that same snapshot into:
   - `Benchmark Frequency`;
   - `Benchmark i+1`;
   - `Benchmark Hermes`.
3. In each profile, create empty decks:
   - `Benchmark Anime`;
   - `Benchmark YouTube ASR`.
4. Keep those output decks out of learner sources.
5. Keep only the profile for the current method open.

Using three profiles prevents one method's cards from changing another method's duplicate detection or learner state. Both corpora may run sequentially inside each method profile because output decks are excluded from learner sources and the order is fixed.

## Run the six conditions

| Run | Anki profile | Corpus | Procedure |
| --- | --- | --- | --- |
| 1 | Benchmark Frequency | Anime | i+1 off; frequency ascending; first 50 |
| 2 | Benchmark Frequency | YouTube ASR | i+1 off; frequency ascending; first 50 |
| 3 | Benchmark i+1 | Anime | i+1 on; frequency ascending; first 50 |
| 4 | Benchmark i+1 | YouTube ASR | i+1 on; frequency ascending; first 50 |
| 5 | Benchmark Hermes | Anime | Normal Hermes request; no intervention |
| 6 | Benchmark Hermes | YouTube ASR | Normal Hermes request; no intervention |

For every run:

1. Use exactly the sources listed for that corpus in `source-manifest.csv`.
2. Set the matching output deck.
3. Request or select exactly 50 cards.
4. Record selected, created, duplicate-skipped, failed, and elapsed time.
5. Do not replace failures or duplicates manually.

For each Hermes run, use only this user request, with the real registered paths substituted:

```text
Mine exactly 50 cards from these registered benchmark sources using the normal
agentic workflow. Use the recorded subtitle offsets and Japanese audio policy.
Operate autonomously through the existing MCP integration and report the full
terminal receipt.
```

That request is the write authorization. Do not give Hermes a special scoring prompt and do not inspect its shortlist before commit.

## Export final cards

Export the final notes from each of the six output decks. Keep only fields a learner actually sees:

- target expression;
- reading;
- sentence;
- definition;
- sentence translation, when present;
- source label.

Strip HTML presentation while preserving the text. Do not include deck name, method, tags, job IDs, selector rationale, frequency rank, or internal candidate data.

Create two files:

- `anime-blinded.csv`: 150 rows maximum;
- `youtube-asr-blinded.csv`: 150 rows maximum.

Use neutral IDs and keep the method mapping in a private `answer-key.csv`. Shuffle each judge's copy with its registered seed.

This text-only evaluation does not assess audio cuts or screenshot timing. If those matter, manually inspect the same random 20 cards per method after the blinded text analysis.

## Use three judges

Send both blinded files and the learner brief independently to ChatGPT, Gemini, and DeepSeek. Record the exact model label and date shown by each product.

Use this identical prompt:

```text
You are a blinded evaluator of Japanese sentence-mining cards. The cards were
created by different methods, but you do not know which method created any row.
Use the supplied learner brief.

Score each card from 1 to 5:

- subtitle_integrity: the sentence appears coherent and free of transcription errors;
- context_clarity: the target's meaning is understandable in the sentence;
- definition_accuracy: the definition matches the target's contextual sense;
- translation_accuracy: the translation is accurate and natural; use null if absent;
- learnability: the final card is concise, useful, and reviewable;
- overall_quality: the card is worth adding for this learner.

Set invalid=true for malformed targets, broken ASR text, a clearly wrong sense,
or an unusable sentence. Do not guess the generating method. Return CSV only:

blind_id,subtitle_integrity,context_clarity,definition_accuracy,
translation_accuracy,learnability,overall_quality,invalid,short_reason
```

Check that every row was returned once and all non-null scores are integers from 1 to 5.

## Analyze results

Average the three judge scores for each card, then report results separately for each corpus:

| Metric | Frequency | i+1 | Hermes |
| --- | ---: | ---: | ---: |
| Requested | 50 | 50 | 50 |
| Created | | | |
| Mean overall quality | | | |
| Mean subtitle integrity | | | |
| Mean context clarity | | | |
| Mean definition accuracy | | | |
| Mean translation accuracy | | | |
| Mean learnability | | | |
| Invalid-card rate | | | |
| Elapsed time | | | |

Also report each judge separately. This shows whether a result is shared across models or driven by one judge.

Treat Hermes as the strongest end-to-end method only if:

1. it has the highest mean overall quality in both corpora;
2. at least two of three judges rank it first in each corpus;
3. its invalid-card rate is no higher than both vanilla methods;
4. its created-card yield remains acceptable.

The difference between Anime and YouTube ASR scores is the robustness result. A small drop indicates better tolerance of noisy subtitles.

## Checklist

- [ ] Two frozen 120-minute corpora registered and hashed.
- [ ] All parameters and model labels recorded.
- [ ] Three Anki profiles created from one snapshot.
- [ ] Six output decks empty before starting.
- [ ] Output decks excluded from learner sources.
- [ ] Vanilla Frequency completed normally.
- [ ] Vanilla i+1 completed normally.
- [ ] Hermes completed through its existing integration without intervention.
- [ ] Exactly 50 cards selected in every run.
- [ ] Final cards blinded into two corpus files.
- [ ] ChatGPT, Gemini, and DeepSeek judged both files.
- [ ] Results reported by corpus, method, and judge.
