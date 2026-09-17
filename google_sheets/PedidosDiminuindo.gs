// Usado pelo Código.gs no lugar de escreverPedidos_ na sincronização de PEDIDOS.
function escreverPedidosDiminuindo_(sheet, rows, updatedAt) {
  const headers = ['PRODUTO', 'ESTOQUE ATUAL', 'FINALIZAÇÃO', 'PRODUÇÃO', 'CORTE', 'DIMINUINDO', 'TOTAL', 'NOVO PEDIDO', 'ID DO PRODUTO'];
  const oldHeaders = sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), 9)).getValues()[0];
  const idIndex = oldHeaders.indexOf('ID DO PRODUTO');
  const manualNames = ['FINALIZAÇÃO', 'PRODUÇÃO', 'CORTE', 'DIMINUINDO', 'NOVO PEDIDO'];
  const manual = {};
  if (sheet.getLastRow() > 1) {
    if (idIndex < 0) throw new Error('ID DO PRODUTO ausente. PEDIDOS foi preservada.');
    sheet.getRange(2, 1, sheet.getLastRow() - 1, oldHeaders.length).getValues().forEach(function(r) {
      const id = Number(r[idIndex]);
      if (!Number.isInteger(id) || id <= 0) return;
      if (Object.prototype.hasOwnProperty.call(manual, id)) throw new Error('ID duplicado. PEDIDOS foi preservada.');
      manual[id] = manualNames.map(name => oldHeaders.indexOf(name) < 0 ? '' : r[oldHeaders.indexOf(name)]);
    });
  }
  if (!rows.length && Object.keys(manual).length) throw new Error('Estoque vazio inesperado. PEDIDOS foi preservada.');
  if (sheet.getMaxRows() < rows.length + 5) sheet.insertRowsAfter(sheet.getMaxRows(), rows.length + 5 - sheet.getMaxRows());
  if (sheet.getMaxColumns() < 9) sheet.insertColumnsAfter(sheet.getMaxColumns(), 9 - sheet.getMaxColumns());
  limparControlado_(sheet);
  sheet.setTabColor(ESTOQUE_TEMA.ordersTab);
  cabecalho_(sheet, headers);
  sheet.getRange(1, 2).setNote(notaAtualizacao_(updatedAt));
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 9).setValues(rows.map(function(r) {
      const m = manual[r.product_id] || ['', '', '', '', ''];
      return [r.product, r.stock, m[0], m[1], m[2], m[3], '', m[4], r.product_id];
    }));
    sheet.getRange(2, 7, rows.length, 1).setFormulasR1C1(rows.map(() => ['=SUM(RC[-5]:RC[-1])']));
    estilizarCorpo_(sheet, rows.length, 8);
    estilizarProdutos_(sheet, rows.length);
    sheet.getRange(2, 2, rows.length, 6).setNumberFormat('#,##0');
    sheet.getRange(2, 2, rows.length, 1).setBackground(ESTOQUE_TEMA.systemBase).setFontWeight('bold');
    sheet.getRange(2, 3, rows.length, 4).setBackground(ESTOQUE_TEMA.ordersInput).setFontWeight('bold')
      .setDataValidation(SpreadsheetApp.newDataValidation().requireNumberGreaterThanOrEqualTo(0).setAllowInvalid(false).build());
    sheet.getRange(2, 7, rows.length, 1).setBackground(ESTOQUE_TEMA.ordersTotal).setFontWeight('bold');
    sheet.getRange(2, 8, rows.length, 1).setBackground(ESTOQUE_TEMA.ordersInput).setFontWeight('bold');
    sheet.getRange(1, 1, rows.length + 1, 9).createFilter();
  }
  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1).setValue('TOTAL');
  // Soma também a coluna manual NOVO PEDIDO no rodapé.
  for (let col = 2; col <= 8; col++) {
    const letter = String.fromCharCode(64 + col);
    sheet.getRange(totalRow, col).setFormula(rows.length ? '=SUM(' + letter + '2:' + letter + (rows.length + 1) + ')' : '=0')
      .setNumberFormat('#,##0').setHorizontalAlignment('right');
  }
  estilizarTotal_(sheet, totalRow, 8);
  contornarGrupos_(sheet, rows, 8);
  escreverTotaisCategorias_(sheet, rows, totalRow, 8, 7);
  [330, 155, 130, 130, 115, 145, 105, 130].forEach((width, index) => sheet.setColumnWidth(index + 1, width));
  sheet.hideColumns(9);
  sheet.setConditionalFormatRules(rows.length ? regrasEstoque_([sheet.getRange(2, 2, rows.length, 1), sheet.getRange(2, 7, rows.length, 1)]) : []);
  proteger_(sheet, rows.length ? [sheet.getRange(2, 3, rows.length, 4), sheet.getRange(2, 8, rows.length, 1)] : []);
}

function aplicarDiminuindo() {
  PropertiesService.getDocumentProperties().deleteProperty(ESTOQUE_CONFIG.propertyPrefix + 'ASSINATURA_PEDIDOS');
  atualizarEstoque();
}
