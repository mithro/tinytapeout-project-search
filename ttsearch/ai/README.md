# AI tags and summaries

Generates 6 to 10 tags from a canonical vocabulary and a four-sentence summary
for every project, using cheap models through OpenRouter with a stronger
model reviewing the result. Everything is resumable and every request's token
counts and cost are logged to `data/ai/usage.jsonl`.

The key is read from `~/.config/tinytapeout-project-search/openrouter.key` or
`OPENROUTER_API_KEY` and never enters the repository.

## Stages

| Command | What it does | Model (default) |
| --- | --- | --- |
| `tt-ai-corpus` | Report the corpus: one document per unique design (resubmissions share one), thin-document count, token estimate | none |
| `tt-ai-pass1` | Free-form tags and a summary per document; `--pilot N` runs a fixed sample, `--repair` re-runs incomplete batches | deepseek/deepseek-v4.1-flash, reasoning off |
| `tt-ai-taxonomy` | Normalise the raw tags, have a strong model design the canonical set with categories and aliases, map every raw tag. Review `data/ai/taxonomy.json` before pass two | deepseek/deepseek-v4-pro-0813 |
| `tt-ai-pass2` | Reclassify every document with canonical tags only, keeping or fixing the summary | deepseek/deepseek-v4-flash, reasoning off |
| `tt-ai-review` | A stronger model checks every entry against the source text and deterministic facts; approve, fix or escalate | deepseek/deepseek-v4-pro-0813 |
| `tt-ai-finalize` | Merge into `data/ai/tags.json`, one entry per project | none |
| `python -m ttsearch.ai.pilot_report` | Compare pilot outputs across models | none |

Every stage takes `--limit-usd` and stops when that run's spend passes it, and
writes one JSON file per batch so an interrupted run resumes without paying
for finished batches again.

## Design choices

- Documents are deduplicated on their documentation text: 3,544 documents
  cover 4,798 projects, so each design is read once.
- Projects with under 200 characters of documentation are flagged as thin;
  the model is told to say so rather than invent a summary.
- The pass-one prompt seeds a starter vocabulary (Tiny Tapeout's suggested
  tags plus common interface names) so spellings stay consistent, but allows
  free tags. The taxonomy step is where the vocabulary is decided.
- The reviewer gets deterministic facts (pinouts detected from pin names,
  language, silicon status) so it can reject tags the source cannot support.
