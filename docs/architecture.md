# Architecture

[README](../README.md) · [User guide](user-guide.md) · [Setup](setup.md) · [Architecture](architecture.md)

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
   with a smaller ask. Workers are prompted to attach a verbatim `source_quote`; check the source when reviewing cards.
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
(role, model, prompt version, inputs). A cache hit avoids a provider call for that task. Mapping, planning, embeddings,
and changed inputs can still incur calls; a repeated deck is not guaranteed to be free.
The cache is database-wide, not scoped to an individual user.

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
    billing.py          plans, page metering, Stripe checkout/portal/webhook sync
    deckgen.py          regenerate a unit, improve a card
    pdf.py, export.py, validators.py, chunking.py, schemas.py
  routes/, templates/, static/, models.py, config.py, tasks.py
tests/                  unit, route, privacy, database, and scripted pipeline tests
```

## Data model

- `User` — credentials, display name, bio, avatar color, deck ownership, and plan.
- `Deck` — source, settings (`settings_json`), and the run (`run_json`: plan, phase,
  totals, stats, last error). Status: `draft → processing → (planned →) processing → ready | failed`.
- `Source` — one **unit** of the document map (kind, density, pages, prerequisites, skip).
- `Card` — with `strategy`, `difficulty`, `source_quote`, `critic_json`, `order_key`,
  `guid`, `review_stats_json`, and links to its task, unit, and figure.
- `PipelineTask` — the trace tree (phase → task), with status, model, tokens, cost.
- `LLMRun` — every model call, attached to its task.
- `Figure` — images pulled from a PDF plus the vision analysis.
- `GenerationCache` — content-addressed results.
- `UsageRecord` — one metered generation run (pages charged). Kept when its deck is
  deleted. The user's Stripe subscription state is mirrored onto `User` by the webhook.


## Runtime and failure handling

Generation uses background threads in the web process, with a bounded thread pool
for parallel model work. There is no durable job queue or automatic restart recovery.
Keep the process alive while a deck builds. Mapping and planning must succeed before
previous cards are replaced; later failures can leave a partial new run. Check the
trace before retrying. AI improvement, bulk regeneration, and coaching execute within
their HTTP request.

Missing tables and columns are created on startup. This additive helper does not
perform arbitrary schema migrations, backfills, or constraint changes.
