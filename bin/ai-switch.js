#!/usr/bin/env node

/**
 * AI Switch - Node.js CLI wrapper
 * Checks Python availability, installs deps if needed, then launches the app.
 */

'use strict';

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const APP_DIR = path.resolve(__dirname, '..');
const REQUIREMENTS = path.join(APP_DIR, 'requirements.txt');

// ── Helpers ──────────────────────────────────────────────

function log(msg) {
  console.log(`\x1b[36m[ai-switch]\x1b[0m ${msg}`);
}

function warn(msg) {
  console.log(`\x1b[33m[ai-switch]\x1b[0m ${msg}`);
}

function err(msg) {
  console.error(`\x1b[31m[ai-switch]\x1b[0m ${msg}`);
}

function findPython() {
  const candidates = ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      const { execSync } = require('child_process');
      const ver = execSync(`${cmd} --version 2>&1`, { encoding: 'utf8' }).trim();
      if (ver.includes('Python 3.')) return cmd;
    } catch (_) {}
  }
  return null;
}

function depsInstalled() {
  try {
    const { execSync } = require('child_process');
    execSync('python3 -c "import flask"', { stdio: 'ignore' });
    return true;
  } catch (_) {
    return false;
  }
}

function installDeps(python) {
  log('Installing Python dependencies...');
  try {
    const { execSync } = require('child_process');
    execSync(`${python} -m pip install -r "${REQUIREMENTS}" --quiet`, {
      stdio: 'inherit',
      cwd: APP_DIR,
    });
    log('Dependencies installed.');
  } catch (e) {
    err('Failed to install dependencies. Run manually:');
    err(`  ${python} -m pip install -r requirements.txt`);
    process.exit(1);
  }
}

// ── Main ─────────────────────────────────────────────────

function main() {
  const python = findPython();
  if (!python) {
    err('Python 3 is required but not found.');
    err('Install from https://www.python.org/downloads/');
    process.exit(1);
  }

  if (!depsInstalled()) {
    installDeps(python);
  }

  const args = [path.join(APP_DIR, 'run.py'), ...process.argv.slice(2)];
  const child = spawn(python, args, {
    cwd: APP_DIR,
    stdio: 'inherit',
    env: { ...process.env },
  });

  child.on('close', (code) => {
    process.exit(code || 0);
  });
}

main();
