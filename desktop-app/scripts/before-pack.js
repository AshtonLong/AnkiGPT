/*
 * electron-builder hook: stop before packaging if the frozen backend has not been built.
 * Without this the installer would be made anyway and would install an app that cannot start.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');

exports.default = async function beforePack() {
  const backend = path.resolve(__dirname, '..', 'backend-dist', 'ankigpt-backend', 'ankigpt-backend.exe');
  if (!fs.existsSync(backend)) {
    throw new Error(`The frozen backend is missing (${backend}). Build it first: npm run build:backend`);
  }
};
