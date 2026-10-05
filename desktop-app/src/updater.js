/*
 * Updates through electron-updater and GitHub Releases (SPEC.md section 9.2).
 *
 *   - Check 10 seconds after start and every 6 hours; download in the background.
 *   - When an update has downloaded, offer "Restart now" or "Later". The offer waits
 *     while a deck is generating. "Later" installs it when the app next quits.
 *   - Help -> Check for updates... checks at once and always says what it found.
 *   - A failed automatic check shows nothing: it is logged and tried again next time.
 *   - Updates are off in development builds.
 */
'use strict';

const { app, dialog } = require('electron');

const FIRST_CHECK_MS = 10_000;
const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;

/**
 * `host` supplies: window(), activeRuns(), and stopBackend() (returns a promise).
 * Returns { checkNow, activityChanged }.
 */
function setupUpdates(host, log) {
  // The last message shown. The restart offer waits for it to be dismissed, so a download
  // that finishes at once cannot put a second dialog on top of the first.
  let notice = Promise.resolve();
  const say = (message, detail, type = 'info') => {
    const options = { type, title: 'AnkiGPT', message, detail, buttons: ['OK'], noLink: true };
    const window = host.window();
    notice = (window ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options)).catch(() => {});
    return notice;
  };

  if (!app.isPackaged) {
    return {
      checkNow: () => say('Updates are off in development builds.', `You are running version ${app.getVersion()} from source.`),
      activityChanged: () => {},
    };
  }

  const { autoUpdater } = require('electron-updater');
  autoUpdater.logger = log;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  let askedByUser = false; // the check in flight came from the Help menu
  let downloaded = null; // the version that is ready to install
  let offering = false;
  let postponed = false;

  const offerRestart = async () => {
    if (!downloaded || offering || postponed || host.activeRuns() > 0) return;
    offering = true;
    await notice;
    if (host.activeRuns() > 0) {
      offering = false; // a run began meanwhile; the offer is made again when it ends
      return;
    }
    const options = {
      type: 'info',
      title: 'AnkiGPT',
      message: `AnkiGPT ${downloaded} is ready to install.`,
      detail: 'Restart to finish updating. Your decks and settings are kept.',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    };
    const window = host.window();
    const { response } = await (window ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options));
    offering = false;
    if (response !== 0) {
      postponed = true; // autoInstallOnAppQuit installs it when the app next quits
      return;
    }
    if (host.activeRuns() > 0) return; // a run started while the dialog was open
    // The installer replaces the backend's files, so the backend must be gone first.
    await host.stopBackend();
    autoUpdater.quitAndInstall();
  };

  autoUpdater.on('update-available', (info) => {
    if (!askedByUser) return;
    askedByUser = false;
    say(`AnkiGPT ${info.version} is available.`, 'It is downloading in the background. You will be asked to restart when it is ready.');
  });
  autoUpdater.on('update-not-available', () => {
    if (!askedByUser) return;
    askedByUser = false;
    say('You have the latest version.', `AnkiGPT ${app.getVersion()} is up to date.`);
  });
  autoUpdater.on('update-downloaded', (info) => {
    downloaded = info.version;
    offerRestart();
  });
  autoUpdater.on('error', (error) => {
    log.warn(`Update check failed: ${error && error.message}`);
    if (!askedByUser) return;
    askedByUser = false;
    say("Couldn't check for updates.", 'Check your internet connection and try again.', 'warning');
  });

  // Failures arrive through the 'error' event too. This only stops an unhandled rejection.
  const check = () => autoUpdater.checkForUpdates().catch(() => {});
  setTimeout(check, FIRST_CHECK_MS);
  setInterval(check, CHECK_EVERY_MS);

  return {
    checkNow() {
      if (downloaded) {
        postponed = false;
        if (host.activeRuns() > 0) {
          say(`AnkiGPT ${downloaded} is ready to install.`, 'You will be asked to restart when your deck has finished generating.');
        } else {
          offerRestart();
        }
        return;
      }
      askedByUser = true;
      check();
    },
    activityChanged: offerRestart,
  };
}

module.exports = { setupUpdates };
