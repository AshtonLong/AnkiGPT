# AnkiGPT Desktop — Specification

Status: written 2026-10-05 and built the same day as version 0.1.0. Not yet released: the
installer has not been run on a clean Windows account, no release has been published, so
one version has not been watched updating to the next, and the manual checklist (section
10.3) has not been run with a real OpenRouter key. The code-signing decision in section 13
is still open. [docs/desktop.md](../docs/desktop.md) is the page for users and developers.

## 1. Summary

AnkiGPT Desktop is a Windows application that runs the existing AnkiGPT app on the
user's own computer, with no server, no account and no sign-in. It is an Electron shell
around the existing Flask app: Electron starts the Python backend as a hidden child
process and shows its pages in a window.

The web version keeps working exactly as it does today, from the same codebase. Everything
desktop-specific sits behind one switch, called **desktop mode** in this document.

### Decisions already made

| Decision | Choice |
|---|---|
| Shell | Electron |
| Backend | The existing Flask app, frozen with PyInstaller. No rewrite. |
| Users | One local user on desktop, no login. The web version keeps accounts. |
| Platform | Windows 10 and 11, x64 only |

### Goals

1. Install with one `.exe`, open from the Start menu, and generate a deck without creating
   an account or running any command.
2. Every workspace feature of the web app works: new deck from text or PDF, source review,
   plan review, live generation trace, editor, AI improve, review import, coach, export.
3. Decks, cards and the OpenRouter key stay on the user's computer.
4. The app updates itself.
5. The web app's behaviour and tests are unchanged.

### Out of scope for version 1

- macOS, Linux and Windows on ARM.
- Generating in the background after the window is closed (no tray icon).
- Sending cards straight into Anki through AnkiConnect.
- A model picker in the UI. Models are changed through a settings file (section 5.9).
- Moving a web account's decks into the desktop app, or syncing between the two.
- Several profiles on one Windows account.
- An in-app backup and restore screen.
- A portable (zip) build and the Microsoft Store.
- Telemetry and crash reporting. The app sends nothing about its use to anyone.

## 2. Architecture

```text
┌─ AnkiGPT.exe (Electron main process) ────────────────────────────────┐
│  owns: window, menus, dialogs, downloads, updates, the install secret  │
│                                                                        │
│   spawns, hidden                        loads http://127.0.0.1:<port>  │
│        │                                              │                │
│        ▼                                              ▼                │
│  ankigpt-backend.exe                     BrowserWindow (sandboxed)   │
│  Flask app on waitress        ◀── HTTP ──  the same HTML, CSS, htmx    │
│  generation threads                        and JS as the web app       │
│        │                                                               │
└────────┼───────────────────────────────────────────────────────────────┘
         ▼
   %APPDATA%\AnkiGPT\data\ankigpt.db        OpenRouter (HTTPS, user's key)
```

There are two programs:

- **The shell** (Electron, JavaScript) has no application logic. It starts the backend,
  shows the window, and handles the things only a desktop program can do.
- **The backend** (Python) is the existing `app/` package with desktop mode switched on,
  served by waitress instead of gunicorn (gunicorn does not run on Windows).

The page in the window gets no Node.js access, and there is no preload script and no IPC
channel. It is an ordinary web page talking to a local server. That keeps the web and
desktop UIs identical and keeps the security model simple.

### 2.1 Startup sequence

1. Electron takes the single-instance lock. If another copy is running, it focuses that
   copy's window and exits.
2. Electron shows a small splash window (a static local HTML file with the brand mark and
   "Starting AnkiGPT…").
3. Electron loads the install secret (section 4.2) and generates a random launch token
   (section 6).
4. Electron spawns the backend with `windowsHide: true` and these environment variables:

   | Variable | Value |
   |---|---|
   | `ANKIGPT_DATA_DIR` | Absolute path of the data folder |
   | `ANKIGPT_LOG_DIR` | Absolute path of the logs folder |
   | `ANKIGPT_TOKEN` | The launch token |
   | `SECRET_KEY` | The install secret |
   | `ANKIGPT_VERSION` | The app version from `package.json` |

5. The backend prepares the data folder, backs up the database if the version changed
   (section 4.3), creates missing tables and columns (the existing `_ensure_schema`),
   creates the local user if needed, and clears interrupted runs (section 5.6).
6. The backend binds waitress to `127.0.0.1` on a port chosen by Windows (port `0`) and
   prints one line to stdout: `{"event":"ready","port":53817}`.
7. Electron loads `http://127.0.0.1:<port>/decks` in the main window. When the page is
   ready to show, the main window appears and the splash closes.

If the backend exits before it is ready, or is not ready within 30 seconds, Electron shows
the start-up failure dialog (section 7.8).

### 2.2 Backend-to-shell messages

The backend's stdout carries only these JSON lines, one per line, flushed immediately.
Logging goes to a file, never to stdout.

| Message | Sent when |
|---|---|
| `{"event":"ready","port":N}` | The server is listening |
| `{"event":"activity","active_runs":N}` | A generation thread starts or ends |

stderr is captured by Electron into `main.log`, so a crash before logging is set up still
leaves a trace.

### 2.3 Shutdown

1. On quit, Electron closes the backend's stdin.
2. A watcher thread in the backend sees end-of-file on stdin, stops waitress and exits.
3. If the backend is still alive after 3 seconds, Electron kills its whole process tree
   (`taskkill /PID <pid> /T /F`). The tree matters because the PDF layout library can
   start worker processes.

Because the backend exits when stdin closes, it also exits if Electron crashes. A stray
backend holding the database open is the failure this prevents.

SQLite runs in WAL mode already (`app/__init__.py:24`), so a hard kill does not corrupt
the database.

## 3. Repository layout

```text
desktop-app/
  SPEC.md                    this document
  package.json               Electron app, scripts, electron-builder config
  package-lock.json          exact npm versions (section 8.4)
  src/
    main.js                  lifecycle: lock, splash, spawn, window, quit
    backend.js               spawn, read stdout messages, shut down, kill tree
    window.js                window options, navigation rules, downloads, context menu
    menu.js                  application menu
    updater.js               electron-updater wiring
    secret.js                install secret via safeStorage
    splash.html
  backend/
    entry.py                 the frozen program's entry point
    ankigpt-backend.spec   PyInstaller build description
    requirements.txt         waitress, pyinstaller (on top of the root requirements.txt)
    constraints.txt          exact Python versions the backend is built with (section 8.4)
    selfcheck/sample.pdf     tiny PDF used by the self-check
  resources/
    icon.ico                 generated from app/static/brand.svg
  scripts/
    build-backend.ps1        clean venv -> pip install -> pyinstaller -> self-check -> smoke test
    smoke-backend.js         starts the frozen backend for real (the second script in section 10.2)
    before-pack.js           stops the installer build if the frozen backend is missing
    make-assets.py           regenerates icon.ico and selfcheck/sample.pdf
app/
  desktop.py                 everything desktop mode adds to the Flask app
  config.py                  gains DesktopConfig
```

The root `.gitignore` already ignores `build/`, `dist/`, `lib/` and `*.spec`, which would
hide electron-builder's default resources folder and the PyInstaller spec file. So this
layout uses `resources/` and `out/` instead of `build/` and `dist/`, and `.gitignore`
gains `!desktop-app/backend/*.spec`, `desktop-app/out/`, `desktop-app/backend-dist/` and
`desktop-app/node_modules/`.

## 4. Data on the user's computer

### 4.1 Layout

Everything lives under Electron's `userData` folder, `%APPDATA%\AnkiGPT`. That folder is
not synced by OneDrive, which matters because SQLite's WAL files must not be synced.

```text
%APPDATA%\AnkiGPT\
  data\
    ankigpt.db             the one database (plus -wal and -shm files)
    uploads\                 PDFs while they are being read; deleted after extraction
    backups\                 automatic copies made before an upgrade
    version.txt              the app version that last opened this database
    settings.env             optional, hand-edited (section 5.9)
  logs\
    main.log                 Electron
    backend.log              Flask, rotating, 5 files of 2 MB
  secret.bin                 the install secret, encrypted by Windows
  window-state.json          window size, position, maximised, zoom
  (Chromium's own profile files)
```

Flask's instance folder becomes `data\`. Today it sits beside the code
(`app/__init__.py:96`), which is read-only once installed. `create_app` gains a way to be
given the instance path, and the web app keeps its current default.

The database schema is the same as the web app's.

### 4.2 The install secret

`SECRET_KEY` signs the session cookie and derives the key that encrypts the saved
OpenRouter key (`app/services/credentials.py:19`). Today it defaults to `"dev-secret"`
(`app/config.py:25`), which must never ship.

- On first launch Electron generates 48 random bytes, encrypts them with Electron's
  `safeStorage` (Windows DPAPI, tied to the Windows user account) and writes `secret.bin`.
- On every launch Electron decrypts it and passes it to the backend as `SECRET_KEY`.
- If `secret.bin` is missing or cannot be decrypted, Electron generates a new one. The
  saved OpenRouter key then becomes unreadable, and the Settings page already handles that
  case by asking for the key again (`app/templates/profile.html:71`). Decks are unaffected.

Consequence: copying `data\` to another computer brings the decks but not the OpenRouter
key. The user pastes the key again there.

### 4.3 Backups and upgrades

- On start, if `version.txt` names a different version than the running app and the
  database exists, the backend copies it to `backups\ankigpt-<old version>-<date>.db`
  using SQLite's backup API, then writes the new version. The three newest backups are kept.
- Schema changes stay additive, as they are today (`_ensure_schema`).
- Opening a database with an older app version than the one that last wrote it is not
  supported.
- Uninstalling leaves `%APPDATA%\AnkiGPT` in place, so reinstalling brings the decks back.

## 5. Backend: desktop mode

Desktop mode is a config flag, `DESKTOP_MODE`, set only by `DesktopConfig`. Code checks the
flag; nothing checks for Electron or for Windows.

### 5.1 Configuration

`DesktopConfig` subclasses `Config` and fixes these values:

| Setting | Desktop value | Why |
|---|---|---|
| `DESKTOP_MODE` | `True` | |
| `SECRET_KEY` | from the environment, required | Section 4.2. The backend refuses to start without it. |
| `SQLALCHEMY_DATABASE_URI` | `sqlite:///<data>/ankigpt.db` | |
| `UPLOAD_FOLDER` | `<data>/uploads` | |
| `OPENROUTER_API_KEY` | `""` | No shared server key. The user's own key is the only key. |
| `OPENROUTER_SITE_URL` | the project's GitHub URL | Sent to OpenRouter as the referring app |
| `UPLOAD_MAX_MB` | `200` | The 50 MB web limit guards a shared server's disk. `MAX_SOURCE_CHARS` still bounds cost. |
| `PROXY_FIX_HOPS` | `0` | No proxy |
| `SESSION_COOKIE_SECURE` | `False` | Plain HTTP on loopback |
| `MAIL_*` | empty | No email on desktop |
| `GENERATION_IN_THREAD` | `True` | |

The desktop backend never reads a `.env` file from the working directory.

### 5.2 The local user

- At start, the backend looks for the user with email `local@ankigpt.invalid` and
  creates it if missing, with a random password hash that no password matches.
- Flask-Login gets a `request_loader` that returns this user for every request, so
  `current_user` is always signed in. No route's ownership check changes: decks belong to
  the local user the same way they belong to an account on the web.

### 5.3 Request guard

Before any other handling, including static files, every request must pass two checks or
it gets a 403:

1. The `X-AnkiGPT-Token` header equals the launch token (compared in constant time).
2. The `Host` header equals `127.0.0.1:<port>`.

Section 6 explains what these protect against. CSRF protection stays on.

### 5.4 What the user sees differently

| Area | Web | Desktop |
|---|---|---|
| `/` | Marketing page | Redirects to `/decks` |
| Sign up, sign in, forgot password, reset, sign out | Available | Routes redirect to `/decks`; their links and buttons are not rendered |
| Sidebar and top-bar "My profile" | Profile page | Named **Settings** |
| Sidebar account chip and "Sign out" | Shown | Replaced by the existing "Local workspace" note (`base.html:64`) |
| Terms and Privacy pages, site footer | Shown | 404, footer not rendered |
| "This server has a shared key" notice | Possible | Never shown |
| Export result text | "Check your downloads…" | "Choose where to save your package…" |

Several messages name "My profile" as the place to add a key: `base.html:41,49,81`,
`deck_preview.html:103`, `partials/auth_control.html`, `routes/main.py:239,540`,
`services/llm.py:37` and `services/pipeline/orchestrator.py:72`. They take the name from
one helper so desktop says "Settings" and web still says "My profile".

### 5.5 The Settings page

Same URL as the profile page (`/auth/profile`), rendered from its own template in desktop
mode. It has three panels:

1. **OpenRouter API key.** The existing panel and behaviour, unchanged: save, replace,
   remove, last four characters shown, never displayed again. The link to
   `openrouter.ai/keys` opens in the user's browser.
2. **Your data.** Plain statements: decks are stored on this computer at the shown path;
   source material and cards are sent to OpenRouter under the user's key when generating,
   improving or coaching; nothing is sent to an AnkiGPT server; the app contacts GitHub
   to check for updates.
3. **About.** App version.

Display name, bio, avatar colour, email and password are not shown on desktop.

### 5.6 Interrupted runs

Generation runs on a daemon thread (`app/tasks.py:34`) and a deck is only marked failed
when the pipeline raises (`orchestrator.py:52`). If the process dies mid-run, the deck
stays in `processing` forever. A server rarely restarts; a desktop app is closed daily.

At start, in desktop mode only:

- Every deck with status `processing` becomes `failed`, with the error "AnkiGPT was
  closed while this deck was generating. Retry to run it again."
- Every `PipelineTask` with status `running` or `queued` becomes `failed`.
- Decks in `planned`, `ready`, `draft` and `failed` are untouched.

This is safe because the single-instance lock guarantees no other process is generating.
It is desktop-only because the web app runs two gunicorn workers and one cannot tell
whether the other has a live run.

The failed-run page already offers **Retry generation** (`partials/status_panel.html:136`).
A retry starts the run again. Worker, critic, cheat-sheet and vision results already
produced are reused from the cache, while mapping and planning are paid for again.

### 5.7 Activity reporting

`dispatch_generation` counts live generation threads and emits the `activity` message
(section 2.2) whenever the count changes. Electron uses it for the quit warning (section
7.6) and to keep the computer awake (section 7.7).

### 5.8 Changes that also apply to the web app

- **Bundled assets.** `base.html:15,19` loads Figtree and Fragment Mono from Google Fonts
  and htmx 1.9.12 from jsDelivr. Both move into `app/static/` (fonts as `.woff2` with
  `@font-face` rules, htmx as a file, each with its licence). Without this the desktop app
  renders unstyled text and loses its interactivity when offline. One code path serves
  both versions.
- **Offline error.** A connection failure during generation currently surfaces as
  `OpenRouter request failed: <raw exception text>` (`services/llm.py:196`).
  `format_generation_error` gains a case: "Couldn't reach OpenRouter. Check your internet
  connection, then retry."

### 5.9 Advanced settings file

The model and pipeline settings are environment variables today, and a desktop user has
no environment to set. Until there is a settings UI, the backend reads
`data\settings.env` if it exists and applies only these keys: `OPENROUTER_MODEL`,
`OPENROUTER_MODEL_*`, `OPENROUTER_REASONING_*`, `OPENROUTER_EMBEDDING_MODEL`,
`OPENROUTER_TEMPERATURE`, `OPENROUTER_TIMEOUT_SECONDS`, `OPENROUTER_MAX_TOKENS`,
`PIPELINE_*` and `MAX_SOURCE_CHARS`. Any other key is ignored and logged.

This is also the escape hatch if the built-in default model (`openai/gpt-6-luna`) is
retired by the provider before an update ships.

### 5.10 The entry point

`desktop-app/backend/entry.py` does these things in this order:

1. Calls `multiprocessing.freeze_support()`. The PDF layout library has a multiprocessing
   path, and in a frozen program a child process re-runs the entry point; without this
   call each child would try to start another server.
2. Handles `--self-check` (section 10.2) and exits.
3. Reads the environment variables from section 2.1 and exits with a clear message on
   stderr if one is missing.
4. Applies `settings.env`, sets up file logging, builds the app with `DesktopConfig`.
5. Starts the stdin watcher, binds waitress (8 threads) and prints the `ready` message.

Run unfrozen with `--dev`, it skips the token check so the backend can be opened in a
normal browser during development. The flag is rejected in a frozen build.

## 6. Security model

A local web server can be reached by anything else on the same computer, and desktop mode
has no login. Two things must not be able to use it:

- **A web page in the user's browser.** A malicious site could send requests to
  `127.0.0.1`, or use DNS rebinding to read the responses.
- **Another program on the computer** probing local ports.

Defences:

| Defence | Stops |
|---|---|
| Bind to `127.0.0.1` only | Other machines on the network |
| Launch token: 32 random bytes made by Electron each launch, sent to the backend in its environment and added by Electron to every request from the app's own session (`session.webRequest.onBeforeSendHeaders`) | Browsers and other programs, which do not know the token |
| `Host` header check | DNS rebinding |
| Random port each launch | Casual probing |
| Existing CSRF tokens | Kept as a second layer |

A program running as the same Windows user can read another process's environment and can
call DPAPI, so it could obtain the token and the install secret. That is outside what a
desktop app can defend against, and the same is true of any locally stored credential.

The window:

- `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`, no preload. Card
  content written by a model is displayed in this window, and it has exactly the powers of
  a web page.
- All permission requests (camera, microphone, notifications, location) are denied.
- Navigation and new windows are restricted (section 7.2).
- DevTools are off in packaged builds unless `ANKIGPT_DEVTOOLS=1` is set.

The OpenRouter key is never written to a log.

## 7. The Electron shell

### 7.1 Window

- Default 1280 × 860, minimum 960 × 640, background `#FAFBFC` so there is no white flash.
- Size, position, maximised state and zoom are saved to `window-state.json` and restored.
  A saved position that is off every current monitor is discarded.
- Standard Windows title bar and frame.
- The port changes each launch, so the page's origin changes too. Nothing may rely on
  origin-scoped browser storage (`localStorage`, IndexedDB). The app uses none today;
  lasting state belongs in the database. Electron clears the HTTP cache at start, which
  also guarantees fresh static files after an update.

### 7.2 Navigation rules

| The page tries to | Result |
|---|---|
| Navigate within `http://127.0.0.1:<port>` | Allowed |
| Navigate to an `https://` address | Blocked; opened in the user's default browser |
| Open a new window on the app's own origin (figure images, `partials/card_row.html:15`) | Opens in a second app window with the same restrictions, so the token header is still added |
| Open a new window on an `https://` address (`openrouter.ai/keys`) | Opened in the default browser |
| Anything else | Blocked |

When a page blocks unloading with `beforeunload`, as `static/profile.js:54` does, Electron
shows no prompt: the window simply refuses to close. The shell handles
`will-prevent-unload` with a "Leave without saving?" dialog, so no page can make the
window impossible to close.

### 7.3 Export and other downloads

The export button fetches the `.apkg` and saves it through a download link
(`static/experience.js:126`). In Electron that raises a download event:

1. A native Save dialog opens in the user's Downloads folder with the deck's file name.
2. When the file is written, a dialog offers **Open in Anki**, **Show in folder** and
   **Done**. "Open in Anki" opens the file with its registered program, which makes Anki
   import it. If no program is registered for `.apkg`, the shell falls back to showing the
   file in its folder.

Uploads (PDFs, and `.apkg`/`.colpkg` files for review import) use the page's normal file
inputs and drag-and-drop. Nothing changes.

### 7.4 Menus

| Menu | Items |
|---|---|
| File | New deck (Ctrl+N) · Your decks · Settings (Ctrl+,) · Open data folder · Quit (Ctrl+Q) |
| Edit | Undo · Redo · Cut · Copy · Paste · Select all |
| View | Reload (Ctrl+R) · Zoom in · Zoom out · Actual size · Full screen (F11) |
| Help | User guide · Check for updates… · Open logs folder · About AnkiGPT |

Electron has no right-click menu by default. The shell adds one: Cut, Copy and Paste in
text fields, Copy on selected text, and spelling suggestions. The card editor needs this.

### 7.5 Single instance

Only one copy runs per Windows user. Starting a second focuses the first. Two backends
would share one database, and the second would mark the first's live runs as failed.

### 7.6 Closing while a deck is generating

If `active_runs` is above zero when the user closes the window or quits, a dialog says a
deck is still being generated and that quitting stops it. The buttons are **Keep
generating** (default) and **Quit anyway**. Otherwise closing the window quits the app.

AI improve, bulk regenerate and coach run inside a single request and are not counted.

### 7.7 Sleep

While `active_runs` is above zero the shell holds a `powerSaveBlocker` of type
`prevent-app-suspension`, so Windows does not sleep in the middle of a run. The screen may
still turn off.

### 7.8 Failures

| Situation | What the user sees |
|---|---|
| Backend not ready in 30 s, or exits before ready | "AnkiGPT couldn't start" with **Open logs** and **Quit** |
| Backend exits while the app is open | "AnkiGPT stopped working" with **Restart** and **Quit** |
| No internet | The app opens; browsing, editing and export work; generation fails with the message from section 5.8 |
| Update check or download fails | Nothing. It is logged and retried at the next check. |

## 8. Packaging

### 8.1 The backend (PyInstaller)

- **One-folder build**, not one-file. A one-file build unpacks itself to a temp folder on
  every start, which is slow and is the pattern antivirus tools flag most often.
- **Console subsystem**, spawned hidden. A windowed build has no stdout, and stdout carries
  the messages in section 2.2.
- **No UPX compression**, which also triggers antivirus false positives.
- **Built from a clean virtual environment** holding only `requirements.txt` plus
  `desktop-app/backend/requirements.txt`. The development venv contains packages that are
  not in the requirements file and must not ship (Playwright at 107 MB, `stripe`,
  `psycopg2`).
- **Python 3.12**, the version the Docker image and the development venv use.
- **Bundled data:** `app/templates`, `app/static`, and the data files of `pymupdf`,
  `pymupdf.layout` and `pymupdf4llm`. The layout library needs its ONNX models and YAML
  files (`pymupdf/layout/resources/onnx/`, 49 MB) and `onnxruntime`'s native libraries.

The PDF stack is the main packaging risk, for a specific reason: `app/services/pdf.py:116`
imports the layout library inside `try` blocks and falls back to plain text extraction
when anything fails. A bundle that is missing a model file would not crash. It would
quietly produce worse text from every PDF. The self-check in section 10.2 exists to catch
that.

### 8.2 The installer (electron-builder)

- NSIS installer, x64, one click, per user. It installs to
  `%LOCALAPPDATA%\Programs\AnkiGPT` without an administrator prompt, which also lets
  updates install without one.
- The frozen backend folder ships as an extra resource at `resources\backend\`.
- Start-menu shortcut; desktop shortcut.
- App data is kept on uninstall.
- Output: `AnkiGPT-Setup-<version>.exe`, `latest.yml` and a `.blockmap` file.

### 8.3 Size

Measured in the development venv: `pymupdf` 105 MB (including the 49 MB of layout models),
`onnxruntime` 45 MB, `numpy` 55 MB. Measured at the first build (0.1.0): the frozen backend
is 203 MB on disk, the installed app 574 MB including it, and the installer 198 MB.
Target: installer under 250 MB.

### 8.4 Tool versions

Current at the time of writing; exact versions are pinned in lockfiles when the build
starts. Electron 44.5.1 (needs Node 22.12 or newer; this machine has 24.14),
electron-builder 26.15.3, electron-updater 6.8.9, PyInstaller 6.22.3, waitress 3.0.2.

## 9. Updates and releases

### 9.1 Versioning

One version number, in `desktop-app/package.json`, following semantic versioning. It is
passed to the backend and shown in Settings and in About. A release is the git tag
`v<version>`.

### 9.2 Auto-update

- `electron-updater` reads GitHub Releases of `AshtonLong/AnkiGPT`. The repository is
  public, so no token is needed.
- The app checks 10 seconds after start and every 6 hours, and downloads in the background.
- When an update is downloaded, a dialog offers **Restart now** and **Later**. The dialog
  waits while `active_runs` is above zero. "Later" installs the update when the app next
  quits.
- **Help → Check for updates…** checks immediately and always reports the result.
- Updates are off in development builds.

### 9.3 Code signing

Unsigned builds work, including auto-update, with two costs: Windows SmartScreen shows
"Windows protected your PC" on first install, and antivirus tools are more likely to flag
the PyInstaller backend. With an unsigned app, the integrity of an update rests on HTTPS
and the checksum in `latest.yml`, which means on the security of the GitHub account.

This is an open decision (section 13).

### 9.4 Release pipeline

The repository has no CI today. Releases add `.github/workflows/desktop-release.yml`,
triggered by a `v*` tag on a Windows runner:

1. Run the existing test suite.
2. Build the backend in a clean venv and run its self-check.
3. Build the installer and upload it to a **draft** GitHub Release.
4. A person runs the manual checklist (section 10.3) against the draft, then publishes it.
   Publishing is what makes installed copies update.

The same scripts run locally, so a release can be built on a Windows machine without CI.

## 10. Testing

### 10.1 Automated, in the existing pytest suite

A new `tests/test_desktop.py` covers:

- Every workspace route works with no sign-in.
- `/`, the sign-in routes and the legal pages redirect or return 404.
- A request without the token, with a wrong token, or with a wrong `Host` gets 403,
  including for a static file.
- The local user is created once, and starting twice does not create a second.
- Interrupted-run cleanup: a `processing` deck becomes `failed` with the message, its
  running and queued tasks become `failed`, and `planned` and `ready` decks are untouched.
- The database and uploads land in the given data folder.
- `settings.env` applies listed keys and ignores the rest.
- Version-change backup creates a copy and keeps three.

The existing tests must pass unmodified. They are the proof the web app did not change.

### 10.2 The frozen backend

`ankigpt-backend.exe --self-check` runs inside the frozen program, prints a JSON report
and exits non-zero on any failure:

- `pymupdf.layout`, `pymupdf4llm` and `onnxruntime` import.
- `extract_pdf_text` on the bundled sample PDF returns text **through the layout path**,
  not the plain-text fallback.
- Figure extraction returns an image from the sample PDF.
- An in-memory deck exports to a valid `.apkg`.
- An encrypt-and-decrypt round trip of a key succeeds.
- Templates and static files are found.

A second script starts the frozen backend for real and checks that it prints `ready`,
serves `/decks` with the token, refuses without it, and exits within 3 seconds of stdin
closing.

### 10.3 Manual release checklist

On a Windows account that has never had AnkiGPT installed:

1. Install. Note any SmartScreen or antivirus prompt.
2. First launch shows the decks page with no sign-in.
3. Add an OpenRouter key. This needs a real key with credit.
4. Generate a deck from a PDF with figures, with plan review on.
5. Edit a card, AI-improve a card, export, and use **Open in Anki**.
6. Import review history and run Coach.
7. Quit during a generation: the warning appears. Quit anyway, relaunch: the deck shows as
   failed with the closed-app message, and Retry works.
8. Disconnect from the internet and launch: pages render with the right fonts and the
   editor works.
9. Launch a second copy: the first window is focused.
10. Update from the previous release: decks and the key survive, and a backup exists.
11. Uninstall and reinstall: decks are still there.

There is no automated end-to-end test of the Electron shell in version 1.

## 11. Effect on the web app

- All desktop behaviour is behind `DESKTOP_MODE`, which only `DesktopConfig` sets.
- Three changes reach the web app, all described in sections 4.1 and 5.8: `create_app` can
  be given an instance path, fonts and htmx are served from `app/static/`, and connection
  failures get a friendlier message.
- Docker, gunicorn, the deploy scripts and the multi-user account system are untouched.
- When the desktop app ships, `docs/` gains a desktop page and the README links to it.

## 12. Build order

Each step ends in something that can be checked.

1. **Packaging spike.** Freeze the backend as it is today and get `--self-check` passing.
   This is the largest unknown, so it comes first; if the PDF stack cannot be frozen
   cleanly, the plan changes before anything else is built.
2. **Desktop mode in Flask.** Config, local user, request guard, Settings page, UI
   differences, interrupted-run cleanup, bundled assets, with tests. Checked in a browser
   using `--dev`.
3. **Electron shell in development.** Spawn the unfrozen backend, window, navigation
   rules, downloads, menus, quit warning. Uses a separate `AnkiGPT Dev` data folder so
   real data is never touched.
4. **Installer.** Frozen backend inside an NSIS installer, tested on a clean account.
5. **Updates and release workflow.** Publish two versions and watch one update to the next.
6. **Release checklist and signing decision**, then the first public release.

## 13. Open decisions and assumptions

Decision needed from Ashton:

- **Code signing.** Ship unsigned at first, or pay for signing before other people
  install it? Recommendation: build and test unsigned, and sign before the first public
  release, because SmartScreen and antivirus warnings are the first thing a new user sees.
  Cost and eligibility of the signing options need checking at that point.

Assumptions made here that are cheap to change before building:

- Releases are published as GitHub Releases on `AshtonLong/AnkiGPT`. That address is
  baked into every installed copy, so moving it after the first public release is costly.
- The desktop Settings page drops display name, bio and avatar colour.
- The upload limit rises to 200 MB on desktop.
- Model selection stays in `settings.env` for version 1, with no UI.
- Closing the window quits the app; there is no tray icon.
- A dialog with **Open in Anki** follows every export.
