import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('./google_sheets/Code.gs', import.meta.url), 'utf8');

function createHarness({ etags = ['"a"', '"a"'], fullEtags = etags, validationEtags = etags, payloads = [{}] } = {}) {
  const cacheValues = new Map();
  const calls = [];
  let fullIndex = 0;
  let validationIndex = 0;
  let currentDay = '2026-10-01';
  const blob = (value) => ({
    getBytes: () => Array.from(Buffer.from(String(value), 'utf8')),
    getDataAsString: () => String(value),
  });
  const Utilities = {
    DigestAlgorithm: { SHA_256: 'SHA_256' },
    Charset: { UTF_8: 'UTF_8' },
    formatDate: () => currentDay,
    computeDigest: (_algorithm, value) => Array.from(Buffer.from(String(value), 'utf8')),
    base64EncodeWebSafe: (bytes) => Buffer.from(bytes).toString('base64url'),
    base64Encode: (bytes) => Buffer.from(bytes).toString('utf8'),
    base64Decode: (value) => value,
    gzip: (value) => value,
    ungzip: (value) => value,
    newBlob: (value) => blob(value),
    sleep: () => {},
  };
  const cache = {
    get: (key) => cacheValues.get(key) || null,
    put: (key, value) => cacheValues.set(key, value),
    remove: (key) => cacheValues.delete(key),
  };
  const response = (status, body, etag) => ({
    getResponseCode: () => status,
    getContentText: () => body,
    getAllHeaders: () => etag ? { ETag: etag } : {},
  });
  const context = vm.createContext({
    console,
    Object, Array, Date, Number, String, RegExp, Math, JSON, Set,
    Utilities,
    CacheService: { getScriptCache: () => cache },
    ScriptApp: { getProjectTriggers: () => [], getOAuthToken: () => 'test-token', EventType: { CLOCK: 'CLOCK', ON_OPEN: 'ON_OPEN' } },
    UrlFetchApp: {
      fetch(url) {
        calls.push(url);
        const isValidation = url.includes('print=silent');
        if (isValidation) {
          const etag = validationEtags[Math.min(validationIndex++, validationEtags.length - 1)];
          return response(204, '', etag);
        }
        const payload = payloads[Math.min(fullIndex++, payloads.length - 1)];
        const snapshot = { format: 1, tables: {}, ...payload };
        const etag = fullEtags[Math.min(fullIndex - 1, fullEtags.length - 1)];
        return response(200, JSON.stringify({ payload: snapshot, revision: 1, updated_at: '2026-10-01T12:00:00Z' }), etag);
      },
    },
  });
  vm.runInContext(source, context);
  context.montarSnapshotPlanilha_ = (payload, today) => ({
    current: [{ product_id: 1, product: String(payload.tables.products?.[0]?.name || 'X'), stock: 1 }],
    months: [{ month: today.slice(0, 7), title: 'OUTUBRO 2026', rows: [] }],
  });
  return { context, calls, cacheValues, setDay: (value) => { currentDay = value; } };
}

test('cache da planilha baixa o snapshot completo uma vez e valida apenas o ETag depois', () => {
  const h = createHarness({ payloads: [{ tables: { products: [{ name: 'MARINHO' }] } }] });
  const first = h.context.buscarSnapshotFirebase_(false);
  const second = h.context.buscarSnapshotFirebase_(false);
  assert.equal(first.current[0].product, 'MARINHO');
  assert.equal(JSON.stringify(second), JSON.stringify(first));
  assert.equal(h.calls.filter((url) => !url.includes('print=silent')).length, 1);
  assert.equal(h.calls.filter((url) => url.includes('print=silent')).length, 1);
});

test('ETag alterado invalida o cache e baixa o snapshot novo; chamada forçada ignora cache', () => {
  const h = createHarness({
    fullEtags: ['"a"', '"b"'], validationEtags: ['"b"'],
    payloads: [{ tables: { products: [{ name: 'MARINHO' }] } }, { tables: { products: [{ name: 'VERDE' }] } }],
  });
  h.context.buscarSnapshotFirebase_(false);
  const changed = h.context.buscarSnapshotFirebase_(false);
  const forced = h.context.buscarSnapshotFirebase_(true);
  assert.equal(changed.current[0].product, 'VERDE');
  assert.equal(forced.current[0].product, 'VERDE');
  assert.equal(h.calls.filter((url) => !url.includes('print=silent')).length, 3);
});

test('cache ausente, corrompido ou sem ETag faz fallback seguro para o snapshot completo', () => {
  const h = createHarness({ fullEtags: ['', ''], validationEtags: ['', ''], payloads: [{ tables: { products: [{ name: 'AZUL' }] } }] });
  h.cacheValues.set('ESTOQUE_FIREBASE_invalid', 'conteúdo inválido');
  h.context.buscarSnapshotFirebase_(false);
  h.context.buscarSnapshotFirebase_(false);
  assert.equal(h.calls.filter((url) => !url.includes('print=silent')).length, 2);
  assert.equal(h.calls.filter((url) => url.includes('print=silent')).length, 0);
});

test('a virada do dia usa uma chave de cache diferente sem reaproveitar projeção antiga', () => {
  const h = createHarness({ fullEtags: ['"a"', '"b"'], payloads: [{ tables: { products: [{ name: 'DIA 1' }] } }, { tables: { products: [{ name: 'DIA 2' }] } }] });
  h.context.buscarSnapshotFirebase_(false);
  h.setDay('2026-10-02');
  const second = h.context.buscarSnapshotFirebase_(false);
  assert.equal(second.current[0].product, 'DIA 2');
  assert.equal(h.calls.filter((url) => !url.includes('print=silent')).length, 2);
});

test('a busca da planilha permanece autenticada e não envolve Supabase ou Firestore', () => {
  assert.match(source, /firebaseDatabaseUrl/);
  assert.match(source, /firebaseio\.com/);
  assert.doesNotMatch(source, /supabase|firestore/i);
  assert.match(source, /X-Firebase-ETag/);
  assert.match(source, /print=silent/);
});
