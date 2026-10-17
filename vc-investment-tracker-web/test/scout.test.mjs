import { test } from 'node:test';
import assert from 'node:assert/strict';
await import('../public/scout.js');
const VS = globalThis.VentureScout;

const memoryStorage = () => { const m = new Map(); return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)) }; };

test('saved companies persist in browser storage, deduplicate by website, and support more than 20', () => {
  const storage = memoryStorage();
  let list = VS.upsertCompany([], { name: 'Acme', website: 'https://acme.test', stage: 'Seed' });
  assert.throws(() => VS.upsertCompany(list, { name: 'Acme again', website: 'https://www.acme.test/about' }), /already tracking/);
  list = VS.upsertCompany(list, { id: list[0].id, name: 'Acme Inc', website: 'https://acme.test', stage: 'Series A' });
  assert.equal(list.length, 1); assert.equal(list[0].stage, 'Series A');
  for (let i = 0; i < 25; i++) list = VS.upsertCompany(list, { name: 'Co ' + i, website: `https://co${i}.test` });
  VS.writeSaved(storage, list);
  assert.equal(VS.loadSaved(storage).length, 26);
  assert.equal(VS.removeCompany(list, list[0].id).length, 25);
});

test('unsafe or incomplete input is rejected', () => {
  assert.throws(() => VS.validateCompany({ name: 'X', website: 'javascript:alert(1)' }), /valid company website/);
  assert.throws(() => VS.validateCompany({ name: 'X', website: 'https://user:pw@example.com' }));
  assert.throws(() => VS.validateCompany({ name: '', website: 'https://x.test' }), /company name/);
  assert.equal(VS.validateCompany({ name: 'X', website: 'https://x.test', stage: 'Unicorn' }).stage, 'Not disclosed');
});

test('corrupt storage does not break the site', () => {
  const s = memoryStorage(); s.setItem(VS.STORAGE_KEY, '{not json');
  assert.deepEqual(VS.loadSaved(s), []);
});

test('backup restore merges and skips duplicates', () => {
  const list = VS.upsertCompany([], { name: 'Acme', website: 'https://acme.test' });
  const r = VS.importBackup(list, { companies: [{ name: 'Acme', website: 'https://acme.test' }, { name: 'Beta', website: 'https://beta.test' }, { name: 'Bad', website: 'ftp://x' }] });
  assert.equal(r.added, 1); assert.equal(r.skipped, 2); assert.equal(r.list.length, 2);
  assert.throws(() => VS.importBackup([], { hello: 1 }), /not a VentureScout backup/);
});

test('discovery filters and sorting', () => {
  const cos = [
    { name: 'A', industry: 'Fintech', funding_stage: 'Seed', confidence: { score: 80 }, investment_score: { total: 50 }, latest_funding: { evidence_status: 'confirmed', amount: 5 }, first_discovered_at: '2026-10-01' },
    { name: 'B', industry: 'Fintech', funding_stage: 'Series A', confidence: { score: 30 }, investment_score: { total: 70 }, latest_funding: { evidence_status: 'rumor', amount: 50 }, first_discovered_at: '2026-10-05' },
    { name: 'C', industry: 'Biotechnology', confidence: { score: 60 }, investment_score: null, latest_funding: {}, first_discovered_at: '2026-10-03' },
  ];
  assert.deepEqual(VS.filterDiscovery(cos).map(c => c.name), ['B', 'C', 'A']);
  assert.deepEqual(VS.filterDiscovery(cos, { industry: 'Fintech', sort: 'score' }).map(c => c.name), ['B', 'A']);
  assert.deepEqual(VS.filterDiscovery(cos, { minConfidence: '45' }).map(c => c.name), ['C', 'A']);
  assert.deepEqual(VS.filterDiscovery(cos, { evidence: 'rumor' }).map(c => c.name), ['B']);
  assert.deepEqual(VS.filterDiscovery(cos, { q: 'bio' }).map(c => c.name), ['C']);
});


test('funding record groups keep superseded source history separate from current records', () => {
  const groups = VS.fundingRecordGroups({
    funding_rounds: [{ id: 11, amount: 12000000 }],
    funding_history: [{ id: 7, amount: 10000000, superseded_by_round_id: 11 }],
  });
  assert.deepEqual(groups.map(g => g.key), ['current', 'historical']);
  assert.deepEqual(groups.map(g => g.rounds.map(r => r.id)), [[11], [7]]);
  assert.match(groups[0].note, /does not mean the company is actively fundraising/);
  assert.match(groups[1].note, /Retained for traceability/);
});

test('funding record groups tolerate missing or empty history for older snapshots', () => {
  assert.deepEqual(VS.fundingRecordGroups({ funding_rounds: [{ id: 1 }] }).map(g => g.key), ['current']);
  assert.deepEqual(VS.fundingRecordGroups({ funding_rounds: [], funding_history: [] }), []);
  assert.deepEqual(VS.fundingRecordGroups(null), []);
});
