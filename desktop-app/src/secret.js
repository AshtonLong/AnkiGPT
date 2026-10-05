/*
 * The install secret (SPEC.md section 4.2).
 *
 * It becomes the backend's SECRET_KEY: it signs the session cookie and derives the key
 * that encrypts the saved OpenRouter key. It is made once, on first launch, and kept in
 * secret.bin encrypted by Windows (DPAPI, through Electron's safeStorage), so it can only
 * be read by the same Windows user on the same computer.
 */
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const { safeStorage } = require('electron');

const SECRET_BYTES = 48;

function loadInstallSecret(file, log) {
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('Windows encryption (DPAPI) is not available, so the install secret cannot be protected.');
  }
  try {
    const secret = safeStorage.decryptString(fs.readFileSync(file));
    if (secret.length >= SECRET_BYTES) return secret;
    log.warn('secret.bin is too short to be an install secret; making a new one');
  } catch (error) {
    if (error.code !== 'ENOENT') log.warn(`secret.bin could not be read (${error.message}); making a new one`);
  }
  // A new secret makes a saved OpenRouter key unreadable. The Settings page notices
  // and asks for the key again; decks are not affected.
  const secret = crypto.randomBytes(SECRET_BYTES).toString('base64url');
  const partial = `${file}.tmp`;
  fs.writeFileSync(partial, safeStorage.encryptString(secret));
  fs.renameSync(partial, file);
  log.info('Created a new install secret');
  return secret;
}

module.exports = { loadInstallSecret };
