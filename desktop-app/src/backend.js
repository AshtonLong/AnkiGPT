/*
 * The Python backend as a child process (SPEC.md sections 2.1 to 2.3).
 *
 * Start it hidden, read its stdout as one JSON message per line, and stop it by closing
 * its stdin. If it has not gone 3 seconds later, kill its whole process tree.
 */
'use strict';

const { execFile, spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { app } = require('electron');

const READY_TIMEOUT_MS = 30_000;
const STOP_GRACE_MS = 3_000;
const KILL_WAIT_MS = 2_000;

function command() {
  if (app.isPackaged) {
    const folder = path.join(process.resourcesPath, 'backend');
    return { file: path.join(folder, 'ankigpt-backend.exe'), args: [], cwd: folder };
  }
  // Development: run the unfrozen backend with the checkout's own Python.
  const repo = path.resolve(__dirname, '..', '..');
  const venv = path.join(repo, '.venv', 'Scripts', 'python.exe');
  const python = process.env.ANKIGPT_PYTHON || (fs.existsSync(venv) ? venv : 'python');
  return { file: python, args: [path.join(repo, 'desktop-app', 'backend', 'entry.py')], cwd: repo };
}

class Backend extends EventEmitter {
  /**
   * `env` holds the variables from SPEC.md section 2.1.
   * Emits `activity` (the number of decks generating) and `stopped` (it exited after
   * it was ready, without being asked to).
   */
  constructor(env, log) {
    super();
    this.env = env;
    this.log = log;
    this.child = null;
    this.ready = false;
    this.stopping = false;
    this.activeRuns = 0;
  }

  /** Resolves with the port once the backend is listening. */
  start() {
    return new Promise((resolve, reject) => {
      const { file, args, cwd } = command();
      this.log.info(`Starting the backend: ${file}`);
      const child = spawn(file, args, {
        cwd,
        env: { ...process.env, ...this.env },
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      this.child = child;

      const timer = setTimeout(() => {
        fail(new Error(`The backend was not ready after ${READY_TIMEOUT_MS / 1000} seconds.`));
        this.killTree();
      }, READY_TIMEOUT_MS);
      const fail = (error) => {
        clearTimeout(timer);
        reject(error);
      };

      child.once('error', (error) => fail(new Error(`The backend could not be started: ${error.message}`)));
      child.once('exit', (code, signal) => {
        this.log.info(`The backend exited (code ${code}, signal ${signal})`);
        if (!this.ready) {
          fail(new Error(`The backend exited before it was ready (code ${code}).`));
        } else if (!this.stopping) {
          this.emit('stopped', code);
        }
      });

      // stdout carries only JSON messages. Logging goes to backend.log.
      readline.createInterface({ input: child.stdout }).on('line', (line) => {
        let message;
        try {
          message = JSON.parse(line);
        } catch {
          this.log.warn(`[backend stdout] ${line}`);
          return;
        }
        if (message.event === 'ready' && Number.isInteger(message.port)) {
          this.ready = true;
          clearTimeout(timer);
          resolve(message.port);
        } else if (message.event === 'activity') {
          this.activeRuns = Number(message.active_runs) || 0;
          this.emit('activity', this.activeRuns);
        }
      });
      // stderr goes to main.log, so a crash before the backend's own log exists leaves a trace.
      readline.createInterface({ input: child.stderr }).on('line', (line) => this.log.info(`[backend] ${line}`));
      // The pipe closing under us (the backend died) must not become an unhandled error.
      child.stdin.on('error', () => {});
    });
  }

  isRunning() {
    const child = this.child;
    return Boolean(child && child.pid && child.exitCode === null && child.signalCode === null);
  }

  /** Ask the backend to exit, and make sure it has. Never rejects. */
  stop() {
    const child = this.child;
    if (!this.isRunning()) return Promise.resolve();
    this.stopping = true;
    return new Promise((resolve) => {
      const grace = setTimeout(() => {
        this.log.warn('The backend did not exit in time; killing its process tree');
        this.killTree();
        setTimeout(resolve, KILL_WAIT_MS);
      }, STOP_GRACE_MS);
      child.once('exit', () => {
        clearTimeout(grace);
        resolve();
      });
      // The backend watches for end-of-file on stdin and exits when it sees it.
      child.stdin.end();
    });
  }

  /** The tree, not just the process: the PDF layout library can start worker processes. */
  killTree() {
    const child = this.child;
    if (!this.isRunning()) return;
    execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }, (error) => {
      if (error) this.log.warn(`taskkill failed: ${error.message}`);
    });
  }
}

module.exports = { Backend };
