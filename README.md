# AnkiGPT

AnkiGPT turns study material into editable Anki decks with an **agentic pipeline**: a
planning agent maps your document, decides how to carve it up, and delegates card
writing to specialist workers that run in parallel. A critic reviews every card against
the source, near-duplicates are merged by meaning, a coverage audit fills gaps, and you
can feed your real Anki review history back in to have failing cards rewritten.

Paste text or upload a PDF, watch the run trace live, edit the cards, export `.apkg`.

## How a deck is generated

```
source ──▶ MAP ──▶ PLAN ──▶ FIGURES ──▶ WRITE ──▶ CRITIQUE ──▶ RECONCILE ──▶ COVERAGE ──▶ FINISH
           │        │         │           │          │            │             │
     document map   │    vision reads  N workers   cold-answer  embeddings    audit each
     units, kinds,  │    figures from  in parallel + judge on   cluster +     unit; spawn
     density,       │    the PDF       one strategy every card  model merges  gap-fillers
     prerequisites  │                  each
                    ▼
        an agent with tools: read_unit · search_source · spawn_task · skip_unit · finish_plan
```

1. **Map** — the source is split on headings into candidates; one cheap model call groups
   them into semantic *units* with a kind (definitions, formulas, procedure, comparison,
   worked example…), a density score, prerequisites, and skip verdicts for front matter,
   references, and recaps. Short sources skip the model and become one unit.
2. **Plan** — the planner is a real tool-using loop. It reads units it is unsure about,
   searches the source, and *spawns tasks*: which units, which **card strategy**, how many
   cards, and specific notes for the worker. Every live unit must end up covered
   (uncovered units get a default task); task and card counts are capped. If the model
   never calls a tool, a single structured-output plan is used instead.
   Tick **Review the plan before writing** and the run pauses so you can skip tasks,
   change strategies, resize budgets, or edit worker notes before anything is written.
3. **Figures** (PDFs) — figure regions are rendered from the page (so vector labels
   survive), a vision call decides whether each is examinable and lists its labelled
   parts, and useful ones become image-backed `figure_recall` tasks. Images ship inside
   the `.apkg`.
4. **Write** — each task is one worker call: the strategy's grammar + the planner's notes
   + the unit text **verbatim** (never a lossy summary) + a hint of what sibling tasks
   cover. Tasks run concurrently (`PIPELINE_MAX_WORKERS`). Truncated outputs are retried
   with a smaller ask. Every card carries a verbatim `source_quote`.
5. **Critique** — two cheap calls per batch of 20 cards. A *cold pass* answers each card
   front with no source (exposes prompts that leak their answer, gives a difficulty
   signal); a *judge pass* rules keep / rewrite / drop on support, atomicity, ambiguity
   and leakage. Dropped cards are kept as `deleted` with `critic:*` tags so you can
   restore them.
6. **Reconcile** — surviving cards are embedded, clustered by cosine similarity, and a
   model decides per cluster what to keep. Exact duplicates are removed first.
7. **Coverage** — per unit, the model lists testable facts no card covers; important gaps
   spawn one bounded round of gap-filler tasks (which also go through the critic).
8. **Finish** — cards are ordered by unit prerequisites so foundations are introduced
   first, and tagged with unit, strategy, and difficulty.

### Card strategies

| Strategy | Use for |
|---|---|
| `general` | Mixed prose; the safe default |
| `definition_sweep` | Term-heavy material; one card per term plus reverse cards for key terms |
| `formula_derivation` | Equations: what it computes, each symbol, validity conditions, limiting cases |
| `mechanism_chain` | Processes and pathways; one card per link |
| `compare_contrast` | Confusable siblings; discriminator cards per (item, dimension) |
| `worked_example` | Problem solving; method selection and next-step reasoning |
| `key_claims` | Narrative / argumentative prose; claims, causes, evidence |
| `pitfall_edge_case` | Exceptions, caveats, common mistakes |
| `figure_recall` | Diagrams and charts, with the image on the card |

All strategies share one base rule set (grounding, minimum-information, cloze hygiene,
math format) placed first in the prompt so provider prompt caches hit across tasks.

### Everything is traced

Every phase and task is a `PipelineTask` row; every model call is an `LLMRun` under its
task. The status page polls `/decks/<id>/progress.json` and renders the tree live —
what the planner decided, which workers are in flight, what the critic dropped, tokens
and cost per phase. The editor's **Run insights** panel shows the same after the fact.

### Content-addressed cache

Worker, critic, vision and coverage results are cached on a hash of
(role, model, prompt version, inputs). Re-running a deck, or generating another deck
from the same chapter, costs nothing for any task whose inputs are unchanged.

### Close the loop with Anki

The export stamps every note with a stable guid. Study the deck, then export it back
from Anki (*File → Export*, include scheduling) and upload it on the editor page. Review
stats (reps, lapses, "again" rate) are attached to each card; cards with ≥2 lapses or a
≥40% again-rate are flagged **struggling**. **Coach** sends them to the model with their
stats and the source unit, which rewrites or splits them (marked *Needs review*).

## Tech stack

- Flask, Flask-Login, Flask-SQLAlchemy, Flask-Migrate, Flask-WTF (CSRF)
- OpenRouter chat completions (structured outputs, tools, vision) + embeddings
- Default model: **`openai/gpt-5.6-luna`** for every role (1M context, tools, vision,
  ~$0.20/M input); any role can be overridden per env var
- Pydantic validation, NumPy for clustering, genanki export
- PDF: pymupdf4llm + pymupdf-layout (Markdown with page offsets), pymupdf figure
  rendering, pypdf fallback
- No queue or broker: generation runs on a background thread inside the web process,
  and the status page polls run state from the database
- SQLite by default (WAL mode; missing columns are added automatically on startup)

## Quick start (local)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy example.env .env      # set SECRET_KEY and OPENROUTER_API_KEY
python run.py
```

Open `http://127.0.0.1:5000`. Set `AUTH_REQUIRED=false` for a no-login demo mode.

## Quick start (Docker)

```bash
cp example.env .env        # then set SECRET_KEY and OPENROUTER_API_KEY in .env
docker compose up --build
```

Open `http://localhost:5000`. Compose runs a single `web` container; the SQLite
database and uploads live in the `ankigpt-data` volume. Run the tests in the
container with:

```bash
docker compose exec web sh -c "pip install -q -r requirements-dev.txt && python -m pytest tests/ -q"
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret` | Flask session/CSRF secret. Change it. |
| `DATABASE_URL` | `sqlite:///instance/ankigpt.db` | SQLAlchemy URL. |
| `AUTH_REQUIRED` | `true` | `false` runs against a local `demo@local` user. |
| `OPENROUTER_API_KEY` | | Required. |
| `OPENROUTER_MODEL` | `openai/gpt-5.6-luna` | Default model for every role. |
| `OPENROUTER_MODEL_{MAPPER,PLANNER,WORKER,CRITIC,RECONCILE,VISION}` | | Per-role overrides (e.g. a stronger planner). |
| `OPENROUTER_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | Used for duplicate clustering. |
| `OPENROUTER_REASONING_{PLANNER,MAPPER,WORKER,CRITIC,RECONCILE,VISION}` | `medium`/`low` | Reasoning effort per role; empty omits the parameter. |
| `OPENROUTER_TEMPERATURE` | | Unset by default — reasoning models reject it. |
| `OPENROUTER_MAX_TOKENS` | `16000` | Output cap per call (billing is per token used). |
| `OPENROUTER_TIMEOUT_SECONDS` / `_MAX_RETRIES` / `_RETRY_BACKOFF_SECONDS` | `180` / `2` / `1.5` | HTTP behaviour. |
| `PIPELINE_MAX_WORKERS` | `6` | Concurrent model calls. |
| `PIPELINE_PLANNER_MAX_TURNS` | `14` | Planner agent loop cap. |
| `PIPELINE_UNIT_MAX_CHARS` | `14000` | Larger units are split deterministically. |
| `PIPELINE_CRITIC_ENABLED` | `true` | Critic phase. |
| `PIPELINE_COVERAGE_ENABLED` | `true` | Coverage audit + gap fill. |
| `PIPELINE_EMBED_DEDUPE_ENABLED` | `true` | Embedding-based duplicate clustering. |
| `PIPELINE_DEDUPE_THRESHOLD` | `0.90` | Cosine threshold for a duplicate cluster. |
| `PIPELINE_CACHE_ENABLED` | `true` | Content-addressed result cache. |
| `PIPELINE_FIGURES_ENABLED` / `PIPELINE_MAX_FIGURES` | `true` / `24` | Figure extraction from PDFs. |
| `MAX_SOURCE_CHARS` | `400000` | Cap on source length (`0` disables). |
| `UPLOAD_MAX_MB` / `UPLOAD_FOLDER` | `50` / `instance/uploads` | Uploads. |
| `GENERATION_IN_THREAD` | `true` | Run generation on a background thread (tests set `false` to run inline). |

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/decks` | Library |
| `GET,POST` | `/decks/new` | Create a deck from text/PDF (figures extracted here) |
| `GET,POST` | `/decks/<id>/preview` | Review source, brief the planner, start the run |
| `GET` | `/decks/<id>/status` | Live run trace |
| `GET` | `/decks/<id>/progress.json` | Trace as JSON (polled by the status page) |
| `GET,POST` | `/decks/<id>/plan` | Review / edit / run the work order; re-plan |
| `GET` | `/decks/<id>` | Card editor with run insights |
| `POST` | `/decks/<id>/reviews` | Import an Anki package with review history |
| `POST` | `/decks/<id>/coach` | Rewrite struggling cards |
| `POST` | `/decks/<id>/export` | Export `.apkg` (with figure media) |
| `POST` | `/cards/<id>` · `/cards/<id>/improve` · `/cards/bulk` | Edit, AI-improve, bulk (delete/restore/tag/regenerate unit/coach) |
| `GET` | `/figures/<id>.png` | Figure image (owner-scoped) |

## Project layout

```text
app/
  services/
    pipeline/
      orchestrator.py   the run: phases, persistence, status transitions
      document_map.py   phase 0 — skeleton + mapper
      planner.py        phase 1 — tool-using planning agent + invariants
      strategies.py     card grammars
      workers.py        phase 2 — worker prompt + call
      critic.py         phase 3 — cold pass + judge, coach diagnosis
      reconcile.py      phase 4 — embedding clusters, coverage audit
      figures.py        PDF figure extraction + vision analysis
      feedback.py       Anki review import + coach
      routing.py        role -> model client
      cache.py          content-addressed cache
      parallel.py       thread-pool fan-out
      trace.py          PipelineTask / LLMRun tracing
    llm.py              OpenRouter HTTP: chat, tools loop, embeddings, JSON repair
    deckgen.py          regenerate a unit, improve a card
    pdf.py, export.py, validators.py, chunking.py, schemas.py
  routes/, templates/, static/, models.py, config.py, tasks.py
tests/                  72 tests incl. an end-to-end run against a scripted model
```

## Data model

- `Deck` — source, settings (`settings_json`), and the run (`run_json`: plan, phase,
  totals, stats, last error). Status: `draft → processing → (planned →) processing → ready | failed`.
- `Source` — one **unit** of the document map (kind, density, pages, prerequisites, skip).
- `Card` — with `strategy`, `difficulty`, `source_quote`, `critic_json`, `order_key`,
  `guid`, `review_stats_json`, and links to its task, unit, and figure.
- `PipelineTask` — the trace tree (phase → task), with status, model, tokens, cost.
- `LLMRun` — every model call, attached to its task.
- `Figure` — images pulled from a PDF plus the vision analysis.
- `GenerationCache` — content-addressed results.

## Testing

```powershell
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The suite includes a scripted model (`tests/conftest.py::FakeLLM`) that drives the whole
pipeline — planner tool calls, workers, critic verdicts, duplicate resolution, coverage
audit, and the coach — without network access.

## Troubleshooting

- **Generation failed** — the status page shows the exact reason and what ran before the
  failure. Auth/quota errors abort immediately and never wipe an existing deck.
- **Planner produced a poor plan** — tick *Review the plan before writing* and adjust, or
  set `OPENROUTER_MODEL_PLANNER` to a stronger model.
- **Cards dropped by the critic** — filter the editor by *Deleted*; each carries the
  critic's reason. Restore anything you disagree with.
- **Review import finds no cards** — export this deck from AnkiGPT first, study it, then
  export it back from Anki *with scheduling*. Cards are matched by note guid.
- **Compressed Anki packages** — newer Anki exports use zstd; `zstandard` is in
  `requirements.txt`, or tick *Support older Anki versions* when exporting.
- **Scanned PDFs** — no OCR; only text-based PDFs extract.

## Security notes

- Rotate any API key that ever touched git history.
- Every deck/card/figure route is scoped to the current user; cross-user access 404s.
- CSRF protection on all state-changing requests (HTMX sends the token as a header).
- Uploaded PDFs are extracted and deleted immediately; figure images live in the DB.
