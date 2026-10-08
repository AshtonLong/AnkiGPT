# Architecture

[README](../README.md) · [User guide](user-guide.md) · [Setup](setup.md) · [Architecture](architecture.md)

## How a deck is generated

```
source ──▶ MAP ──▶ FIGURES ──▶ PLAN ──▶ WRITE ──▶ CRITIQUE ──▶ COVERAGE ──▶ RECONCILE ──▶ FINISH
           │         │          │         │          │            │             │
     document map  vision reads │     N workers   cold-answer  audit each    embeddings
     units, kinds, figures from │     in parallel + judge on   unit; gap     cluster +
     density,      the PDF      │     one strategy every card  cards pass    model merges
     prerequisites              │     each                     a review
                                ▼
        an agent with tools: read_unit · search_source · spawn_task · skip_unit ·
                             spawn_figure_task · skip_figure · finish_plan
```

1. **Map** — the source is split on headings into candidates; one cheap model call groups
   them into semantic *units* with a kind (definitions, formulas, procedure, comparison,
   worked example…), a density score, prerequisites, and skip verdicts for front matter,
   references, and recaps. Short sources skip the model and become one unit.
   - **Cheat sheet** (optional, off by default) — with **Make a cheat sheet first**
     ticked, each live unit is rewritten as the section of a cheat sheet a student could
     bring into the exam: every examinable concept in its barest form, edge cases
     included, in plain language. A concept keeps at most one example, and only one the
     source gives; nothing is added from outside the source. The unit's text is then
     *replaced* by that section, so the planner sizes, workers write from, the critic
     judges against, and the coverage audit back-fills from the cheat sheet alone;
     nothing later can re-inflate what it cut. One cached call per unit, in parallel. A
     unit whose call fails keeps its full text; a unit with nothing exam-critical is
     skipped; an entirely empty sheet fails the run before anything is replaced.

     The sheet keeps the source's diagrams. The writer is told what each diagram in
     its section shows and places a `[[Figure N]]` marker line for it; a diagram it
     leaves out is added at the end of the section, because the vision pass saw the
     image and the writer did not. Once the planner has ruled on the figures, the
     diagrams it gave no cards come off the sheet again, so the sheet and the image cards
     are the same set. `GET /decks/<id>/cheat-sheet` renders the sheet as a printable
     page with each image where its marker sits. The writer is asked for maths as
     `\( ... \)` and `\[ ... \]`, like the cards; the page marks each formula (`$...$`
     and `$$...$$` are read too) and KaTeX, served from `static/vendor/katex`, typesets
     it on screen and in print.
2. **Figures** (PDFs) — figure regions are rendered from the page (so vector labels
   survive) and each gets one vision call. It says whether the figure is material to
   learn (exercises and decoration are not), lists its labelled parts and the facts it
   conveys, and says what it adds beyond the text around it, with a suggested number of
   cards. Nothing is decided here; the analyses go to the cheat sheet and the planner.
   - A figure is attached to its unit by page. A page with a figure and no text
     continues the unit before it. Figures in a skipped unit are not read.
   - A figure that is only text set as an image (a table of formulas, a block of
     rules) is not treated as a picture. The vision pass transcribes it and the
     transcript joins the text of its unit, where it is condensed, planned, written and
     de-duplicated like any other text. Shown on a card, such an image would print the
     answer above the question.
3. **Plan** — the planner is a real tool-using loop. It reads units it is unsure about,
   searches the source, and *spawns tasks*: which units, which **card strategy**, how many
   cards, and specific notes for the worker. It rules on every figure too: a
   `figure_recall` task of its own, or no cards. So every card count is in one plan,
   weighed against the text, and visible on the plan review.
   - The budget it starts from is an **estimate, not a cap**: what the text is worth by
     size and density plus what the vision pass thinks each figure adds. The planner is
     told to go over it where the material holds more and under it where it holds less.
     A deck size the student typed is a stronger suggestion: stay close to it.
   - What is fixed are the safety rails. Every live unit ends up covered (an uncovered
     one gets a default task), every figure ends up decided (an undecided one follows
     the vision pass), and a plan beyond 2.5× the estimate is scaled back. If the model
     never calls a tool, a single structured-output plan is used instead.

   Tick **Review the plan before writing** and the run pauses so you can skip tasks,
   change strategies, resize budgets, or edit worker notes before anything is written.
4. **Write** — each task is one worker call: the strategy's grammar + the planner's notes
   + the unit text **verbatim** (the source itself, or its cheat-sheet section when that
   option is on) + a hint of what sibling tasks cover. Tasks run concurrently
   (`PIPELINE_MAX_WORKERS`). The card count a worker is given is an estimate it may go
   over or under. Figure tasks run after the text tasks, and each is shown the cards its
   unit already has, so it writes what the figure adds instead of repeating them. Images
   ship inside the `.apkg`. Truncated outputs are retried with a smaller ask. Workers are
   prompted to attach a verbatim `source_quote`; a card of a task that read several units
   is filed under the unit its quote comes from.
5. **Critique** — two cheap calls per batch of 20 cards. A *cold pass* answers each card
   front with no source (exposes prompts that leak their answer, gives a difficulty
   signal); a *judge pass* rules keep / rewrite / drop on support, atomicity, ambiguity,
   leakage, and whether the card is worth learning at all (a detail of one worked
   example is not). Figure cards are judged against the unit text *plus* what the vision
   pass read off the figure; the text alone rarely states what a diagram shows. Dropped
   cards are kept as `deleted` with `critic:*` tags so you can restore them.
6. **Coverage** — per unit, the model reads the unit next to every card written from it
   and lists testable facts no card covers. Important gaps spawn one round of gap-filler
   tasks, whose writers see the unit's existing cards. A back-fill card then has to pass
   two checks: the critic, and a **back-fill review** that sees the candidates next to
   the cards the unit already has and lets in only the ones that test something new and
   worth asking. When in doubt it rejects. A rejected card is kept as `deleted` with the
   reason (`backfill:rejected`); if the review itself fails, the cards wait as
   `needs_review` instead of joining the deck unreviewed.
7. **Reconcile** — surviving cards, back-fill included, are embedded, clustered by cosine
   similarity, and a model decides per cluster what to keep. Exact duplicates are removed
   first.
8. **Finish** — cards are ordered by unit prerequisites so foundations are introduced
   first, and tagged with unit, strategy, and difficulty. The run records the estimate it
   started from and where the kept cards came from (text, figures, back-fill), so going
   past the estimate is visible and explained.

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

### The model a run uses

`pipeline/catalog.py` lists the models a user can pick, each under the company that
makes it, with the `reasoning.effort` levels it takes (copied from
`reasoning.supported_efforts` in OpenRouter's model list). The server's
`OPENROUTER_MODEL` is the default. A user's pick under **My profile → AI model** is
stored in `User.openrouter_model` and runs every role of that user's runs; with no pick,
the roles follow the server's configuration, per-role overrides included. Every model is
called through OpenRouter under its OpenRouter id, so adding one is an entry in
`MODELS`, plus a logo in `company_logo` (`templates/partials/ui.html`) when its company
is new.

### Reasoning effort per agent

Each call is made by one of twelve agents. Most are a role; the critic role runs the
cold reader, the judge, the back-fill reviewer, the card improver and the coach, and the
reconcile role runs the duplicate resolver and the coverage auditor
(`routing.AGENT_ROLES`). An agent takes its
model and its default effort from its role's configuration. A user can set the effort
of any single agent under **My profile → Advanced** (`pipeline/efforts.py`); only the
agents they moved are stored, in `User.agent_efforts_json`, and those win for that
user's runs. The sliders offer the levels of the user's model, and every effort, a
user's or a role's default, is fitted to the model it is sent to
(`catalog.fit_effort`): a level the model lacks becomes the next one up that it has, or
its highest when there is none above. A model the catalog does not list is sent what
was asked for.

### Content-addressed cache

Cheat-sheet, worker, critic, vision, coverage and back-fill review results are cached on a hash of
(role, model, prompt version, inputs), plus the reasoning effort, as sent, of any agent its user
moved off the default. A cache hit avoids a provider call for that task. Mapping, planning, embeddings,
and changed inputs can still incur calls; a repeated deck is not guaranteed to be free.
The cache is database-wide, not scoped to an individual user.

## Project layout

```text
app/
  services/
    pipeline/
      orchestrator.py   the run: phases, persistence, status transitions
      document_map.py   phase 0 — skeleton + mapper
      cheatsheet.py     optional — condense each unit to an exam cheat sheet, diagrams kept
      figures.py        PDF figure extraction + vision analysis
      planner.py        the plan — tool-using planning agent + invariants, figures included
      strategies.py     card grammars
      workers.py        write — worker prompt + call
      critic.py         critique — cold pass + judge, coach diagnosis
      reconcile.py      coverage audit + back-fill review, embedding clusters
      feedback.py       Anki review import + coach
      routing.py        role -> model client, and each agent's reasoning effort
      catalog.py        the models a user can pick, and the effort levels each takes
      efforts.py        the effort each user set per agent (the Advanced panel)
      cache.py          content-addressed cache
      parallel.py       thread-pool fan-out
      trace.py          PipelineTask / LLMRun tracing
    llm.py              OpenRouter HTTP: chat, tools loop, embeddings, JSON repair
    credentials.py      each user's OpenRouter API key, encrypted at rest
    deckgen.py          regenerate a unit, improve a card
    pdf.py, export.py, validators.py, chunking.py, schemas.py
  routes/, templates/, static/, models.py, config.py, tasks.py
  desktop.py            everything desktop mode adds (see docs/desktop.md)
desktop-app/            the Windows app: Electron shell, backend entry point, build scripts
tests/                  unit, route, privacy, database, and scripted pipeline tests
```

## Data model

- `User` — credentials, display name, bio, avatar color, deck ownership, the
  user's encrypted OpenRouter API key, the model they picked, and the reasoning effort
  they set per agent.
- `Deck` — source, settings (`settings_json`), and the run (`run_json`: plan with its
  figure decisions, phase, totals, stats, last error). Status: `draft → processing → (planned →) processing → ready | failed`.
- `Source` — one **unit** of the document map (kind, density, pages, prerequisites, skip).
  Its `text` is the cheat-sheet section when the deck was generated with that option;
  a `[[Figure N]]` line in it is a diagram kept on the sheet.
- `Card` — with `strategy`, `difficulty`, `source_quote`, `critic_json`, `order_key`,
  `guid`, `review_stats_json`, and links to its task, unit, and figure.
- `PipelineTask` — the trace tree (phase → task), with status, model, tokens, cost.
- `LLMRun` — every model call, attached to its task.
- `Figure` — images pulled from a PDF plus the vision analysis.
- `GenerationCache` — content-addressed results.

Everything is stored in a single SQLite database (WAL mode).

## Runtime and failure handling

Generation uses background threads in the web process, with a bounded thread pool
for parallel model work. There is no durable job queue or automatic restart recovery.
Keep the process alive while a deck builds. Mapping and planning must succeed before
previous cards are replaced; later failures can leave a partial new run. Check the
trace before retrying. AI improvement, bulk regeneration, and coaching execute within
their HTTP request.

Missing tables and columns are created on startup. This additive helper does not
perform arbitrary schema migrations, backfills, or constraint changes, and it never
drops anything: a database created by an older version keeps its unused billing
columns and `usage_record` table.
