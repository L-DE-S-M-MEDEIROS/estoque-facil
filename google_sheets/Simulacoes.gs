// Complemento do Código.gs. Não substitui nem grava a aba PEDIDOS.
const FULL_ABAS = Object.freeze([
  { nome: 'FULL SH', envio: 'ENVIO SH', fabrica: false },
  { nome: 'FULL ML', envio: 'ENVIO ML', fabrica: false },
  { nome: 'FULL SP', envio: 'ENVIO SP', fabrica: true },
  { nome: 'FULL AM', envio: 'ENVIO AM', fabrica: false },
]);

function instalarSimulacoes() {
  atualizarSimulacoes();
  const ss = SpreadsheetApp.openById(ESTOQUE_CONFIG.spreadsheetId);
  const triggers = ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'atualizarSimulacoes');
  if (!triggers.some(t => t.getEventType() === ScriptApp.EventType.CLOCK)) {
    ScriptApp.newTrigger('atualizarSimulacoes').timeBased().everyMinutes(1).create();
  }
  if (!triggers.some(t => t.getEventType() === ScriptApp.EventType.ON_OPEN)) {
    ScriptApp.newTrigger('atualizarSimulacoes').forSpreadsheet(ss).onOpen().create();
  }
  console.log('Simulações instaladas. Atualização automática; PEDIDOS não foi alterada.');
}

function fullManuaisPedidos_(ss) {
  const sheet = ss.getSheetByName(ESTOQUE_CONFIG.ordersSheet);
  if (!sheet || sheet.getLastRow() < 2) return '[]';
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const id = headers.indexOf('ID DO PRODUTO');
  const fields = ['FINALIZAÇÃO', 'PRODUÇÃO', 'CORTE', 'DIMINUINDO', 'NOVO PEDIDO'].map(name => headers.indexOf(name));
  return JSON.stringify(sheet.getRange(2, 1, sheet.getLastRow() - 1, headers.length).getValues()
    .filter(r => Number.isInteger(Number(r[id])) && Number(r[id]) > 0)
    .map(r => [r[id]].concat(fields.map(i => i < 0 ? '' : r[i])))
    .sort((a, b) => Number(a[0]) - Number(b[0])));
}

function atualizarSimulacoes() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) throw new Error('Outra sincronização em andamento. Tente novamente.');
  const props = PropertiesService.getDocumentProperties();
  try {
    const ss = SpreadsheetApp.openById(ESTOQUE_CONFIG.spreadsheetId);
    const pedidosAntes = fullManuaisPedidos_(ss);
    const snapshot = buscarSnapshotFirebase_();
    const rows = fullValidarProdutos_(snapshot.current);
    FULL_ABAS.forEach(function(config, index) {
      let sheet = ss.getSheetByName(config.nome);
      const created = !sheet;
      if (!sheet) sheet = ss.insertSheet(config.nome);
      if (sheet.getIndex() !== index + 3) {
        ss.setActiveSheet(sheet);
        ss.moveActiveSheet(index + 3);
      }
      fullAtualizarAba_(sheet, rows, config, props, created);
    });
    SpreadsheetApp.flush();
    if (pedidosAntes !== fullManuaisPedidos_(ss)) {
      throw new Error('Os campos de PEDIDOS mudaram durante a execução. Confira edições simultâneas.');
    }
    props.setProperty('FULL_ULTIMA_VERIFICACAO', new Date().toISOString());
    props.deleteProperty('FULL_ULTIMO_ERRO');
    console.log('4 abas FULL verificadas; ' + rows.length + ' produtos; campos de PEDIDOS preservados.');
  } catch (error) {
    props.setProperty('FULL_ULTIMO_ERRO', String(error.message || error));
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function fullValidarProdutos_(rows) {
  if (!Array.isArray(rows)) throw new Error('Resposta de estoque inválida. Simulações preservadas.');
  const ids = new Set();
  rows.forEach(function(row) {
    if (!Number.isInteger(row.product_id) || row.product_id <= 0 || ids.has(row.product_id)
        || typeof row.product !== 'string' || !Number.isFinite(row.stock)) {
      throw new Error('Produto inválido ou duplicado. Simulações preservadas.');
    }
    ids.add(row.product_id);
  });
  return rows;
}

function fullFormula_(config) {
  return config.fabrica ? '=RC[-3]-SUM(RC[-2])+SUM(RC[-1])' : '=RC[-2]-SUM(RC[-1])';
}

function fullManuais_(values, config) {
  const idIndex = config.fabrica ? 5 : 4;
  const result = {};
  values.forEach(function(row) {
    const id = Number(row[idIndex]);
    if (Number.isInteger(id) && id > 0) {
      if (Object.prototype.hasOwnProperty.call(result, id)) throw new Error('ID duplicado na simulação. Dados preservados.');
      result[id] = config.fabrica ? [row[2], row[3]] : [row[2]];
    }
  });
  return result;
}

function fullAtualizarAba_(sheet, rows, config, props, created) {
  if (!FULL_ABAS.some(c => c.nome === sheet.getName())) throw new Error('Aba fora do escopo FULL.');
  const totalCol = config.fabrica ? 5 : 4;
  const idCol = totalCol + 1;
  const existing = sheet.getLastRow() > 1
    ? sheet.getRange(2, 1, sheet.getLastRow() - 1, idCol).getValues() : [];
  if (!created && sheet.getLastRow() > 1 && sheet.getRange(1, idCol).getValue() !== 'ID DO PRODUTO') {
    throw new Error(config.nome + ' já possui dados em outro formato. Nenhum dado foi apagado.');
  }
  const manual = fullManuais_(existing, config);
  if (!rows.length && Object.keys(manual).length) throw new Error('Estoque vazio inesperado. Simulação preservada.');
  const signature = assinaturaDados_('FULL_LAYOUT_1', rows.map(r => [r.product_id, r.product, r.group_name]));
  const key = 'FULL_LAYOUT_' + sheet.getSheetId();
  const sameOrder = existing.slice(0, rows.length).every((r, i) => Number(r[idCol - 1]) === rows[i].product_id)
    && Object.keys(manual).length === rows.length;
  if (!created && sameOrder && props.getProperty(key) === signature) {
    // Atualizações normais nunca reescrevem as células editáveis.
    if (rows.some((r, i) => existing[i][1] !== r.stock)) {
      sheet.getRange(2, 2, rows.length, 1).setValues(rows.map(r => [r.stock]));
    }
    return;
  }
  if (sheet.getMaxRows() < rows.length + 5) sheet.insertRowsAfter(sheet.getMaxRows(), rows.length + 5 - sheet.getMaxRows());
  if (sheet.getMaxColumns() < idCol) sheet.insertColumnsAfter(sheet.getMaxColumns(), idCol - sheet.getMaxColumns());
  limparControlado_(sheet);
  sheet.setTabColor('#5D6DA8');
  sheet.setFrozenColumns(1);
  const headers = ['PRODUTO', 'ESTOQUE ATUAL', config.envio];
  if (config.fabrica) headers.push('VAI PELA FÁBRICA');
  headers.push('TOTAL', 'ID DO PRODUTO');
  cabecalho_(sheet, headers);
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, idCol).setValues(rows.map(function(row) {
      const inputs = manual[row.product_id] || (config.fabrica ? ['', ''] : ['']);
      return [row.product, row.stock].concat(inputs, ['', row.product_id]);
    }));
    sheet.getRange(2, totalCol, rows.length, 1).setFormulasR1C1(rows.map(() => [fullFormula_(config)]));
    estilizarCorpo_(sheet, rows.length, totalCol);
    estilizarProdutos_(sheet, rows.length);
    sheet.getRange(2, 2, rows.length, totalCol - 1).setNumberFormat('#,##0');
    sheet.getRange(2, 2, rows.length, 1).setBackground(ESTOQUE_TEMA.systemBase).setFontWeight('bold');
    sheet.getRange(2, 3, rows.length, totalCol - 3).setBackground(ESTOQUE_TEMA.input)
      .setDataValidation(SpreadsheetApp.newDataValidation().requireNumberGreaterThanOrEqualTo(0)
        .setAllowInvalid(false).setHelpText('Informe uma quantidade maior ou igual a zero.').build());
    sheet.getRange(2, totalCol, rows.length, 1).setBackground(ESTOQUE_TEMA.calculated).setFontWeight('bold');
    sheet.getRange(1, 1, rows.length + 1, idCol).createFilter();
  }
  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1).setValue('TOTAL');
  for (let col = 2; col <= totalCol; col += 1) {
    const letter = String.fromCharCode(64 + col);
    sheet.getRange(totalRow, col).setFormula(rows.length ? '=SUM(' + letter + '2:' + letter + (rows.length + 1) + ')' : '=0')
      .setNumberFormat('#,##0');
  }
  estilizarTotal_(sheet, totalRow, totalCol);
  escreverTotaisCategorias_(sheet, rows, totalRow, totalCol, totalCol);
  contornarGrupos_(sheet, rows, totalCol);
  sheet.setColumnWidth(1, 360);
  sheet.setColumnWidth(2, 160);
  sheet.setColumnWidth(3, 140);
  if (config.fabrica) sheet.setColumnWidth(4, 180);
  sheet.setColumnWidth(totalCol, 140);
  sheet.hideColumns(idCol);
  sheet.setConditionalFormatRules(rows.length ? regrasEstoque_([
    sheet.getRange(2, 2, rows.length, 1), sheet.getRange(2, totalCol, rows.length, 1),
  ]) : []);
  proteger_(sheet, rows.length ? [sheet.getRange(2, 3, rows.length, totalCol - 3)] : []);
  props.setProperty(key, signature);
}

function conferirSimulacoes() {
  const ss = SpreadsheetApp.openById(ESTOQUE_CONFIG.spreadsheetId);
  FULL_ABAS.forEach(function(config) {
    const sheet = ss.getSheetByName(config.nome);
    if (!sheet) throw new Error('Aba ausente: ' + config.nome);
    const totalCol = config.fabrica ? 5 : 4;
    const values = sheet.getDataRange().getDisplayValues();
    if (values.some(row => row.some(v => /^#(REF!|ERROR!|VALUE!|NAME\?|DIV\/0!|N\/A)/.test(v)))) {
      throw new Error('Erro de fórmula em ' + config.nome);
    }
    const protection = sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET)
      .find(p => String(p.getDescription()).indexOf(ESTOQUE_CONFIG.protectionPrefix) === 0);
    if (!protection || protection.isWarningOnly()) throw new Error('Proteção ausente em ' + config.nome);
    console.log(config.nome + ': posição ' + sheet.getIndex() + '; fórmula ' + sheet.getRange(2, totalCol).getFormula()
      + '; editáveis ' + protection.getUnprotectedRanges().map(r => r.getA1Notation()).join(', '));
  });
  console.log('Última verificação: ' + PropertiesService.getDocumentProperties().getProperty('FULL_ULTIMA_VERIFICACAO'));
}
