const ESTOQUE_CONFIG = Object.freeze({
  endpoint: 'https://raleparpityoscsykssk.supabase.co/functions/v1/google-sheets-sync',
  spreadsheetId: '1eXMlyvFpO_-MkD8oaux1NrlupqR-ECNyEZS1XSgJIiY',
  currentSheet: 'ESTOQUE ATUAL',
  protectionPrefix: 'Estoque Bolsas Baby - ',
});

function configurarAutomacao() {
  const handlers = new Set(['atualizarEstoque', 'enviarContagem']);
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (handlers.has(trigger.getHandlerFunction())) ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('atualizarEstoque').timeBased().everyMinutes(1).create();
  atualizarEstoque();
}

function atualizarEstoque() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return;
  try {
    escreverEstoque_(chamarSupabase_('snapshot', {}));
  } finally {
    lock.releaseLock();
  }
}

function enviarContagem(event) {
  // Mantido apenas para remover gatilhos antigos. A CONTAGEM é local à planilha.
  return;
}

function chamarSupabase_(action, fields) {
  const body = Object.assign({
    action: action,
    spreadsheet_id: ESTOQUE_CONFIG.spreadsheetId,
  }, fields || {});
  const response = UrlFetchApp.fetch(ESTOQUE_CONFIG.endpoint, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    payload: JSON.stringify(body),
    muteHttpExceptions: true,
  });
  let parsed;
  try {
    parsed = JSON.parse(response.getContentText() || '{}');
  } catch (error) {
    throw new Error('O serviço online respondeu em formato inválido.');
  }
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300 || parsed.ok !== true) {
    throw new Error(parsed.error || 'Não foi possível sincronizar o estoque.');
  }
  return parsed;
}

function escreverEstoque_(snapshot) {
  const spreadsheet = SpreadsheetApp.getActive();
  let current = spreadsheet.getSheetByName(ESTOQUE_CONFIG.currentSheet);
  if (!current) {
    const first = spreadsheet.getSheets()[0];
    if (spreadsheet.getSheets().length === 1 && first.getLastRow() <= 1 && first.getLastColumn() <= 1 && first.getRange('A1').isBlank()) {
      first.setName(ESTOQUE_CONFIG.currentSheet);
      current = first;
    } else {
      current = spreadsheet.insertSheet(ESTOQUE_CONFIG.currentSheet, 0);
    }
  }
  if (current.getIndex() !== 1) {
    spreadsheet.setActiveSheet(current);
    spreadsheet.moveActiveSheet(1);
  }
  escreverAtual_(current, snapshot.current || []);
  const currentStockByProduct = {};
  (snapshot.current || []).forEach(function(item) {
    currentStockByProduct[item.product_id] = item.stock;
  });

  (snapshot.months || []).forEach(function(month) {
    let sheet = spreadsheet.getSheetByName(month.title);
    if (!sheet) sheet = spreadsheet.insertSheet(month.title);
    escreverMes_(sheet, month, currentStockByProduct);
  });
}

function limparControlado_(sheet) {
  sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function(protection) {
    if (String(protection.getDescription() || '').indexOf(ESTOQUE_CONFIG.protectionPrefix) === 0) protection.remove();
  });
  if (sheet.getFilter()) sheet.getFilter().remove();
  sheet.clearConditionalFormatRules();
  const lastRow = Math.max(sheet.getLastRow(), 1);
  const lastColumn = Math.max(sheet.getLastColumn(), 1);
  sheet.getRange(1, 1, lastRow, lastColumn).clear({ contentsOnly: false });
  sheet.showColumns(1, Math.min(8, sheet.getMaxColumns()));
  sheet.setHiddenGridlines(true);
  sheet.setFrozenRows(1);
}

function cabecalho_(sheet, headers) {
  const range = sheet.getRange(1, 1, 1, headers.length);
  range.setValues([headers]);
  range.setBackground('#1F4E78').setFontColor('#FFFFFF').setFontWeight('bold');
  range.setHorizontalAlignment('center').setVerticalAlignment('middle');
  range.setBorder(true, true, true, true, true, true, '#7F9DB9', SpreadsheetApp.BorderStyle.SOLID);
  sheet.setRowHeight(1, 28);
}

function escreverAtual_(sheet, rows) {
  limparControlado_(sheet);
  cabecalho_(sheet, ['PRODUTO', 'ESTOQUE ATUAL']);
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 2).setValues(rows.map(function(item) {
      return [item.product, item.stock];
    }));
    sheet.getRange(2, 1, rows.length, 1).setFontWeight('bold').setBackground('#D9EAF2');
    sheet.getRange(2, 2, rows.length, 1).setBackground('#E2F0D9').setNumberFormat('#,##0');
    sheet.getRange(2, 1, rows.length, 2).setBorder(true, true, true, true, true, true, '#7F9DB9', SpreadsheetApp.BorderStyle.SOLID);
  }
  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1, 1, 2).setValues([['TOTAL', '']]);
  sheet.getRange(totalRow, 2).setFormula(rows.length ? '=SUM(B2:B' + (rows.length + 1) + ')' : '=0').setNumberFormat('#,##0');
  sheet.getRange(totalRow, 1, 1, 2).setBackground('#D9EAD3').setFontWeight('bold').setBorder(true, false, false, false, false, false, '#1F4E78', SpreadsheetApp.BorderStyle.SOLID_MEDIUM);
  sheet.setColumnWidth(1, 310);
  sheet.setColumnWidth(2, 145);
  if (rows.length) sheet.getRange(1, 1, rows.length + 1, 2).createFilter();
  const rules = [];
  if (rows.length) rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberLessThan(0).setBackground('#5A0B1A').setFontColor('#FFFFFF').setBold(true).setRanges([sheet.getRange(2, 2, rows.length, 1)]).build());
  sheet.setConditionalFormatRules(rules);
  proteger_(sheet, []);
}

function escreverMes_(sheet, month, currentStockByProduct) {
  const rows = month.rows || [];
  const countKey = 'CONTAGEM_LOCAL_' + month.month;
  const properties = PropertiesService.getDocumentProperties();
  const preserveLocalCounts = properties.getProperty(countKey) === '1';
  const localCounts = {};
  if (preserveLocalCounts && sheet.getLastRow() >= 2) {
    const existingRows = sheet.getRange(2, 1, sheet.getLastRow() - 1, Math.min(sheet.getLastColumn(), 6)).getValues();
    existingRows.forEach(function(existing) {
      const productId = Number(existing[5]);
      if (Number.isInteger(productId) && productId > 0 && existing[2] !== '' && existing[2] !== null) {
        localCounts[productId] = existing[2];
      }
    });
  }
  properties.setProperty(countKey, '1');
  limparControlado_(sheet);
  cabecalho_(sheet, ['PRODUTO', 'ESTOQUE DO SISTEMA', 'CONTAGEM', 'DIFERENÇA', 'ESTOQUE FINAL', 'ID DO PRODUTO', 'MOVIMENTOS APÓS CONTAGEM', 'MÊS']);
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 8).setValues(rows.map(function(item) {
      const systemStock = month.is_current && Object.prototype.hasOwnProperty.call(currentStockByProduct, item.product_id)
        ? currentStockByProduct[item.product_id]
        : item.system_stock;
      return [item.product, systemStock, Object.prototype.hasOwnProperty.call(localCounts, item.product_id) ? localCounts[item.product_id] : '', '', '', item.product_id, '', month.month];
    }));
    // O Planilhas em português usa ponto e vírgula como separador de argumentos.
    sheet.getRange(2, 4, rows.length, 1).setFormulasR1C1(rows.map(function() { return ['=IF(RC[-1]="";"";RC[-1]-RC[-2])']; }));
    sheet.getRange(2, 5, rows.length, 1).setFormulasR1C1(rows.map(function() { return ['=IF(RC[-2]="";RC[-3];RC[-2])']; }));
    sheet.getRange(2, 1, rows.length, 1).setFontWeight('bold').setBackground('#D9EAF2');
    sheet.getRange(2, 2, rows.length, 1).setBackground('#E2F0D9');
    sheet.getRange(2, 3, rows.length, 1).setBackground('#FFF2CC').setNote('Digite aqui a contagem física. Ela fica registrada somente nesta planilha.');
    sheet.getRange(2, 4, rows.length, 2).setBackground('#F4F7FA');
    sheet.getRange(2, 2, rows.length, 6).setNumberFormat('#,##0');
    sheet.getRange(2, 1, rows.length, 5).setBorder(true, true, true, true, true, true, '#7F9DB9', SpreadsheetApp.BorderStyle.SOLID);
  }
  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1, 1, 5).setValues([['TOTAL', '', '', '', '']]);
  sheet.getRange(totalRow, 5).setFormula(rows.length ? '=SUM(E2:E' + (rows.length + 1) + ')' : '=0').setNumberFormat('#,##0');
  sheet.getRange(totalRow, 1, 1, 5).setBackground('#D9EAD3').setFontWeight('bold').setBorder(true, false, false, false, false, false, '#1F4E78', SpreadsheetApp.BorderStyle.SOLID_MEDIUM);
  sheet.setColumnWidth(1, 310);
  sheet.setColumnWidth(2, 145);
  sheet.setColumnWidth(3, 105);
  sheet.setColumnWidth(4, 105);
  sheet.setColumnWidth(5, 125);
  sheet.hideColumns(6, 3);
  if (rows.length) sheet.getRange(1, 1, rows.length + 1, 5).createFilter();
  const rules = [];
  if (rows.length) {
    const difference = sheet.getRange(2, 4, rows.length, 1);
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberLessThan(0).setBackground('#F4CCCC').setFontColor('#9C0006').setRanges([difference]).build());
    rules.push(SpreadsheetApp.newConditionalFormatRule().whenNumberGreaterThan(0).setBackground('#D9EAD3').setFontColor('#274E13').setRanges([difference]).build());
  }
  sheet.setConditionalFormatRules(rules);
  proteger_(sheet, rows.length ? [sheet.getRange(2, 3, rows.length, 1)] : []);
}

function proteger_(sheet, unprotectedRanges) {
  const protection = sheet.protect().setDescription(ESTOQUE_CONFIG.protectionPrefix + sheet.getName());
  protection.setWarningOnly(false);
  protection.setUnprotectedRanges(unprotectedRanges);
  const me = Session.getEffectiveUser();
  protection.addEditor(me);
  protection.getEditors().forEach(function(editor) {
    if (editor.getEmail() !== me.getEmail()) protection.removeEditor(editor);
  });
  if (protection.canDomainEdit()) protection.setDomainEdit(false);
}
