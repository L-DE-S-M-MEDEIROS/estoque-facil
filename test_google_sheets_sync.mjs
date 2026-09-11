import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  buildSheetSnapshot,
  monthTitle,
  todayInSaoPaulo,
} from './supabase/functions/google-sheets-sync/logic.ts';

const appsScriptSource = readFileSync(new URL('./google_sheets/Code.gs', import.meta.url), 'utf8');
const appsScriptManifest = JSON.parse(
  readFileSync(new URL('./google_sheets/appsscript.json', import.meta.url), 'utf8'),
);

function payload() {
  return {
    format: 1,
    app: 'Estoque Bolsas Baby',
    tables: {
      operation_types: [{ id: 4, name: 'Inventário', effect: 'set', legacy_type: 'inventario', active: 1, protected: 1 }],
      products: [{ id: 7, group_name: 'Casinha', name: 'Azul', variant: 'Bebê', created_at: '2026-08-20T10:00:00' }],
      movement_batches: [
        { id: 1, operation_id: 1, movement_date: '2026-08-26', reason: 'Entrada', performed_by: 'Teste', created_at: '2026-08-26T10:00:00' },
      ],
      movements: [
        { id: 1, product_id: 7, type: 'entrada', quantity: 12, resulting_stock: 12, informed_quantity: null, movement_date: '2026-08-26', reason: 'Entrada', checked_by: 'Teste', created_at: '2026-08-26T10:00:00-0000', operation_id: 1, batch_id: 1 },
      ],
      monthly_stock_counts: [],
    },
    photos: {},
  };
}

test('monta estoque atual, nome combinado e abas mensais', () => {
  const result = buildSheetSnapshot(payload(), '2026-09-10');
  assert.equal(result.current.length, 1);
  assert.deepEqual(result.current[0], { product_id: 7, product: 'CASINHA AZUL BEBÊ', group_name: 'CASINHA', stock: 12 });
  assert.deepEqual(result.months.map((item) => item.title), ['AGOSTO 2026', 'SETEMBRO 2026']);
  assert.equal(result.months[1].rows[0].system_stock, 12);
  assert.equal(monthTitle('2026-09'), 'SETEMBRO 2026');
});

test('ignora contagens do aplicativo e entrega somente o estoque do sistema', () => {
  const source = payload();
  source.tables.monthly_stock_counts.push({
    product_id: 7,
    count_month: '2026-09',
    system_stock: 99,
    counted_quantity: 4,
    difference: -95,
  });
  const result = buildSheetSnapshot(source, '2026-09-10');
  const row = result.months.find((item) => item.month === '2026-09').rows[0];
  assert.equal(row.system_stock, 12);
  assert.equal(row.counted, null);
  assert.equal(row.difference, null);
  assert.equal(row.final_stock, 12);
});

test('produto TESTE sem grupo fica fora de todas as abas, sem ocultar produtos reais', () => {
  const source = payload();
  source.tables.products.push({ id: 78, name: ' TESTE ', group_name: '', created_at: '2026-08-20' });
  source.tables.products.push({ id: 79, name: 'TESTE', group_name: 'Casinha', created_at: '2026-08-20' });
  const result = buildSheetSnapshot(source, '2026-09-11');
  assert.deepEqual(result.current.map(row => row.product_id), [7, 79]);
  assert.ok(result.months.every(month => month.rows.every(row => row.product_id !== 78)));
});

test('usa o calendário de São Paulo perto da virada UTC', () => {
  assert.equal(todayInSaoPaulo(new Date('2026-09-11T01:30:00.000Z')), '2026-09-10');
});

test('Apps Script sincroniza sozinho e abre a planilha pelo ID', () => {
  assert.match(appsScriptSource, /SpreadsheetApp\.openById\(ESTOQUE_CONFIG\.spreadsheetId\)/);
  assert.match(appsScriptSource, /timeBased\(\)\.everyMinutes\(1\)\.create\(\)/);
  assert.match(appsScriptSource, /forSpreadsheet\(spreadsheet\)\.onOpen\(\)\.create\(\)/);
  assert.ok(appsScriptManifest.oauthScopes.includes('https://www.googleapis.com/auth/spreadsheets'));
});

test('Apps Script evita redesenhar abas sem alteração de dados', () => {
  assert.match(appsScriptSource, /layoutVersion:\s*8/);
  assert.match(appsScriptSource, /DigestAlgorithm\.SHA_256/);
  assert.match(appsScriptSource, /ASSINATURA_ATUAL/);
  assert.match(appsScriptSource, /ASSINATURA_MES_/);
});

test('Apps Script cria a aba PEDIDOS em segundo lugar e preserva os campos locais', () => {
  assert.match(appsScriptSource, /ordersSheet:\s*'PEDIDOS'/);
  assert.match(appsScriptSource, /moveActiveSheet\(2\)/);
  assert.match(appsScriptSource, /ASSINATURA_PEDIDOS/);
  assert.match(appsScriptSource, /'FINALIZAÇÃO',/);
  assert.doesNotMatch(appsScriptSource, /'FINALIZAÇÃO 1',/);
  assert.match(appsScriptSource, /NOVO PEDIDO/);
  assert.match(appsScriptSource, /=SUM\(RC\[-4\]:RC\[-1\]\)/);
  assert.match(appsScriptSource, /sheet\.getRange\(2, 3, rows\.length, 3\)/);
  assert.match(appsScriptSource, /sheet\.getRange\(2, 7, rows\.length, 1\)/);
});

test('Apps Script mantém contagem local e aplica o novo tema visual', () => {
  assert.match(appsScriptSource, /headingFont:\s*'Montserrat'/);
  assert.match(appsScriptSource, /bodyFont:\s*'Roboto'/);
  assert.match(appsScriptSource, /requireNumberGreaterThanOrEqualTo\(0\)/);
  assert.match(appsScriptSource, /CONTAGEM_LOCAL_/);
  assert.match(appsScriptSource, /'=IF\(RC\[-1\]=\"\";\"\";RC\[-1\]-RC\[-2\]\)'/);
  assert.doesNotMatch(appsScriptSource, /chamarSupabase_\(['"]record_count['"]/);
});
