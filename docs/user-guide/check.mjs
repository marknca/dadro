// Run from any directory: node /path/to/dadro-src/docs/user-guide/check.mjs
// Standard-library check for the guide's links and interactive worked examples.
import assert from 'node:assert/strict';
import {readFileSync, existsSync} from 'node:fs';
import vm from 'node:vm';

const page = new URL('./index.html', import.meta.url);
const html = readFileSync(page, 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, 'IDs must be unique');
for (const section of ['quick-start', 'installation', 'setup', 'readout', 'datums', 'coordinates',
  'points', 'offsets', 'holes', 'tool-path', 'fast-mode', 'keyboard', 'practice', 'persistence',
  'troubleshooting', 'reference']) assert(ids.includes(section), `Missing chapter: ${section}`);
for (const [, attribute, href] of html.matchAll(/\b(href|src)="([^"]+)"/g)) {
  assert(!/^(https?:)?\/\//.test(href), `Guide should work offline: ${href}`);
  const target = new URL(href, page);
  const hash = decodeURIComponent(target.hash.slice(1));
  target.hash = '';
  assert(existsSync(target), `Missing ${attribute}: ${href}`);
  if (hash) assert(readFileSync(target, 'utf8').includes(`id="${hash}"`), `Missing anchor: ${href}`);
}
for (const [, tag] of html.matchAll(/<(img\b[^>]*|input\b[^>]*)>/g)) {
  if (tag.startsWith('img')) assert(/alt="[^"]+"/.test(tag), 'Images need useful alternative text');
  else {
    const id = tag.match(/id="([^"]+)"/)?.[1];
    assert(id && html.includes(`for="${id}"`), `Input needs a label: ${tag}`);
  }
}
const theme = readFileSync(new URL('../yadro-dos-commands.html', page), 'utf8');
for (const token of theme.match(/:root\{([^}]+)\}/)[1].split(';')) {
  assert(html.includes(token), `Keep the reference theme token: ${token}`);
}
assert(html.includes('<noscript>'), 'Provide a non-JavaScript explanation');
assert(html.includes('@media print'), 'Provide a printable guide');

const nodes = new Map(ids.map(id => [id, {
  value: '', textContent: '', innerHTML: '', disabled: true, events: {}, attributes: {},
  addEventListener(event, callback) { this.events[event] = callback; },
  setAttribute(key, value) { this.attributes[key] = String(value); }
}]));
const node = id => { assert(nodes.has(id), `Missing interactive element: ${id}`); return nodes.get(id); };
for (const [, id, value] of html.matchAll(/<input\b[^>]*id="([^"]+)"[^>]*value="([^"]+)"/g)) node(id).value = value;
Object.entries({'datum-mode': 'abs', 'datum-direction': '1', 'datum-units': 'mm'}).forEach(([id, value]) => node(id).value = value);
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {document: {getElementById: node}});
const click = id => { assert(!node(id).disabled, `${id} is disabled`); node(id).events.click(); };
const input = (id, value) => { node(id).value = String(value); node(id).events.input(); };
const change = (id, value) => { node(id).value = String(value); node(id).events.change(); };
assert(!node('datum-lab').disabled && !node('hole-lab').disabled);
assert.equal(node('datum-result').textContent, '25.400 mm');
change('datum-mode', 'rel'); click('datum-zero');
input('datum-position', 50.8); assert.equal(node('datum-result').textContent, '25.400 mm');
change('datum-units', 'in'); assert.equal(node('datum-result').textContent, '1.0000 in');
change('datum-mode', 'abs'); assert.equal(node('datum-result').textContent, '2.0000 in');
change('datum-mode', 'rel'); change('datum-direction', '-1');
assert.equal(node('datum-result').textContent, '-1.0000 in');
click('datum-clear'); assert.equal(node('datum-result').textContent, '-2.0000 in');
click('datum-reset'); click('datum-zero');
input('datum-position', 30.4); change('datum-mode', 'rel'); click('datum-zero');
change('datum-mode', 'abs'); input('datum-position', 40.4); click('datum-zero');
change('datum-mode', 'rel'); assert.equal(node('datum-result').textContent, '-5.000 mm', 'ABS zero retains REL offset');
for (const bad of ['', 'NaN', 'Infinity', '10000001']) {
  input('datum-position', bad); assert.equal(node('datum-result').textContent, '—');
  assert(node('datum-zero').disabled);
}
click('datum-reset'); assert.equal(node('datum-result').textContent, '25.400 mm');

assert.equal(node('hole-target').textContent, 'Hole 1 of 4 · X 10.000 mm · Y 15.000 mm');
assert(node('hole-prev').disabled);
for (const [index, x, y] of [[2, 5, 20], [3, 10, 25], [4, 15, 20]]) {
  click('hole-next');
  assert.equal(node('hole-target').textContent, `Hole ${index} of 4 · X ${x.toFixed(3)} mm · Y ${y.toFixed(3)} mm`);
}
assert(node('hole-next').disabled);
click('hole-prev'); input('hole-number', 1); input('hole-angle', 90);
assert.match(node('hole-target').textContent, /X 5.000 mm · Y 20.000 mm/);
input('hole-angle', -90); assert.match(node('hole-target').textContent, /X 15.000 mm · Y 20.000 mm/);
input('hole-count', 1); assert(node('hole-next').disabled && node('hole-prev').disabled);
for (const [id, bad] of [['hole-count', 0], ['hole-count', 1.5], ['hole-count', 65],
  ['hole-diameter', 0], ['hole-diameter', -1], ['hole-angle', 361], ['hole-center-x', ''],
  ['hole-center-y', 'Infinity'], ['hole-number', 0], ['hole-number', 5]]) {
  click('hole-reset'); input(id, bad);
  assert.equal(node('hole-target').textContent, '—', `Reject invalid input ${id}=${bad}`);
  assert(node('hole-prev').disabled && node('hole-next').disabled);
  assert.equal(node('hole-markers').innerHTML, '');
}
click('hole-reset'); input('hole-count', 64); input('hole-number', 64);
assert.match(node('hole-target').textContent, /Hole 64 of 64/);
assert.equal((node('hole-markers').innerHTML.match(/<circle /g) || []).length, 64);
input('hole-count', 2); assert.equal(node('hole-number').value, '2', 'Smaller patterns clamp the selected hole');
click('hole-reset');
assert.match(node('hole-markers').innerHTML, /aria-hidden="true"/);
console.log('PASS: offline links, chapter anchors, labels, theme, datum transitions, circle targets, boundaries, and invalid input.');
