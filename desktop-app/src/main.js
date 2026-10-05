/*
 * AnkiGPT Desktop: the Electron shell (desktop-app/SPEC.md).
 *
 * The shell has no application logic. It starts the Python backend as a hidden child
 * process, shows the backend's pages in a window, and does the things only a desktop
 * program can do: the single-instance lock, menus, dialogs, downloads, updates, and
 * keeping the install secret.
 */
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { app, dialog, Menu, powerSaveBlocker, session, shell } = require('electron');

const { Backend } = require('./backend');
const { buildMenu } = require('./menu');
const { loadInstallSecret } = require('./secret');
const { setupUpdates } = require('./updater');
const { createMainWindow, createSplashWindow, devToolsAllowed, protectSession, restrict } = require('./window');

const MAIN_LOG_BYTES = 2 * 1024 * 1024;

// A development run keeps its own folder, so it can never touch real decks.
if (!app.isPackaged) app.setPath('userData', path.join(app.getPath('appData'), 'AnkiGPT Dev'));
app.setAppUserModelId('com.ashtonlong.ankigpt');

const userData = app.getPath('userData');
const folders = { data: path.join(userData, 'data'), logs: path.join(userData, 'logs') };

let log = null;
let backend = null;
let mainWindow = null;
let splash = null;
let origin = null;
let updates = null;
let sleepBlocker = null;
let quitConfirmed = false; // the user chose "Quit anyway", or nothing is generating
let backendStopped = false;
let failed = false; // a failure dialog is showing; don't stack another

// ------------------------------------------------------------------ logging
function createLog(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  try {
    if (fs.statSync(file).size > MAIN_LOG_BYTES) fs.renameSync(file, `${file}.1`);
  } catch {
    // No log yet.
  }
  // Written synchronously: the lines that matter most are the last ones before the app
  // exits, and a buffered write would lose exactly those.
  const write = (level) => (message) => {
    try {
      fs.appendFileSync(file, `${new Date().toISOString()} ${level} ${message}\n`);
    } catch {
      // A full disk must not take the app down.
    }
  };
  return { info: write('INFO'), warn: write('WARNING'), error: write('ERROR'), debug: () => {} };
}

// ------------------------------------------------------------------ dialogs
function activeRuns() {
  return backend ? backend.activeRuns : 0;
}

function liveWindow() {
  return mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
}

/** Closing or quitting while a deck is generating stops it, so ask first. True means quit. */
function confirmQuit() {
  if (quitConfirmed || activeRuns() === 0) return true;
  const options = {
    type: 'warning',
    title: 'AnkiGPT',
    message: 'A deck is still being generated.',
    detail: 'Quitting now stops it. You can retry the deck the next time you open AnkiGPT.',
    buttons: ['Keep generating', 'Quit anyway'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  };
  const window = liveWindow();
  const choice = window ? dialog.showMessageBoxSync(window, options) : dialog.showMessageBoxSync(options);
  quitConfirmed = choice === 1;
  return quitConfirmed;
}

function startupFailed(error) {
  if (failed) return;
  failed = true;
  // The log itself may be what failed, if the logs folder could not be made.
  if (log) log.error(`Start-up failed: ${error && error.stack ? error.stack : error}`);
  if (splash && !splash.isDestroyed()) splash.destroy();
  const choice = dialog.showMessageBoxSync({
    type: 'error',
    title: 'AnkiGPT',
    message: "AnkiGPT couldn't start",
    detail: 'The part of AnkiGPT that stores and builds your decks did not start. The logs may show why.',
    buttons: ['Open logs', 'Quit'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  if (choice === 0) shell.openPath(folders.logs);
  quitConfirmed = true;
  app.quit();
}

function backendStoppedWorking(code) {
  if (failed) return;
  failed = true;
  log.error(`The backend stopped while the app was open (exit code ${code})`);
  const options = {
    type: 'error',
    title: 'AnkiGPT',
    message: 'AnkiGPT stopped working',
    detail: 'Your decks are saved. Restart AnkiGPT to carry on. A deck that was generating can be retried.',
    buttons: ['Restart', 'Quit'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  };
  const window = liveWindow();
  const choice = window ? dialog.showMessageBoxSync(window, options) : dialog.showMessageBoxSync(options);
  quitConfirmed = true;
  if (choice === 0) app.relaunch();
  app.quit();
}

// ------------------------------------------------------------------ activity
function activityChanged(runs) {
  // Keep Windows from sleeping in the middle of a run. The screen may still turn off.
  if (runs > 0 && sleepBlocker === null) {
    sleepBlocker = powerSaveBlocker.start('prevent-app-suspension');
  } else if (runs === 0 && sleepBlocker !== null) {
    powerSaveBlocker.stop(sleepBlocker);
    sleepBlocker = null;
  }
  if (runs === 0) quitConfirmed = false; // a later run gets its own warning
  if (updates) updates.activityChanged();
}

// ------------------------------------------------------------------ start-up
async function start() {
  log = createLog(path.join(folders.logs, 'main.log'));
  log.info(`AnkiGPT ${app.getVersion()} starting (${app.isPackaged ? 'packaged' : 'development'}), data in ${userData}`);
  Menu.setApplicationMenu(null); // no menu until there is an app behind it
  splash = createSplashWindow();

  // The install secret outlives every launch. The launch token is new each time.
  const secret = loadInstallSecret(path.join(userData, 'secret.bin'), log);
  const token = crypto.randomBytes(32).toString('hex');

  backend = new Backend({
    ANKIGPT_DATA_DIR: folders.data,
    ANKIGPT_LOG_DIR: folders.logs,
    ANKIGPT_TOKEN: token,
    SECRET_KEY: secret,
    ANKIGPT_VERSION: app.getVersion(),
  }, log);
  backend.on('activity', activityChanged);
  backend.on('stopped', backendStoppedWorking);
  const port = await backend.start();
  origin = `http://127.0.0.1:${port}`;
  log.info(`The backend is ready on port ${port}`);

  protectSession(session.defaultSession, origin, token, log);
  // The port, and so the origin, changes every launch. Clearing the cache also
  // guarantees fresh static files after an update.
  await session.defaultSession.clearCache();

  mainWindow = createMainWindow(path.join(userData, 'window-state.json'));
  mainWindow.on('close', (event) => {
    if (!confirmQuit()) event.preventDefault();
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  let shown = false;
  mainWindow.webContents.on('did-fail-load', (_event, code, description, _url, isMainFrame) => {
    // -3 is a load that was replaced by another one, which is not a failure.
    if (shown || !isMainFrame || code === -3) return;
    startupFailed(new Error(`The first page did not load: ${description} (${code})`));
  });
  mainWindow.once('ready-to-show', () => {
    if (failed) return;
    shown = true;
    mainWindow.show();
    if (splash && !splash.isDestroyed()) splash.destroy();
    splash = null;
  });

  updates = setupUpdates({ window: liveWindow, activeRuns, stopBackend }, log);
  Menu.setApplicationMenu(buildMenu({
    folders,
    devTools: devToolsAllowed,
    actions: {
      window: liveWindow,
      navigate(pagePath) {
        const window = liveWindow();
        if (!window) return;
        if (window.isMinimized()) window.restore();
        window.focus();
        window.loadURL(origin + pagePath);
      },
      openFolder: (folder) => shell.openPath(folder),
      checkForUpdates: () => updates.checkNow(),
    },
  }));

  // A failed load is reported by 'did-fail-load' above.
  mainWindow.loadURL(`${origin}/decks`).catch(() => {});
}

function stopBackend() {
  if (backendStopped || !backend) return Promise.resolve();
  backendStopped = true;
  return backend.stop();
}

function backendIsRunning() {
  return Boolean(backend) && !backendStopped && backend.isRunning();
}

// ------------------------------------------------------------------ app lifecycle
// Two copies would share one database, and the second would mark the first's runs as failed.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // While starting, the main window exists but is still hidden: bring the splash forward instead.
    const window = splash && !splash.isDestroyed() ? splash : liveWindow();
    if (!window) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  });

  // Every window, the second ones opened for figures included, gets the same restrictions.
  app.on('web-contents-created', (_event, contents) => restrict(contents));

  app.on('before-quit', (event) => {
    if (!confirmQuit()) event.preventDefault();
  });

  // Closing the window quits the app. There is no tray icon. A failure dialog quits by
  // itself once it has been answered.
  app.on('window-all-closed', () => {
    if (!failed) app.quit();
  });

  app.on('will-quit', (event) => {
    if (!backendIsRunning()) return;
    // Hold the quit until the backend has gone, so it never outlives the app. Quit again
    // on a later tick: Electron ignores a quit requested while this event is still being
    // handled, and the app would be left running with no window.
    event.preventDefault();
    stopBackend().then(() => setImmediate(() => app.quit()));
  });

  app.whenReady().then(start).catch(startupFailed);
}
