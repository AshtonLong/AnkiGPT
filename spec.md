# AnkiGPT — Product & Technical Specification

## 1) Summary
AnkiGPT is a full web application that ingests educational content (pasted text or uploaded PDF) and produces a comprehensive Anki deck in either **Basic** (front/back) or **Cloze** format. Users can **review and edit cards in-browser** before exporting a downloadable `.apkg` deck file.

The application uses **OpenRouter** to call a configurable LLM for extraction, summarization, and card generation.

---

## 2) Goals / Non-goals

### Goals
- Turn raw learning material into a high-quality Anki deck quickly.
- Support both **Basic** and **Cloze** card styles, with user-configurable settings.
- Provide a fast, simple **card editor** (bulk edit, inline edits, delete/merge, regenerate).
- Export a standards-compatible **Anki `.apkg`** file.
- Keep prompt outputs **structured and validated** to minimize broken decks.
- Offer an MVP that can run locally (SQLite) and deploy easily (Postgres).

### Non-goals (MVP)
- Spaced repetition scheduling inside the webapp (Anki already does this).
- Full WYSIWYG/Markdown-rich editor (basic formatting only).
- Multi-language OCR for scanned PDFs (optional future).
- Collaborative multi-user sharing/versioning (future).

---

## 3) Target Users & Primary Use Cases

### Personas
- **Student**: uploads lecture slides PDF, generates cloze deck for exams.
- **Professional learner**: pastes documentation notes, generates basic Q/A deck.
- **Teacher/tutor**: builds a deck from course readings and edits for clarity.

### Primary flows
1. Upload PDF or paste text → preview extracted text → select deck style/settings.
2. Generate deck (async) → view cards → edit/regenerate problematic cards.
3. Export `.apkg` → import into Anki → study.

---

## 4) Core Features (MVP)

### 4.1 Content ingestion
- **Text input**: paste or type into a large text area.
- **PDF upload**:
  - Extract text from PDF.
  - Show extraction preview with basic stats (pages, chars, detected headings).
  - Allow user to choose page ranges (optional).

Recommended libraries:
- `pypdf` or `pdfplumber` for text extraction.
- Optional later: OCR via Tesseract for scanned PDFs.

### 4.2 Deck generation settings
- Card type: `basic` or `cloze`.
- No user-specified card count: the system should generate **enough cards** to comprehensively cover all main topics.
- Detail level: full technical detail with additional intuition/“why” cards.
- Focus controls:
  - “Prioritize definitions / equations / procedures / lists”.
  - “Exclude: trivia / dates / overly detailed facts”.
- Optional: user-provided glossary / must-include terms.

### 4.3 LLM-powered card generation (OpenRouter)
Pipeline:
1. **Preprocess**: clean text, normalize whitespace, remove repeated headers/footers.
2. **Segment**: chunk text by headings/paragraphs, ensuring token limits.
3. **Generate**:
   - For each chunk, call LLM to produce candidate cards in strict JSON.
4. **Deduplicate/merge**:
   - Remove near-duplicates.
   - Merge complementary cards.
5. **Validate & repair**:
   - Validate JSON with a schema (Pydantic).
   - If invalid: run an automatic “repair JSON” LLM call (bounded retries).
6. **Postprocess**:
   - Enforce cloze syntax.
   - Ensure Basic cards have concise fronts.
   - Tagging by section/source.

OpenRouter integration notes:
- Store API key in server config (environment variable).
- Allow selecting model from a curated list (admin-configurable).
- Track token usage/cost (best-effort; store raw OpenRouter usage fields).

### 4.4 Card review & editor
- Cards list/table view:
  - Search/filter by tag, type, source chunk, “needs review”.
  - Bulk select → delete / tag / regenerate.
- Inline editor:
  - Basic: edit `front`, `back`.
  - Cloze: edit `text` with cloze markers `{{c1::...}}`.
  - Add notes field / extra context (optional).
- Regenerate actions:
  - Regenerate selected cards from the same source chunk.
  - “Improve wording” for a card.
- Validation UI:
  - Highlight invalid cloze markup (unbalanced braces, missing `cN` indices).
  - Warn if fields are empty or too long.

### 4.5 Export `.apkg`
- Generate `.apkg` using `genanki`.
- Include:
  - Deck name, optional description.
  - Card model mapping:
    - Basic: Front/Back
    - Cloze: Text/Extra (optional)
  - Tags (section tags; user tags).
  - Media (MVP: none; later: images extracted from PDFs).
- Provide download link once generated.

---

## 5) UX / Pages

### Style (UI + Cards)
The overall style should feel **clean, modern, and linear**: a focused, step-by-step flow with generous whitespace, clear typography, and minimal visual noise. This style applies both to the **webapp UI** and to the **generated Anki cards** (content formatting and templates).

Guidelines:
- Prefer a single-column “wizard” feel for creation/generation; keep choices contextual and progressively disclosed.
- Use consistent typography scale, spacing, and subtle dividers; avoid heavy borders/shadows.
- Keep primary actions obvious (one primary button per view); reduce secondary actions to links/menus.
- Favor plain, readable card templates (minimal HTML), consistent punctuation, and short, scannable fields.

### Public
- Landing page: value prop, “Get started”.
- Login / signup (if auth enabled for deployment).

### App
- **New Deck**: paste text / upload PDF.
- **Extract Preview**: show extracted text; choose settings.
- **Generation Status**: progress (chunks completed, estimated time).
- **Deck Editor**:
  - Cards table + filters.
  - Card detail editor panel.
  - Regenerate / bulk operations.
- **Export**: deck metadata + download `.apkg`.
- **History**: previous decks, status, re-export.

Front-end approach (recommended for Flask MVP):
- Server-rendered pages (Jinja2) + **HTMX** for fast interactions (search/filter/edit).
- Minimal styling using Bootstrap or Tailwind (pick one early).

---

## 6) System Architecture

### 6.1 Components
- **Flask Web Server**
  - Handles auth, uploads, deck CRUD, editor actions, export.
- **Worker**
  - Runs long tasks: PDF extraction (if heavy), chunking, LLM calls, export.
  - Recommended: Celery + Redis (or RQ + Redis).
- **Database**
  - Dev: SQLite
  - Prod: Postgres
- **Object storage (optional)**
  - Store uploaded PDFs and exported `.apkg` files (S3-compatible).
  - MVP can store on disk in `instance/` with TTL cleanup.

### 6.2 High-level data flow
1. User creates Deck → uploads content.
2. Extraction produces canonical `source_text`.
3. Worker chunks text → calls LLM per chunk → stores cards.
4. User edits cards → final export creates `.apkg` → download.

---

## 7) Data Model (Proposed)

### `users`
- `id`, `email`, `password_hash`, `created_at`

### `decks`
- `id`, `user_id`, `title`, `card_style` (`basic|cloze`)
- `status` (`draft|processing|ready|failed`)
- `source_type` (`text|pdf`)
- `source_text` (canonical extracted/cleaned text)
- `settings_json` (generation settings)
- `created_at`, `updated_at`

### `sources`
Represents chunked segments from `source_text`.
- `id`, `deck_id`
- `idx` (ordering)
- `title` (heading/section label, optional)
- `text` (chunk content)
- `hash` (for caching/dedup)

### `cards`
- `id`, `deck_id`, `source_id` (nullable for manually added cards)
- `type` (`basic|cloze`)
- `front`, `back` (basic)
- `cloze_text`, `extra` (cloze)
- `tags` (array-ish; store as normalized join table or JSON)
- `status` (`ok|needs_review|deleted`)
- `created_at`, `updated_at`

### `llm_runs`
Audit/debug.
- `id`, `deck_id`, `source_id`, `model`, `prompt_version`
- `input_tokens`, `output_tokens`, `cost_estimate`
- `request_json`, `response_text`, `parsed_json`, `error`
- `created_at`

### `exports`
- `id`, `deck_id`
- `path` or `object_key`, `size_bytes`, `created_at`

---

## 8) API / Routes (Sketch)

### HTML routes (Flask)
- `GET /` landing
- `GET/POST /auth/login`, `/auth/signup`, `/auth/logout`
- `GET /decks` list
- `GET/POST /decks/new` create from text/pdf
- `GET /decks/<deck_id>` deck editor
- `POST /decks/<deck_id>/generate` start generation job
- `GET /decks/<deck_id>/status` polling endpoint (HTMX)
- `POST /cards/<card_id>` update card fields
- `POST /cards/bulk` bulk actions (delete/tag/regenerate)
- `POST /decks/<deck_id>/export` start export job
- `GET /exports/<export_id>/download` download `.apkg`

### JSON endpoints (optional)
If a SPA is preferred later, mirror the above with `/api/...`.

---

## 9) LLM Prompting & Output Contract

### 9.1 Output JSON schema (conceptual)
For each chunk, the model returns:
- `cards`: list of card objects
- Each card includes:
  - `type`: `"basic"` or `"cloze"`
  - `front`/`back` OR `cloze_text`/`extra`
  - `tags`: list of strings
  - `rationale` (optional, not exported; used for debugging)
  - `source_quotes` (optional, short; avoid excessive copying)

### 9.2 Guardrails
- Enforce strict JSON parsing; no prose outside JSON.
- Bound cloze deletions:
  - Prefer 1–2 deletions per sentence.
  - Use consistent numbering, starting at `c1` within each note (or global strategy).
- Avoid overly long card fronts; split multi-part questions.
- Prefer recall-oriented prompts (“What is…”, “How do you…”, “Why does…”).
- Avoid ambiguous questions:
  - Include subject, scope, and conditions in the front.
  - Avoid vague pronouns (it/they/this) without context.
  - Split multi-answer prompts into multiple precise cards.
- Scope control:
  - Use only facts explicitly present in the provided content.
  - Do not add external knowledge or assumptions.

### 9.3 Chunking strategy
- Chunk by headings when possible.
- Keep within a token budget (e.g., ~2k–4k tokens per chunk, model-dependent).
- Include chunk title + 1–2 surrounding context lines if it improves coherence.

### 9.4 Caching
- Cache by `(model, prompt_version, source_hash, settings_hash)` to avoid re-billing.

### 9.5 Style requirements for generated cards
To match the “clean modern linear” feel:
- Use consistent, simple formatting (plain text preferred; minimal HTML only when needed for line breaks).
- Keep **one main idea per card**; split multi-step lists into multiple cards.
- Avoid excessive emphasis (no all-caps, minimal bold); prefer clarity over flair.
- Use consistent patterns:
  - Definitions: `Term` → concise definition + 1 key distinguishing detail.
  - Procedures: short “What are the steps…” cards, one step sequence per card.
  - Comparisons: single contrast per card (A vs B).
- Cloze cards should read naturally with 1–2 deletions per sentence and minimal nesting.
- Include a mix of technical-detail cards and intuition/why cards for key concepts.

---

## 10) Quality Heuristics (Deck “Comprehensiveness”)
The generator should aim to cover:
- Definitions and key terms
- Important distinctions/contrasts
- Step-by-step procedures
- Core formulas/equations + variables meaning
- Common misconceptions/pitfalls
- Example-driven applications (lightweight)

Automatic checks (post-generation):
- Empty fields
- Duplicate fronts / near-duplicate cloze sentences
- Too-long fields (configurable)
- Too many cards from one chunk (skew)
- Cloze syntax validity
- Style consistency (fronts concise, minimal formatting, one-concept-per-card)

---

## 11) Security, Privacy, and Compliance
- Treat uploaded content as sensitive:
  - TLS in production
  - Private access per user
  - Configurable retention (auto-delete uploaded PDFs after N days)
- Do not log raw content by default (store in DB as required for product; but keep logs clean).
- Store OpenRouter API keys server-side only; never expose to browser.
- Rate limiting:
  - Per-user generation quotas (cards/day or tokens/day)
  - Upload size limits
- CSRF protection for form posts; secure session cookies.

---

## 12) Observability
- Structured logs around deck jobs (start/end/error).
- Track per-deck:
  - elapsed time, chunks processed
  - LLM usage/cost
  - export success/failure
- Admin/debug UI (optional) to inspect failed LLM runs.

---

## 13) Deployment

### Local dev
- `flask --app anki_gpt run`
- SQLite + local file storage.
- Redis optional (only if using Celery/RQ).

### Production (recommended)
- Docker container(s):
  - web (Flask + gunicorn)
  - worker (Celery/RQ)
  - Redis
  - Postgres
- Reverse proxy (nginx/managed) + HTTPS.
- S3-compatible storage for uploads/exports (optional but ideal).

---

## 14) MVP Milestones

### Milestone A — Skeleton app
- Flask app scaffold + auth + deck CRUD.
- Upload PDF + extract text preview.

### Milestone B — Generation pipeline
- Chunking + OpenRouter LLM calls.
- Store cards + show status page.

### Milestone C — Editor
- Cards table + inline editing + bulk delete.
- Basic validation checks.

### Milestone D — Export
- `.apkg` generation with `genanki`.
- Download endpoint.

---

## 15) Open Questions (Decisions to make early)
- Do we require accounts for MVP, or allow anonymous “single deck” sessions?
- Front-end choice: Bootstrap vs Tailwind; pure server-rendered vs HTMX-heavy.
- Cloze numbering strategy: per-note reset (`c1...`) vs global unique.
- PDF image extraction support (later) and how to store media.
- Regeneration UX: do we preserve user edits or replace fully?
