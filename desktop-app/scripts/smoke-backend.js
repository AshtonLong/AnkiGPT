#!/usr/bin/env node
/*
 * Start the desktop backend the way the Electron shell does and check the contract
 * between them (desktop-app/SPEC.md section 10.2):
 *
 *   - it prints {"event":"ready","port":N}, and nothing on stdout that is not JSON
 *   - it serves /decks to a request carrying the launch token
 *   - it refuses requests with no token, a wrong token, or a wrong Host, static files included
 *   - it exits within 3 seconds of its stdin closing
 *
 *   node scripts/smoke-backend.js                     the frozen backend in backend-dist
 *   node scripts/smoke-backend.js path\to\backend.exe
 *   node scripts/smoke-backend.js backend\entry.py    unfrozen, with python (or %ANKIGPT_PYTHON%)
 */
'use strict';

const { spawn } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');

const READY_TIMEOUT_MS = 30_000;
const EXIT_LIMIT_MS = 3_000;

const desktop = path.resolve(__dirname, '..');
const target = path.resolve(process.argv[2] || path.join(desktop, 'backend-dist', 'ankigpt-backend', 'ankigpt-backend.exe'));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'ankigpt-smoke-'));
const token = crypto.randomBytes(32).toString('hex');
const failures = [];

function check(name, passed, detail = '') {
  console.log(`${passed ? '  ok  ' : ' FAIL '} ${name}${detail ? ` (${detail})` : ''}`);
  if (!passed) failures.push(name);
}

function get(port, urlPath, headers = {}) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: '127.0.0.1', port, path: urlPath, headers }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body }));
    });
    request.on('error', reject);
  });
}

async function main() {
  if (!fs.existsSync(target)) throw new Error(`No backend at ${target}. Build it first: npm run build:backend`);
  const python = target.endsWith('.py');
  const command = python ? (process.env.ANKIGPT_PYTHON || 'python') : target;
  console.log(`Backend: ${target}`);

  const started = Date.now();
  const child = spawn(command, python ? [target] : [], {
    cwd: scratch,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      ANKIGPT_DATA_DIR: path.join(scratch, 'data'),
      ANKIGPT_LOG_DIR: path.join(scratch, 'logs'),
      ANKIGPT_TOKEN: token,
      SECRET_KEY: crypto.randomBytes(48).toString('base64url'),
      ANKIGPT_VERSION: '0.0.0-smoke',
    },
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  const exited = new Promise((resolve) => child.once('exit', (code) => resolve(code)));

  const stray = [];
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`not ready after ${READY_TIMEOUT_MS / 1000} s`)), READY_TIMEOUT_MS);
    child.once('error', reject);
    exited.then((code) => reject(new Error(`exited with code ${code} before it was ready`)));
    readline.createInterface({ input: child.stdout }).on('line', (line) => {
      let message;
      try { message = JSON.parse(line); } catch { stray.push(line); return; }
      if (message.event === 'ready') { clearTimeout(timer); resolve(message.port); }
    });
  }).catch((error) => {
    child.kill();
    throw new Error(`${error.message}\n${stderr}`);
  });

  try {
    check('prints the ready message', Number.isInteger(port) && port > 0, `port ${port}, ${Date.now() - started} ms`);
    const here = { 'X-AnkiGPT-Token': token };

    const decks = await get(port, '/decks', here);
    check('serves /decks with the token', decks.status === 200 && decks.body.includes('Your decks'), `HTTP ${decks.status}`);
    check('needs no sign-in', !decks.body.includes('Sign in'));
    check('redirects / to the library', (await get(port, '/', here)).status === 302);
    check('serves static files with the token', (await get(port, '/static/style.css', here)).status === 200);
    check('serves the bundled font', (await get(port, '/static/fonts/figtree-latin.woff2', here)).status === 200);

    check('refuses a request with no token', (await get(port, '/decks')).status === 403);
    check('refuses a wrong token', (await get(port, '/decks', { 'X-AnkiGPT-Token': 'f'.repeat(64) })).status === 403);
    check('refuses a static file with no token', (await get(port, '/static/style.css')).status === 403);
    check('refuses a wrong Host', (await get(port, '/decks', { ...here, Host: `localhost:${port}` })).status === 403);
    check('refuses a rebound Host', (await get(port, '/decks', { ...here, Host: `attacker.example:${port}` })).status === 403);

    check('keeps its data in the data folder', fs.existsSync(path.join(scratch, 'data', 'ankigpt.db')));
    check('writes its log file', fs.existsSync(path.join(scratch, 'logs', 'backend.log')));
    check('prints only JSON on stdout', stray.length === 0, stray.slice(0, 2).join(' | '));
  } finally {
    const closed = Date.now();
    child.stdin.end();
    const code = await Promise.race([exited, new Promise((resolve) => setTimeout(() => resolve('timeout'), EXIT_LIMIT_MS + 2000))]);
    const took = Date.now() - closed;
    if (code === 'timeout') child.kill();
    check('exits within 3 seconds of stdin closing', code === 0 && took <= EXIT_LIMIT_MS, `exit ${code}, ${took} ms`);
  }
}

main()
  .catch((error) => { console.error(`\n${error.message}`); failures.push('smoke test could not run'); })
  .finally(() => {
    // Give Windows a moment to let go of the database file before deleting it.
    setTimeout(() => {
      try { fs.rmSync(scratch, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch { /* scratch files only */ }
      if (failures.length) {
        console.error(`\n${failures.length} check(s) failed.`);
        process.exit(1);
      }
      console.log('\nThe backend passes the smoke test.');
    }, 200);
  });
