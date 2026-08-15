#!/usr/bin/env node

/**
 * postinstall - Auto-install Python deps after npm install
 */

'use strict';

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const APP_DIR = path.resolve(__dirname, '..');
const REQUIREMENTS = path.join(APP_DIR, 'requirements.txt');

function log(msg) {
  console.log(`\x1b[36m[ai-switch]\x1b[0m ${msg}`);
}

function findPython() {
  for (const cmd of ['python3', 'python']) {
    try {
      const ver = execSync(`${cmd} --version 2>&1`, { encoding: 'utf8' }).trim();
      if (ver.includes('Python 3.')) return cmd;
    } catch (_) {}
  }
  return null;
}

const python = findPython();
if (!python) {
  log('⚠ Python 3 not found — run `ai-switch` after installing Python 3.9+');
  process.exit(0);
}

try {
  log('Installing Python dependencies...');
  execSync(`${python} -m pip install -r "${REQUIREMENTS}" --quiet`, {
    stdio: 'inherit',
    cwd: APP_DIR,
  });
  log('✓ Ready! Run: npx ai-switch');
} catch (e) {
  log('⚠ Auto-install failed. Run manually:');
  log(`  ${python} -m pip install -r requirements.txt`);
}
