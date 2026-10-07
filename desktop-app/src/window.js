/*
 * Windows and everything that restricts them (SPEC.md sections 6 and 7.1 to 7.4):
 * window options and saved state, navigation rules, permissions, the launch-token
 * header, downloads, pages saved as PDF, and the right-click menu.
 *
 * The page in the window is an ordinary web page talking to the local backend. It gets
 * no Node.js access, no preload script and no IPC channel. Card content written by a
 * model is shown here, and it has exactly the powers of a web page.
 */
'use strict';

const { execFile } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { app, BrowserWindow, dialog, Menu, screen, shell } = require('electron');

const DEFAULT_SIZE = { width: 1280, height: 860 };
const MINIMUM_SIZE = { width: 960, height: 640 };
const BACKGROUND = '#FAFBFC'; // the page background, so there is no white flash
const TOKEN_HEADER = 'X-AnkiGPT-Token';
// A page asks for a PDF of itself by navigating here (static/experience.js). The
// navigation is blocked like any other that leaves the app, so the page stays put.
const SAVE_PDF_URL = 'ankigpt:save-pdf';
const LETTER_COUNTRIES = ['US', 'CA', 'MX']; // everywhere else prints on A4

/** DevTools are off in packaged builds unless ANKIGPT_DEVTOOLS=1 is set. */
const devToolsAllowed = !app.isPackaged || process.env.ANKIGPT_DEVTOOLS === '1';

const webPreferences = {
  contextIsolation: true,
  sandbox: true,
  nodeIntegration: false,
  devTools: devToolsAllowed,
};

// Set by `protectSession` once the backend's port is known. Until then nothing is the app.
let appOrigin = null;

/** 'app' for the backend's own origin, 'external' for https, 'blocked' for anything else. */
function classify(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return 'blocked';
  }
  if (appOrigin && parsed.origin === appOrigin) return 'app';
  return parsed.protocol === 'https:' ? 'external' : 'blocked';
}

// ------------------------------------------------------------------ session
/**
 * Everything that applies to the app's whole session rather than to one window.
 * `origin` is http://127.0.0.1:<port> and `token` is this launch's token.
 */
function protectSession(session, origin, token, log) {
  appOrigin = origin;

  // The backend refuses any request without the token. Only requests to the backend
  // get it, so a card that points an image at another site can never leak it.
  session.webRequest.onBeforeSendHeaders((details, callback) => {
    if (classify(details.url) === 'app') details.requestHeaders[TOKEN_HEADER] = token;
    callback({ requestHeaders: details.requestHeaders });
  });

  // Camera, microphone, notifications, location and the rest: the app needs none of them.
  session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);

  session.on('will-download', (_event, item, contents) => handleDownload(item, contents, log));
}

// ------------------------------------------------------------------ downloads
function handleDownload(item, contents, log) {
  const name = item.getFilename();
  const extension = path.extname(name).slice(1).toLowerCase();
  const isDeck = extension === 'apkg' || extension === 'colpkg';
  // With no save path set, Electron shows the native Save dialog with these options.
  item.setSaveDialogOptions({
    title: isDeck ? 'Save your deck' : 'Save file',
    defaultPath: path.join(app.getPath('downloads'), name),
    filters: [
      ...(isDeck ? [{ name: 'Anki deck package', extensions: [extension] }] : []),
      { name: 'All files', extensions: ['*'] },
    ],
  });
  item.once('done', (_done, state) => {
    if (state === 'completed') {
      offerToOpen(BrowserWindow.fromWebContents(contents), item.getSavePath(), isDeck);
    } else if (state === 'interrupted') {
      log.warn(`Download of ${name} was interrupted`);
    }
  });
}

async function offerToOpen(window, file, isDeck) {
  const options = {
    type: 'info',
    title: 'AnkiGPT',
    message: isDeck ? 'Your deck is saved.' : 'Your file is saved.',
    detail: file,
    buttons: [isDeck ? 'Open in Anki' : 'Open', 'Show in folder', 'Done'],
    defaultId: 0,
    cancelId: 2,
    noLink: true,
  };
  const live = window && !window.isDestroyed();
  const { response } = await (live ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options));
  if (response === 1) {
    shell.showItemInFolder(file);
  } else if (response === 0) {
    // Opening an .apkg with its registered program makes Anki import it. With no program
    // registered, Windows would ask "How do you want to open this file?", so show the
    // file in its folder instead.
    const failed = !(await hasRegisteredProgram(path.extname(file))) || (await shell.openPath(file));
    if (failed) shell.showItemInFolder(file);
  }
}

function hasRegisteredProgram(extension) {
  const keys = [
    `HKCR\\${extension}`,
    `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\${extension}\\UserChoice`,
  ];
  const exists = (key) => new Promise((resolve) => {
    execFile('reg', ['query', key], { windowsHide: true }, (error) => resolve(!error));
  });
  return Promise.all(keys.map(exists)).then((found) => found.some(Boolean));
}

// ------------------------------------------------------------------ pages as PDF
const savingPdf = new WeakSet(); // web contents with a PDF on the way

/** "Cheat sheet · Cell biology · AnkiGPT" becomes "Cheat sheet - Cell biology.pdf". */
function pdfFileName(title) {
  const name = title
    .replace(/\s*·\s*AnkiGPT\s*$/, '')
    .replace(/\s*·\s*/g, ' - ')
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .slice(0, 120)
    .replace(/[. ]+$/, '');
  return `${name || 'AnkiGPT'}.pdf`;
}

/**
 * Write the page in `contents` to a PDF the user picks a place for. The page is laid
 * out with its print styles, the way a browser's "Save as PDF" would. Electron has no
 * print preview, and on some computers its print dialog fails without ever opening
 * ("Invalid printer settings"), so `window.print()` cannot do this job here.
 */
async function savePageAsPdf(contents, log) {
  if (savingPdf.has(contents)) return;
  savingPdf.add(contents);
  const window = BrowserWindow.fromWebContents(contents);
  const live = () => window && !window.isDestroyed();
  try {
    // Backgrounds stay off, as in a browser's print dialog: the page's print styles name
    // the shading that belongs on paper, and the window's own colour stays off it.
    const data = await contents.printToPDF({
      pageSize: LETTER_COUNTRIES.includes(app.getLocaleCountryCode()) ? 'Letter' : 'A4',
    });
    const options = {
      title: 'Save as PDF',
      defaultPath: path.join(app.getPath('downloads'), pdfFileName(contents.getTitle())),
      filters: [{ name: 'PDF document', extensions: ['pdf'] }],
    };
    const { canceled, filePath } = await (live() ? dialog.showSaveDialog(window, options) : dialog.showSaveDialog(options));
    if (canceled || !filePath) return;
    await fs.promises.writeFile(filePath, data);
    offerToOpen(window, filePath, false);
  } catch (error) {
    log.warn(`Could not save the page as a PDF: ${error.message}`);
    const options = {
      type: 'error',
      title: 'AnkiGPT',
      message: "The PDF couldn't be saved",
      detail: 'If a PDF with that name is open in another program, close it and try again.',
      buttons: ['OK'],
      noLink: true,
    };
    if (live()) dialog.showMessageBox(window, options);
    else dialog.showMessageBox(options);
  } finally {
    savingPdf.delete(contents);
  }
}

// ------------------------------------------------------------------ every window
/** Navigation rules, the unsaved-changes prompt and the right-click menu, for any web contents. */
function restrict(contents, log) {
  // Fires for navigation in any frame, started by the user or by the page.
  contents.on('will-frame-navigate', (event) => {
    const kind = classify(event.url);
    if (kind === 'app') return;
    event.preventDefault();
    if (!event.isMainFrame) return;
    if (event.url === SAVE_PDF_URL) savePageAsPdf(contents, log);
    else if (kind === 'external') shell.openExternal(event.url);
  });
  // The same rule for the two other ways a main-frame navigation is announced (a file
  // dropped on the window, a server redirect). Blocking twice does no harm.
  for (const name of ['will-navigate', 'will-redirect']) {
    contents.on(name, (event, url) => {
      if (classify(url) !== 'app') event.preventDefault();
    });
  }

  contents.setWindowOpenHandler(({ url }) => {
    const kind = classify(url);
    if (kind === 'app') {
      // A figure image, for example. A second app window in the same session, so the
      // token header is still added and the same rules apply.
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          width: 1000, height: 760, minWidth: 480, minHeight: 360, backgroundColor: BACKGROUND,
          autoHideMenuBar: true, webPreferences,
        },
      };
    }
    if (kind === 'external') shell.openExternal(url);
    return { action: 'deny' };
  });
  contents.on('will-attach-webview', (event) => event.preventDefault());

  // A page that blocks unloading (unsaved changes) gets no prompt from Electron: the
  // window would silently refuse to close. Ask, so no page can make it impossible.
  contents.on('will-prevent-unload', (event) => {
    const window = BrowserWindow.fromWebContents(contents);
    const options = {
      type: 'question',
      title: 'AnkiGPT',
      message: 'Leave without saving?',
      detail: 'This page has changes that are not saved yet.',
      buttons: ['Leave', 'Stay'],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
    };
    const choice = window ? dialog.showMessageBoxSync(window, options) : dialog.showMessageBoxSync(options);
    if (choice === 0) event.preventDefault(); // preventing the event lets the page unload
  });

  contents.on('context-menu', (_event, params) => showContextMenu(contents, params));
}

/** Electron has no right-click menu of its own. The card editor needs one. */
function showContextMenu(contents, params) {
  const template = [];
  if (params.isEditable) {
    for (const suggestion of params.dictionarySuggestions.slice(0, 5)) {
      template.push({ label: suggestion, click: () => contents.replaceMisspelling(suggestion) });
    }
    if (params.misspelledWord && !params.dictionarySuggestions.length) {
      template.push({ label: 'No spelling suggestions', enabled: false });
    }
    if (template.length) template.push({ type: 'separator' });
    template.push(
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste },
    );
  } else if (params.selectionText.trim()) {
    template.push({ role: 'copy' });
  }
  if (!template.length) return;
  Menu.buildFromTemplate(template).popup({ window: BrowserWindow.fromWebContents(contents) || undefined });
}

// ------------------------------------------------------------------ window state
function readState(file) {
  try {
    const state = JSON.parse(fs.readFileSync(file, 'utf8'));
    return state && typeof state === 'object' ? state : {};
  } catch {
    return {};
  }
}

/** True when enough of the window, title bar included, sits on a monitor that is still there. */
function isOnScreen(bounds) {
  return screen.getAllDisplays().some(({ workArea }) => {
    const overlapX = Math.min(bounds.x + bounds.width, workArea.x + workArea.width) - Math.max(bounds.x, workArea.x);
    const titleBarVisible = bounds.y >= workArea.y - 8 && bounds.y <= workArea.y + workArea.height - 48;
    return overlapX >= 120 && titleBarVisible;
  });
}

function initialBounds(state) {
  const whole = (value) => Number.isFinite(value) && Math.round(value);
  const width = Math.max(whole(state.width) || DEFAULT_SIZE.width, MINIMUM_SIZE.width);
  const height = Math.max(whole(state.height) || DEFAULT_SIZE.height, MINIMUM_SIZE.height);
  const bounds = { width, height };
  if (Number.isFinite(state.x) && Number.isFinite(state.y)) {
    const placed = { x: Math.round(state.x), y: Math.round(state.y), width, height };
    // A position saved on a monitor that has since been unplugged is dropped; the size is kept.
    if (isOnScreen(placed)) Object.assign(bounds, placed);
  }
  return bounds;
}

// ------------------------------------------------------------------ windows
function createSplashWindow() {
  const splash = new BrowserWindow({
    width: 420, height: 260, frame: false, resizable: false, maximizable: false, fullscreenable: false,
    center: true, show: false, backgroundColor: BACKGROUND, title: 'AnkiGPT', icon: windowIcon(), webPreferences,
  });
  splash.once('ready-to-show', () => splash.show());
  splash.loadFile(path.join(__dirname, 'splash.html'));
  return splash;
}

/** `stateFile` is window-state.json: size, position, maximised and zoom. */
function createMainWindow(stateFile) {
  const state = readState(stateFile);
  const window = new BrowserWindow({
    ...initialBounds(state),
    minWidth: MINIMUM_SIZE.width,
    minHeight: MINIMUM_SIZE.height,
    backgroundColor: BACKGROUND,
    show: false,
    title: 'AnkiGPT',
    icon: windowIcon(),
    webPreferences,
  });
  if (state.maximized) window.maximize();

  // Zoom belongs to the origin, and the origin changes with the port on every launch.
  if (Number.isFinite(state.zoomLevel) && state.zoomLevel !== 0) {
    window.webContents.once('did-finish-load', () => window.webContents.setZoomLevel(state.zoomLevel));
  }

  window.on('close', () => {
    const saved = {
      ...window.getNormalBounds(),
      maximized: window.isMaximized(),
      zoomLevel: window.webContents.getZoomLevel(),
    };
    try {
      fs.writeFileSync(stateFile, JSON.stringify(saved, null, 2));
    } catch {
      // Losing the window position is not worth interrupting a quit for.
    }
  });
  return window;
}

/** A packaged build takes its icon from the .exe. In development, point at the file. */
function windowIcon() {
  return app.isPackaged ? undefined : path.join(__dirname, '..', 'resources', 'icon.ico');
}

module.exports = { createMainWindow, createSplashWindow, devToolsAllowed, protectSession, restrict };
