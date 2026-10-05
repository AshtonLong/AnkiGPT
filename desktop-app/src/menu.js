/*
 * The application menu (SPEC.md section 7.4).
 */
'use strict';

const { app, dialog, Menu, shell } = require('electron');

const USER_GUIDE = 'https://github.com/AshtonLong/AnkiGPT/blob/main/docs/user-guide.md';

/**
 * `actions` supplies what the menu cannot do by itself:
 *   navigate(path)   show a page of the app in the main window
 *   openFolder(dir)  open a folder in Explorer
 *   checkForUpdates()
 *   window()         the main window, for dialogs
 * `folders` holds the data and logs folders. `devTools` adds the developer item.
 */
function buildMenu({ actions, folders, devTools }) {
  const about = () => {
    const options = {
      type: 'info',
      title: 'About AnkiGPT',
      message: 'AnkiGPT',
      detail: [
        `Version ${app.getVersion()}`,
        '',
        'Turn PDFs and notes into editable Anki flashcards, on your own computer.',
        '',
        `Electron ${process.versions.electron} · Chromium ${process.versions.chrome}`,
      ].join('\n'),
      buttons: ['OK'],
      noLink: true,
    };
    const window = actions.window();
    return window ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options);
  };

  return Menu.buildFromTemplate([
    {
      label: '&File',
      submenu: [
        { label: '&New deck', accelerator: 'Ctrl+N', click: () => actions.navigate('/decks/new') },
        { label: 'Your &decks', click: () => actions.navigate('/decks') },
        { label: '&Settings', accelerator: 'Ctrl+,', click: () => actions.navigate('/auth/profile') },
        { type: 'separator' },
        { label: '&Open data folder', click: () => actions.openFolder(folders.data) },
        { type: 'separator' },
        { label: '&Quit', accelerator: 'Ctrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '&Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { type: 'separator' },
        { role: 'selectAll', label: 'Select all' },
      ],
    },
    {
      label: '&View',
      submenu: [
        { role: 'reload', accelerator: 'Ctrl+R' },
        { type: 'separator' },
        { role: 'zoomIn', label: 'Zoom in', accelerator: 'Ctrl+=' },
        { role: 'zoomOut', label: 'Zoom out' },
        { role: 'resetZoom', label: 'Actual size' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: 'Full screen', accelerator: 'F11' },
        ...(devTools ? [{ type: 'separator' }, { role: 'toggleDevTools' }] : []),
      ],
    },
    {
      label: '&Help',
      submenu: [
        { label: '&User guide', click: () => shell.openExternal(USER_GUIDE) },
        { label: 'Check for &updates…', click: () => actions.checkForUpdates() },
        { type: 'separator' },
        { label: 'Open &logs folder', click: () => actions.openFolder(folders.logs) },
        { type: 'separator' },
        { label: '&About AnkiGPT', click: about },
      ],
    },
  ]);
}

module.exports = { buildMenu };
