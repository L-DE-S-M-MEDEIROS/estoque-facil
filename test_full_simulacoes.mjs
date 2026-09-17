import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./google_sheets/Simulacoes.gs', import.meta.url), 'utf8');
function setup() {
  const ctx = vm.createContext({});
  vm.runInContext(source, ctx);
  return ctx;
}
const plain = value => JSON.parse(JSON.stringify(value));
test('quatro canais, somente SP soma fábrica', () => {
  const ctx = setup();
  assert.deepEqual(plain(vm.runInContext('FULL_ABAS.map(c => c.nome)', ctx)), ['FULL SH', 'FULL ML', 'FULL SP', 'FULL AM']);
  assert.equal(ctx.fullFormula_({ fabrica: true }), '=RC[-3]-SUM(RC[-2])+SUM(RC[-1])');
  assert.equal(ctx.fullFormula_({ fabrica: false }), '=RC[-2]-SUM(RC[-1])');
});
test('preserva números, zeros e vazios por ID, não por posição', () => {
  const ctx = setup();
  assert.deepEqual(plain(ctx.fullManuais_([
    ['B', 9, 0, '', '', 2], ['A', 5, 3, 7, '', 1], ['TOTAL', 14, 3, 7, 21, ''],
  ], { fabrica: true })), { 1: [3, 7], 2: [0, ''] });
  assert.throws(() => ctx.fullManuais_([['A', 0, 2, '', 1], ['B', 0, 3, '', 1]], {}), /duplicado/);
});
test('recusa resposta inválida antes de tocar nas simulações', () => {
  const ctx = setup();
  assert.throws(() => ctx.fullValidarProdutos_(null), /inválida/);
  const row = { product_id: 1, product: 'A', stock: 0 };
  assert.throws(() => ctx.fullValidarProdutos_([row, row]), /duplicado/);
  assert.throws(() => ctx.fullValidarProdutos_([{ ...row, stock: NaN }]), /inválido/);
  assert.equal(ctx.fullValidarProdutos_([row]).length, 1);
});
test('mudança de estoque escreve somente B e preserva edição simultânea', () => {
  const ctx = setup();
  const writes = [];
  const existing = [['A', 20, 7, 13, 1], ['TOTAL', 20, 7, 13, '']];
  const sheet = {
    getName: () => 'FULL SH', getLastRow: () => 3, getSheetId: () => 15,
    getRange: (...range) => ({
      getValues: () => existing, getValue: () => 'ID DO PRODUTO',
      setValues: values => writes.push({ range, values: plain(values) }),
    }),
  };
  ctx.assinaturaDados_ = () => 'unchanged-layout';
  const props = { getProperty: () => 'unchanged-layout' };
  ctx.fullAtualizarAba_(sheet, [{ product_id: 1, product: 'A', stock: 24 }], {}, props, false);
  assert.deepEqual(writes, [{ range: [2, 2, 1, 1], values: [[24]] }]);
  assert.equal(existing[0][2], 7);
  writes.length = 0;
  ctx.fullAtualizarAba_(sheet, [{ product_id: 1, product: 'A', stock: 20 }], {}, props, false);
  assert.deepEqual(writes, []);
});
test('não permite writer na aba PEDIDOS e não modifica automação anterior', () => {
  const ctx = setup();
  assert.throws(() => ctx.fullAtualizarAba_({ getName: () => 'PEDIDOS' }, [], {}, {}, false), /fora do escopo/);
  assert.doesNotMatch(source, /escreverPedidos_|escreverEstoque_|escreverMes_|deleteTrigger|record_count/);
});
