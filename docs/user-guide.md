# User guide

[README](../README.md) · [Setup](setup.md) · [Architecture](architecture.md) · [Desktop app](desktop.md)

Using the Windows desktop app? There is no account to create, and **My profile** is
called **Settings**. [Desktop app](desktop.md) lists the other differences.

## 1. Add your material

Create an account or sign in, then choose **New deck**. Give it a title and select
**Basic** (front/back), **Cloze** (fill in the blank), or **Mixed** cards.
Paste text or upload PDFs. To build one deck from several PDFs, such as a run of
lectures, add them all (pick or drop a few at a time) and put them in order by
dragging them or with the arrow buttons. They are combined into one document in that
order, and source review lists each file with the pages it became. For PDFs,
optionally select a page range using 1-based page numbers; with several PDFs the
pages count through the combined document. Scanned documents need text extraction elsewhere first; the app has
no OCR workflow. Only PDF uploads are supported.

The default upload limit is 50 MB in total, across up to 20 PDFs. Sources longer than 400,000 characters are
truncated with a warning; use a smaller page range if the omitted material matters.
Both limits are configurable. Continue to source review to check the extraction.

## 2. Brief the planner

The source preview displays the first 4,000 characters; generation uses the full
stored source, subject to the source cap above. Add exam/context, focus areas,
exclusions, and must-include terms. Leave deck size on **Auto** or set an approximate
target. On Auto no card count reaches the AI at all: the planner decides what each task
covers, never how many cards it makes, and each writer writes a card for each point that
earns one. (A number in a model's context, even one called an estimate, is one it writes
towards.) A target you set is passed on and followed closely, and the coverage back-fill
then adds only what an examiner would very likely ask. The finished run shows how many
cards came from the text, from figures, and from the back-fill.

Turn on **Make a cheat sheet first** when the full source would be information
overload. Before any cards are planned, the source is boiled down to what you would fit
on a cheat sheet you were allowed to bring into the exam: every concept in its barest
form, with its edge cases, and without the background, repetition, and academic padding.
The sheet is built from first principles: every fundamental law, assumption and
underlying reason your source states is kept, and each result is tied to the one it
comes from. A concept keeps at most one example, and only if your source gives one; nothing is
added that your source does not say. Diagrams from a PDF that earn cards stay on the
sheet. Cards are written from that alone. Expect fewer, higher-yield cards; detail the cheat sheet leaves
out gets no card, and the coverage audit will not add it back. It is off by default and
adds one model pass over the source. The run trace shows how far each unit was
condensed.

With the cheat sheet on, **High-yield only** goes further: it keeps only what the exam
asks you to *do*. Every point in the source is sorted into one of three tiers:

- **Core** is what an examiner would set a question on. Each core line is tagged with what
  the question asks of you: *calculate*, *apply* (carry out a method, rule or pattern: solve
  a type of problem, write or trace code, build a sentence), *explain*, *distinguish*,
  *define* or *recall*.
- **Supporting** is background a core line can't be answered without, such as a constant
  or a unit.
- **Left off** is everything else: the particulars of worked examples, scenarios and
  illustrations, statistics and figures that only illustrate a point, history,
  background, extra examples. An example keeps the method it teaches as a core line; its
  details are left off.

What counts as a fact worth recalling depends on the subject. In a language course the
vocabulary and forms are what gets tested, and in a programming course what a construct
does; a statistic that only shows how big something is is left off unless your source
stresses it. A sentence that lists several facts is judged one fact at a time.

Each section of your source is sorted on its own, and next to its own section almost
every slide has a point that looks core. So the sorting gets a second look: one call
rates every core line of the whole document against all the others, keeps the ones an
exam would ask, makes the next tier supporting (no card), and leaves the rest off. A
point repeated across slides keeps only its clearest statement. A section left with
nothing to card comes off entirely.

Each core line gets one card, shaped by its tag, so a calculation line gets a card that
has you set up or work the calculation rather than name the formula. Supporting lines get
no cards of their own, and there is no coverage back-fill: a high-yield deck is meant to
leave things out. Your **Exam / context** brief steers the sorting: "calculation-heavy
final, scenario questions" makes definitions and recall facts earn their place. Expect far
fewer cards than either the full source or the plain cheat sheet gives.

Nothing left off is thrown away. With **Review the plan before writing** on, the plan
review lists every unit's lines and left-off points, each with its tier. Give a
supporting or left-off line a card tag to bring it in, or choose **Leave off** to drop
one. On Auto the writer simply gets the new lines; with a deck size set, the unit's task
gains or loses a card for each change, unless you changed that task's count yourself. The cheat sheet page shows each line's tag, and lists what was left
off under its section (left-off points are not printed).

Once the plan exists, **Cheat sheet** (on the plan review, the finished run, and the
deck editor) opens the sheet as a page with its diagrams in place and its formulas
typeset. Read it to see exactly what your cards were written from, or choose **Print or
save as PDF** to keep a copy; the PDF has the typeset formulas too.

Enable **Review the plan before writing** to pause after planning. If extracted PDF
figures are available, **Read figures with vision** controls whether they are read. The
planner then decides which figures get image-backed cards and how many. An image that is
only text (a table of formulas, say) is transcribed and treated as text; exercises and
decoration get nothing. Select **Plan & generate** to begin.

## 3. Review the plan and follow progress

When plan review is enabled, inspect the document map and proposed tasks. Skip tasks,
change strategies, set each task's target (1–60 cards), or edit its worker notes.
Figure tasks are listed with their image under the unit they belong to, and can be
skipped or resized like any other; the figures the planner gave no cards are listed
below the tasks with its reason. On a high-yield deck, the **High-yield sheet** panel
lists each unit's lines and left-off points so you can re-sort them (see above). Keep at least one task, then choose **Run this plan**. **Re-plan** asks the planner
to produce another plan and can incur additional model calls.

The run trace shows phase and task status, token usage, and recorded cost. You can
leave the page while the server continues working. Server restarts interrupt work;
there is no durable queue that resumes it automatically.

## 4. Edit and export

Search cards and filter by type, status, or strategy. Source quotes and critic notes
provide context for review. Edit fields and tags, then **Save** each card. **AI improve**
asks the model to tighten a card. Select cards for bulk tagging, deletion, restoration,
coaching, or regeneration. **Regenerate unit** rewrites the source units behind the
selection, so it can affect more than the selected cards.

| Editor status | Exported? | What to do |
|---|---|---|
| Reviewed (`ok`) | Yes | Check accuracy and save edits before exporting. This label does not prove a person reviewed it. |
| Needs review | No | Review and save the card; cloze cards need valid deletion syntax. |
| Deleted | No | Restore it explicitly if you want to keep it. Editing alone does not restore it. |

**Export deck** downloads an `.apkg` containing all `ok` cards in the deck, regardless
of the current search or filters. Figure media is included. Import the package into
Anki to study. Basic cards use front/back fields; cloze cards use Anki deletion syntax,
such as `{{c1::answer}}`. Anki can create several review cards from one cloze note.

Every card keeps one note ID for life, so exporting a deck again and importing it
updates the notes already in Anki instead of adding copies, and two different decks
never share a note. Decks exported by version 0.6.0 or earlier are the exception: their
note IDs could repeat from one deck to the next, so the next export gives those cards
new ones, and Anki adds them as new notes beside the old ones.

**Run insights** shows the document map, strategy distribution, and recorded phase
costs. The library lets you permanently delete a whole deck; that differs from
restorable card deletion in the editor.

## 5. Bring review history back

After exporting from AnkiGPT and studying in Anki, export the deck or collection from
Anki with scheduling information. Upload the `.apkg` or `.colpkg` through
**Import reviews**. Matching uses the stable note GUID assigned by AnkiGPT's export.
Unrelated notes will not match.

Cards with at least three reviews, and either at least two lapses or an again-rate of at
least 40%, are flagged as struggling. The **Coach** tab can rewrite or split these cards using the review stats
and source. Suggestions are marked **Needs review**; inspect and save them before
exporting. This is a manual file round trip, not continuous Anki synchronization.

## Account settings

Open **My profile** to change your display name, bio, or avatar color. Email and
password changes require your current password. Deck and card totals on this page
belong to your account. Sign out from the sidebar when finished.

### Your OpenRouter API key

AnkiGPT is free, and the AI that writes your cards runs on your own OpenRouter
account. Create a key at [openrouter.ai/keys](https://openrouter.ai/keys), add credit
there, then paste the key under **My profile → OpenRouter API key** and save. You need
to do this once, before your first deck. The key is stored encrypted and is never shown
again; the page only tells you which key is saved by its last four characters. Paste a
new key to replace it, or choose **Remove key**. OpenRouter charges you for what your
decks use, and the run trace shows the recorded cost of each run.

### AI model

**My profile → AI model** is the model picker. It lists the models you can use, grouped
by the company that makes them:

| Company | Model | Reasoning effort it takes |
|---|---|---|
| DeepSeek | DeepSeek V4.1 Flash (`deepseek/deepseek-v4.1-flash-1017`), the default | Off, Low, High, Max |
| OpenAI | GPT-6 Luna (`openai/gpt-6-luna`) | Off, Low, Medium, High, Extra high, Max |
| Anthropic | Claude Haiku 5.5 (`anthropic/claude-haiku-5.5`) | Low, Medium, High, Extra high, Max |

Pick one and choose **Save model**. From your next run it does everything: generating a
deck, regenerating a unit, **AI improve** and the coach. Every model runs through
OpenRouter on the one key you saved, and OpenRouter bills each at its own rate. The
brief page names the model a deck is about to be written by, above **Plan & generate**.

### Advanced: how hard each agent thinks

A deck is built by several AI agents, and **My profile → Advanced** has a slider for
each one. Move a slider to set that agent's reasoning effort. The stops are the levels
your model takes, listed in the table above and at the top of the panel, so they change
when you pick another model. More effort means more careful work, and a slower and
costlier run. A slider left on **Default** follows the app's own setting, which is
shown beside it.

| Agent | What it does |
|---|---|
| Outliner | Splits your source into study units. |
| Figure reader | Reads the figures found in a PDF. |
| Cheat sheet writer | Boils each unit down when **Make a cheat sheet first** is on. |
| Planner | Decides what to cover, figures included, and hands out the writing tasks. |
| Card writers | Write the cards for each task in the plan. |
| Cold reader | Answers each card without the source, to catch cards that give their answer away. |
| Judge | Rules keep, rewrite or drop on each card against the source. |
| Coverage auditor | Looks for testable facts that no card covers yet. |
| Back-fill reviewer | Decides whether each card written for a coverage gap earns its place in the deck. |
| Duplicate resolver | Picks the card to keep when several test the same fact. |
| Card improver | Rewrites a single card when you choose **AI improve**. |
| Coach | Diagnoses and rewrites the cards you keep getting wrong in Anki. |

The last seven are the review agents: each check a card goes through after it is written
has its own slider. **Set all** moves every slider at once, **Save effort** stores your
choices, and **Reset to defaults** clears them. Changes apply from your next run, and to
your account only. An effort you set under one model is kept when you switch. If the new
model has no such level, the agent uses the next level up that it has (its highest, if
there is none above), and the slider shows that level. So **Off** under GPT-6 Luna
becomes **Low** under Claude Haiku 5.5, which cannot have reasoning turned off, and
**Medium** becomes **High** under DeepSeek V4.1 Flash, which has no Medium.

## Troubleshooting

- **No extracted text:** use a PDF with selectable text or paste notes directly.
- **"Add your OpenRouter API key":** save a key under **My profile** first; see
  [Your OpenRouter API key](#your-openrouter-api-key).
- **Generation failed:** check the run trace. Confirm that your OpenRouter key is
  valid, has credit, and can use the configured model. Previous cards survive failures
  during mapping/planning; later failures can leave a partial new run.
- **Too few cards:** inspect Deleted and Needs review, check the plan's coverage, and
  confirm the source wasn't truncated. The requested deck size is approximate. If
  **Make a cheat sheet first** was on, turn it off to write from the full source.
- **Nothing exports:** only `ok` cards export. Review, save, or restore cards first.
- **Review import matches nothing:** use a deck originally exported by this app and
  include scheduling when exporting it back from Anki.
- **Compressed package errors:** install the supplied `zstandard` dependency; an
  older-compatible Anki export is another option.

See [setup and data handling](setup.md#deployment-and-data) for storage, credentials,
and what is sent to the model provider.
