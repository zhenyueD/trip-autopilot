'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = __dirname;
const deployed = path.resolve(root, '../../research/manus-frontend-baseline-2026-11-06/source/public');
const assets = ['design-tokens.css', 'handbook-presentation.css', 'handbook-presentation.js'];

test('Manus deployment copies are byte-identical to canonical presentation assets', () => {
  for (const name of assets) assert.deepEqual(fs.readFileSync(path.join(deployed, name)), fs.readFileSync(path.join(root, name)), name);
});

test('shared tokens and both shells use the canonical accent', () => {
  const tokens = fs.readFileSync(path.join(root, 'design-tokens.css'), 'utf8');
  assert.match(tokens, /--ta-accent:\s*#0071E3;/);
  for (const file of ['session.html', 'session.css', 'handbook-presentation.css']) {
    const text = fs.readFileSync(path.join(root, file), 'utf8');
    assert.match(text, file.endsWith('.html') ? /design-tokens\.css/ : /var\(--ta-/);
  }
  assert.match(fs.readFileSync(path.join(root, 'DESIGN.md'), 'utf8'), /#0071E3/);
});
