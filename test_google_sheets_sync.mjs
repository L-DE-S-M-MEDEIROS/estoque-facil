import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildSheetSnapshot,
  monthTitle,
  todayInSaoPaulo,
} from './supabase/functions/google-sheets-sync/logic.ts';

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
  assert.deepEqual(result.current[0], { product_id: 7, product: 'CASINHA AZUL BEBÊ', stock: 12 });
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

test('usa o calendário de São Paulo perto da virada UTC', () => {
  assert.equal(todayInSaoPaulo(new Date('2026-09-11T01:30:00.000Z')), '2026-09-10');
});
