# User guide

[README](../README.md) · [Setup](setup.md) · [Architecture](architecture.md)

## 1. Add your material

Create an account or sign in, then choose **New deck**. Give it a title and select
**Basic** (front/back), **Cloze** (fill in the blank), or **Mixed** cards.
Paste text or upload a PDF. For PDFs, optionally select a page range using 1-based
page numbers. Scanned documents need text extraction elsewhere first; the app has
no OCR workflow. Only PDF uploads are supported.

The default upload limit is 50 MB. Sources longer than 400,000 characters are
truncated with a warning; use a smaller page range if the omitted material matters.
Both limits are configurable. Continue to source review to check the extraction.

## 2. Brief the planner

The source preview displays the first 4,000 characters; generation uses the full
stored source, subject to the source cap above. Add exam/context, focus areas,
exclusions, and must-include terms. Leave deck size on **Auto** or set an approximate
target. Validation, deduplication, and coverage work can change the final count.

Enable **Review the plan before writing** to pause after planning. If extracted PDF
figures are available, **Read figures with vision** controls whether they are analyzed
for image-backed cards. Select **Plan & generate** to begin.

## 3. Review the plan and follow progress

When plan review is enabled, inspect the document map and proposed tasks. Skip tasks,
change strategies, set each task's target (1–60 cards), or edit its worker notes.
Keep at least one task, then choose **Run this plan**. **Re-plan** asks the planner
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

**Run insights** shows the document map, strategy distribution, and recorded phase
costs. The library lets you permanently delete a whole deck; that differs from
restorable card deletion in the editor.

## 5. Bring review history back

After exporting from AnkiGPT and studying in Anki, export the deck or collection from
Anki with scheduling information. Upload the `.apkg` or `.colpkg` through
**Import reviews**. Matching uses the stable note GUID assigned by AnkiGPT's export.
Unrelated notes will not match.

Cards with at least two lapses or an again-rate of at least 40% are flagged as
struggling. The **Coach** tab can rewrite or split these cards using the review stats
and source. Suggestions are marked **Needs review**; inspect and save them before
exporting. This is a manual file round trip, not continuous Anki synchronization.

## Account settings

Open **My profile** to change your display name, bio, or avatar color. Email and
password changes require your current password. Deck and card totals on this page
belong to your account. Sign out from the sidebar when finished.

## Troubleshooting

- **No extracted text:** use a PDF with selectable text or paste notes directly.
- **Generation failed:** check the run trace and server logs. Confirm the server's API
  key, available credit, and model access. Previous cards survive failures during
  mapping/planning; later failures can leave a partial new run.
- **Too few cards:** inspect Deleted and Needs review, check the plan's coverage, and
  confirm the source wasn't truncated. The requested deck size is approximate.
- **Nothing exports:** only `ok` cards export. Review, save, or restore cards first.
- **Review import matches nothing:** use a deck originally exported by this app and
  include scheduling when exporting it back from Anki.
- **Compressed package errors:** install the supplied `zstandard` dependency; an
  older-compatible Anki export is another option.

See [setup and data handling](setup.md#deployment-and-data) for storage, credentials,
and what is sent to the model provider.
