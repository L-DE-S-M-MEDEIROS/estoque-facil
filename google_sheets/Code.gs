const ESTOQUE_CONFIG = Object.freeze({
  firebaseDatabaseUrl: 'https://estoque-bolsas-baby-default-rtdb.firebaseio.com',
  firebaseWorkspace: 'bolsas-baby',
  spreadsheetId: '1eXMlyvFpO_-MkD8oaux1NrlupqR-ECNyEZS1XSgJIiY',
  currentSheet: 'ESTOQUE ATUAL',
  ordersSheet: 'PEDIDOS',
  protectionPrefix: 'Estoque Bolsas Baby - ',
  propertyPrefix: 'ESTOQUE_SYNC_',
  timeZone: 'America/Sao_Paulo',
  layoutVersion: 8,
});

const ESTOQUE_TEMA = Object.freeze({
  header: '#173F5F',
  headerBorder: '#FFFFFF',
  bodyText: '#20333E',
  productOdd: '#EAF3F7',
  productEven: '#DDECF3',
  systemBase: '#F2F8F2',
  input: '#FFF2CC',
  calculated: '#F4F7FA',
  total: '#D7E9E2',
  border: '#B8C9D3',
  negativeDark: '#5A0B1A',
  dangerFill: '#F4CCCC',
  dangerText: '#9C0006',
  warningFill: '#FFF2CC',
  warningText: '#7F6000',
  successFill: '#D9EAD3',
  successText: '#274E13',
  currentTab: '#173F5F',
  currentMonthTab: '#2E7D6E',
  historyTab: '#879AA5',
  ordersTab: '#C26A2B',
  ordersInput: '#FFF4E5',
  ordersTotal: '#E7F2EE',
  headingFont: 'Montserrat',
  bodyFont: 'Roboto',
});

function configurarAutomacao() {
  const spreadsheet = SpreadsheetApp.openById(ESTOQUE_CONFIG.spreadsheetId);
  const handlers = new Set(['atualizarEstoque', 'enviarContagem']);
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (handlers.has(trigger.getHandlerFunction())) ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('atualizarEstoque').timeBased().everyMinutes(1).create();
  ScriptApp.newTrigger('atualizarEstoque').forSpreadsheet(spreadsheet).onOpen().create();
  forcarAtualizacao();
}

function forcarAtualizacao() {
  return atualizarEstoque(true);
}

function atualizarEstoque(forcar) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return 0;
  const properties = PropertiesService.getDocumentProperties();
  try {
    const snapshot = buscarSnapshotFirebase_();
    const updatedSheets = escreverEstoque_(snapshot, forcar === true);
    if (updatedSheets > 0) SpreadsheetApp.flush();
    properties.setProperty(ESTOQUE_CONFIG.propertyPrefix + 'ULTIMA_VERIFICACAO', new Date().toISOString());
    properties.deleteProperty(ESTOQUE_CONFIG.propertyPrefix + 'ULTIMO_ERRO');
    return updatedSheets;
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    properties.setProperty(
      ESTOQUE_CONFIG.propertyPrefix + 'ULTIMO_ERRO',
      new Date().toISOString() + ' | ' + message.slice(0, 500)
    );
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function enviarContagem(event) {
  // Mantido apenas para remover gatilhos antigos. A CONTAGEM é local à planilha.
  return;
}

function buscarSnapshotFirebase_() {
  const url = ESTOQUE_CONFIG.firebaseDatabaseUrl + '/workspaces/'
    + encodeURIComponent(ESTOQUE_CONFIG.firebaseWorkspace) + '.json';
  const response = fetchComRetry_(url, {
    method: 'get',
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true,
  });
  let parsed;
  try {
    parsed = JSON.parse(response.getContentText() || '{}');
  } catch (error) {
    throw new Error('O Firebase respondeu em formato inválido.');
  }
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    throw new Error((parsed && parsed.error) || 'Não foi possível consultar o Firebase.');
  }
  if (!parsed || !parsed.payload || parsed.payload.format !== 1) {
    throw new Error('O estoque online ainda não possui uma cópia válida.');
  }
  const projection = montarSnapshotPlanilha_(parsed.payload, Utilities.formatDate(
    new Date(), ESTOQUE_CONFIG.timeZone, 'yyyy-MM-dd'
  ));
  return Object.assign({
    ok: true,
    revision: Number(parsed.revision || 1),
    updated_at: String(parsed.updated_at || parsed.payload.exported_at || ''),
  }, projection);
}

const NOMES_MESES_ = Object.freeze([
  '', 'JANEIRO', 'FEVEREIRO', 'MARÇO', 'ABRIL', 'MAIO', 'JUNHO',
  'JULHO', 'AGOSTO', 'SETEMBRO', 'OUTUBRO', 'NOVEMBRO', 'DEZEMBRO',
]);

function registroFirebase_(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function linhasFirebase_(payload, table) {
  const value = payload.tables && payload.tables[table];
  return Array.isArray(value) ? value.map(registroFirebase_) : [];
}

function numeroFirebase_(value, fallback) {
  const result = Number(value);
  return Number.isFinite(result) ? result : (fallback === undefined ? 0 : fallback);
}

function inteiroFirebase_(value) {
  const result = numeroFirebase_(value, NaN);
  return Number.isInteger(result) ? result : 0;
}

function textoFirebase_(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function dataFirebase_(value) {
  return textoFirebase_(value).slice(0, 10);
}

function ultimoDiaMesFirebase_(month) {
  const parts = month.split('-').map(Number);
  return Utilities.formatDate(new Date(Date.UTC(parts[0], parts[1], 0)), 'UTC', 'yyyy-MM-dd');
}

function tituloMesFirebase_(month) {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) throw new Error('Mês inválido.');
  return NOMES_MESES_[Number(match[2])] + ' ' + match[1];
}

function sequenciaMesesFirebase_(first, last) {
  const result = [];
  let parts = first.split('-').map(Number);
  const end = last.split('-').map(Number);
  while ((parts[0] < end[0] || (parts[0] === end[0] && parts[1] <= end[1])) && result.length < 120) {
    result.push(String(parts[0]).padStart(4, '0') + '-' + String(parts[1]).padStart(2, '0'));
    parts[1] += 1;
    if (parts[1] === 13) { parts[0] += 1; parts[1] = 1; }
  }
  return result;
}

function nomeProdutoFirebase_(product) {
  return ['group_name', 'name', 'variant'].map(function(key) {
    return textoFirebase_(product[key]);
  }).filter(Boolean).join(' ').toLocaleUpperCase('pt-BR');
}

function saldoAteFirebase_(movements, productId, endDate) {
  return movements.reduce(function(total, movement) {
    return inteiroFirebase_(movement.product_id) === productId && dataFirebase_(movement.movement_date) <= endDate
      ? total + numeroFirebase_(movement.quantity) : total;
  }, 0);
}

function montarSnapshotPlanilha_(payload, today) {
  const products = linhasFirebase_(payload, 'products').filter(function(product) {
    return !(textoFirebase_(product.name).toLocaleLowerCase('pt-BR') === 'teste' && !textoFirebase_(product.group_name));
  }).sort(function(left, right) {
    return nomeProdutoFirebase_(left).localeCompare(nomeProdutoFirebase_(right), 'pt-BR', { sensitivity: 'base' });
  });
  const movements = linhasFirebase_(payload, 'movements');
  const currentMonth = today.slice(0, 7);
  const datedValues = products.map(function(item) { return dataFirebase_(item.created_at).slice(0, 7); })
    .concat(movements.map(function(item) { return dataFirebase_(item.movement_date).slice(0, 7); }))
    .filter(function(value) { return /^\d{4}-\d{2}$/.test(value); }).sort();
  const firstMonth = datedValues.length ? datedValues[0] : currentMonth;
  const months = sequenciaMesesFirebase_(firstMonth, currentMonth).map(function(month) {
    const endDate = month === currentMonth ? today : ultimoDiaMesFirebase_(month);
    const monthRows = [];
    products.forEach(function(product) {
      const productId = inteiroFirebase_(product.id);
      const existed = dataFirebase_(product.created_at) <= endDate || movements.some(function(movement) {
        return inteiroFirebase_(movement.product_id) === productId && dataFirebase_(movement.movement_date) <= endDate;
      });
      if (!productId || !existed) return;
      const finalStock = saldoAteFirebase_(movements, productId, endDate);
      monthRows.push({
        product_id: productId,
        product: nomeProdutoFirebase_(product),
        group_name: textoFirebase_(product.group_name).toLocaleUpperCase('pt-BR'),
        system_stock: finalStock,
        counted: null,
        difference: null,
        post_count_delta: 0,
        final_stock: finalStock,
      });
    });
    return { month: month, title: tituloMesFirebase_(month), is_current: month === currentMonth, rows: monthRows };
  });
  const currentMonthData = months.find(function(item) { return item.is_current; });
  const current = currentMonthData ? currentMonthData.rows.map(function(item) {
    return { product_id: item.product_id, product: item.product, group_name: item.group_name, stock: item.final_stock };
  }) : [];
  return { current: current, months: months };
}

function fetchComRetry_(url, options) {
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const response = UrlFetchApp.fetch(url, options);
      const status = response.getResponseCode();
      if ((status === 429 || status >= 500) && attempt === 0) {
        Utilities.sleep(750);
        continue;
      }
      return response;
    } catch (error) {
      lastError = error;
      if (attempt === 0) {
        Utilities.sleep(750);
        continue;
      }
    }
  }
  const detail = lastError && lastError.message ? ' ' + lastError.message : '';
  throw new Error('Não foi possível acessar o serviço online.' + detail);
}

function escreverEstoque_(snapshot, forcar) {
  const spreadsheet = SpreadsheetApp.openById(ESTOQUE_CONFIG.spreadsheetId);
  const properties = PropertiesService.getDocumentProperties();
  let updatedSheets = 0;
  let current = spreadsheet.getSheetByName(ESTOQUE_CONFIG.currentSheet);
  let currentCreated = false;
  if (!current) {
    const sheets = spreadsheet.getSheets();
    const first = sheets[0];
    if (sheets.length === 1 && first.getLastRow() <= 1 && first.getLastColumn() <= 1 && first.getRange('A1').isBlank()) {
      first.setName(ESTOQUE_CONFIG.currentSheet);
      current = first;
    } else {
      current = spreadsheet.insertSheet(ESTOQUE_CONFIG.currentSheet, 0);
    }
    currentCreated = true;
  }
  if (current.getIndex() !== 1) {
    spreadsheet.setActiveSheet(current);
    spreadsheet.moveActiveSheet(1);
  }

  const currentRows = snapshot.current || [];
  let pedidos = spreadsheet.getSheetByName(ESTOQUE_CONFIG.ordersSheet);
  const pedidosCreated = !pedidos;
  if (!pedidos) pedidos = spreadsheet.insertSheet(ESTOQUE_CONFIG.ordersSheet);
  if (pedidos.getIndex() !== 2) {
    spreadsheet.setActiveSheet(pedidos);
    spreadsheet.moveActiveSheet(2);
  }

  const currentSignature = assinaturaDados_('ATUAL', currentRows);
  const currentProperty = ESTOQUE_CONFIG.propertyPrefix + 'ASSINATURA_ATUAL';
  if (forcar || currentCreated || properties.getProperty(currentProperty) !== currentSignature) {
    escreverAtual_(current, currentRows, snapshot.updated_at);
    properties.setProperty(currentProperty, currentSignature);
    updatedSheets += 1;
  }

  const pedidosSignature = assinaturaDados_('PEDIDOS', currentRows);
  const pedidosProperty = ESTOQUE_CONFIG.propertyPrefix + 'ASSINATURA_PEDIDOS';
  if (forcar || pedidosCreated || properties.getProperty(pedidosProperty) !== pedidosSignature) {
    escreverPedidosDiminuindo_(pedidos, currentRows, snapshot.updated_at);
    properties.setProperty(pedidosProperty, pedidosSignature);
    updatedSheets += 1;
  }

  const currentStockByProduct = {};
  currentRows.forEach(function(item) {
    currentStockByProduct[String(item.product_id)] = item.stock;
  });

  (snapshot.months || []).forEach(function(month) {
    let sheet = spreadsheet.getSheetByName(month.title);
    const created = !sheet;
    if (!sheet) sheet = spreadsheet.insertSheet(month.title);
    const rows = prepararLinhasMes_(month, currentStockByProduct);
    const signature = assinaturaDados_('MES_' + month.month, {
      title: month.title,
      isCurrent: month.is_current === true,
      rows: rows,
    });
    const property = ESTOQUE_CONFIG.propertyPrefix + 'ASSINATURA_MES_' + month.month;
    if (forcar || created || properties.getProperty(property) !== signature) {
      escreverMes_(sheet, month, rows, snapshot.updated_at);
      properties.setProperty(property, signature);
      updatedSheets += 1;
    }
  });
  return updatedSheets;
}

function prepararLinhasMes_(month, currentStockByProduct) {
  return (month.rows || []).map(function(item) {
    const productKey = String(item.product_id);
    const systemStock = month.is_current && Object.prototype.hasOwnProperty.call(currentStockByProduct, productKey)
      ? currentStockByProduct[productKey]
      : item.system_stock;
    return {
      product: item.product,
      group_name: item.group_name,
      systemStock: systemStock,
      productId: item.product_id,
      month: month.month,
    };
  });
}

function assinaturaDados_(type, value) {
  const source = JSON.stringify({
    layoutVersion: ESTOQUE_CONFIG.layoutVersion,
    type: type,
    value: value,
  });
  const digest = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    source,
    Utilities.Charset.UTF_8
  );
  return Utilities.base64EncodeWebSafe(digest).replace(/=+$/, '');
}

function limparControlado_(sheet) {
  sheet.getProtections(SpreadsheetApp.ProtectionType.SHEET).forEach(function(protection) {
    if (String(protection.getDescription() || '').indexOf(ESTOQUE_CONFIG.protectionPrefix) === 0) protection.remove();
  });
  if (sheet.getFilter()) sheet.getFilter().remove();
  sheet.clearConditionalFormatRules();
  const lastRow = Math.max(sheet.getLastRow(), 1);
  const lastColumn = Math.max(sheet.getLastColumn(), 1);
  sheet.getRange(1, 1, lastRow, lastColumn)
    .clear()
    .clearDataValidations()
    .clearNote();
  sheet.showColumns(1, Math.min(9, sheet.getMaxColumns()));
  sheet.setHiddenGridlines(true);
  sheet.setFrozenRows(1);
}

function cabecalho_(sheet, headers) {
  const range = sheet.getRange(1, 1, 1, headers.length);
  range.setValues([headers]);
  range.setBackground(ESTOQUE_TEMA.header).setFontColor('#FFFFFF').setFontWeight('bold');
  range.setFontFamily(ESTOQUE_TEMA.headingFont).setFontSize(10).setWrap(true);
  range.setHorizontalAlignment('center').setVerticalAlignment('middle');
  range.setBorder(
    true,
    true,
    true,
    true,
    true,
    true,
    ESTOQUE_TEMA.headerBorder,
    SpreadsheetApp.BorderStyle.SOLID
  );
  sheet.setRowHeight(1, 32);
}

function estilizarCorpo_(sheet, rowCount, visibleColumns) {
  if (!rowCount) return;
  const body = sheet.getRange(2, 1, rowCount, visibleColumns);
  body.setFontFamily(ESTOQUE_TEMA.bodyFont).setFontSize(10).setFontColor(ESTOQUE_TEMA.bodyText);
  body.setVerticalAlignment('middle').setWrap(false);
  body.setBorder(
    true,
    true,
    true,
    true,
    true,
    true,
    ESTOQUE_TEMA.border,
    SpreadsheetApp.BorderStyle.SOLID
  );
  sheet.setRowHeights(2, rowCount, 25);
  sheet.getRange(2, 1, rowCount, 1).setHorizontalAlignment('left');
  if (visibleColumns > 1) sheet.getRange(2, 2, rowCount, visibleColumns - 1).setHorizontalAlignment('right');
}

function estilizarProdutos_(sheet, rowCount) {
  if (!rowCount) return;
  const backgrounds = Array.from({ length: rowCount }, function(_, index) {
    return [index % 2 === 0 ? ESTOQUE_TEMA.productOdd : ESTOQUE_TEMA.productEven];
  });
  sheet.getRange(2, 1, rowCount, 1)
    .setBackgrounds(backgrounds)
    .setFontWeight('bold');
}

function contornarGrupos_(sheet, rows, columns) {
  let start = 0;
  while (start < rows.length) {
    // Use o grupo cadastrado, nunca deduza pelo nome/cor do produto.
    const group = rows[start].group_name;
    let end = start + 1;
    while (end < rows.length && rows[end].group_name === group) end += 1;
    if (typeof group === 'string' && group.trim()) {
      sheet.getRange(start + 2, 1, end - start, columns).setBorder(
        true, true, true, true, null, null,
        '#172B3A', SpreadsheetApp.BorderStyle.SOLID_MEDIUM
      );
    }
    start = end;
  }
}

function escreverTotaisCategorias_(sheet, rows, totalRow, columns, stockColumn) {
  // Limpe explicitamente o rodapé anterior, inclusive a linha de separação.
  sheet.getRange(totalRow + 1, 1, 3, columns)
    .clear().clearDataValidations().clearNote();
  const categoryRanges = { bolsas: [], roupas: [] };
  const letter = String.fromCharCode(64 + stockColumn);
  rows.forEach(function(item, index) {
    const group = String(item.group_name || '').trim().toLocaleUpperCase('pt-BR');
    const category = group === 'ROUPA MATERNIDADE' ? 'roupas' : 'bolsas';
    categoryRanges[category].push(letter + (index + 2));
  });
  ['bolsas', 'roupas'].forEach(function(category, index) {
    const row = totalRow + index + 2;
    sheet.getRange(row, 1).setValue(index === 0 ? 'BOLSAS' : 'ROUPAS');
    sheet.getRange(row, stockColumn)
      .setFormula(categoryRanges[category].length ? '=SUM(' + categoryRanges[category].join(';') + ')' : '=0')
      .setNumberFormat('#,##0').setHorizontalAlignment('right');
    estilizarTotal_(sheet, row, columns);
  });
}

function estilizarTotal_(sheet, row, columns) {
  const range = sheet.getRange(row, 1, 1, columns);
  range.setBackground(ESTOQUE_TEMA.total)
    .setFontFamily(ESTOQUE_TEMA.headingFont)
    .setFontSize(10)
    .setFontColor(ESTOQUE_TEMA.header)
    .setFontWeight('bold')
    .setVerticalAlignment('middle');
  range.setBorder(
    true,
    false,
    true,
    false,
    false,
    false,
    ESTOQUE_TEMA.header,
    SpreadsheetApp.BorderStyle.SOLID_MEDIUM
  );
  sheet.setRowHeight(row, 28);
}

function regrasEstoque_(ranges) {
  return [
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberLessThan(0)
      .setBackground(ESTOQUE_TEMA.negativeDark)
      .setFontColor('#FFFFFF')
      .setBold(true)
      .setRanges(ranges)
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberBetween(0, 15)
      .setBackground(ESTOQUE_TEMA.dangerFill)
      .setFontColor(ESTOQUE_TEMA.dangerText)
      .setBold(true)
      .setRanges(ranges)
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberBetween(16, 29)
      .setBackground(ESTOQUE_TEMA.warningFill)
      .setFontColor(ESTOQUE_TEMA.warningText)
      .setBold(true)
      .setRanges(ranges)
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberGreaterThanOrEqualTo(30)
      .setBackground(ESTOQUE_TEMA.successFill)
      .setFontColor(ESTOQUE_TEMA.successText)
      .setBold(true)
      .setRanges(ranges)
      .build(),
  ];
}

function regrasDiferenca_(range) {
  return [
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberLessThan(0)
      .setBackground(ESTOQUE_TEMA.dangerFill)
      .setFontColor(ESTOQUE_TEMA.dangerText)
      .setBold(true)
      .setRanges([range])
      .build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenNumberGreaterThan(0)
      .setBackground(ESTOQUE_TEMA.successFill)
      .setFontColor(ESTOQUE_TEMA.successText)
      .setBold(true)
      .setRanges([range])
      .build(),
  ];
}

function notaAtualizacao_(updatedAt) {
  let formatted = '';
  if (updatedAt) {
    const date = new Date(updatedAt);
    if (!Number.isNaN(date.getTime())) {
      formatted = Utilities.formatDate(date, ESTOQUE_CONFIG.timeZone, 'dd/MM/yyyy HH:mm');
    }
  }
  return formatted
    ? 'Sincronização automática. Última alteração recebida do aplicativo: ' + formatted + '.'
    : 'Sincronização automática com o estoque do aplicativo.';
}

function escreverAtual_(sheet, rows, updatedAt) {
  limparControlado_(sheet);
  sheet.setTabColor(ESTOQUE_TEMA.currentTab);
  cabecalho_(sheet, ['PRODUTO', 'ESTOQUE ATUAL']);
  sheet.getRange(1, 2).setNote(notaAtualizacao_(updatedAt));
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 2).setValues(rows.map(function(item) {
      return [item.product, item.stock];
    }));
    estilizarCorpo_(sheet, rows.length, 2);
    estilizarProdutos_(sheet, rows.length);
    sheet.getRange(2, 2, rows.length, 1)
      .setBackground(ESTOQUE_TEMA.systemBase)
      .setFontWeight('bold')
      .setNumberFormat('#,##0');
  }
  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1, 1, 2).setValues([['TOTAL', '']]);
  sheet.getRange(totalRow, 2)
    .setFormula(rows.length ? '=SUM(B2:B' + (rows.length + 1) + ')' : '=0')
    .setNumberFormat('#,##0')
    .setHorizontalAlignment('right');
  estilizarTotal_(sheet, totalRow, 2);
  contornarGrupos_(sheet, rows, 2);
  escreverTotaisCategorias_(sheet, rows, totalRow, 2, 2);
  sheet.setColumnWidth(1, 330);
  sheet.setColumnWidth(2, 155);
  if (rows.length) sheet.getRange(1, 1, rows.length + 1, 2).createFilter();
  const rules = rows.length ? regrasEstoque_([sheet.getRange(2, 2, rows.length, 1)]) : [];
  sheet.setConditionalFormatRules(rules);
  proteger_(sheet, []);
}

function escreverPedidos_(sheet, rows, updatedAt) {
  const manualByProduct = {};
  const existingRowCount = Math.max(sheet.getLastRow() - 1, 0);
  const legacy = sheet.getRange(1, 4).getValue() === 'FINALIZAÇÃO 2';
  if (legacy && existingRowCount > 0) {
    sheet.copyTo(sheet.getParent()).setName('PEDIDOS BACKUP ' + Date.now());
  }
  if (existingRowCount > 0 && sheet.getMaxColumns() >= 8) {
    const existingRows = sheet.getRange(2, 1, existingRowCount, legacy ? 9 : 8).getValues();
    existingRows.forEach(function(existing) {
      const productId = Number(existing[legacy ? 8 : 7]);
      if (Number.isInteger(productId) && productId > 0) {
        manualByProduct[productId] = {
          final1: legacy && (existing[2] !== '' || existing[3] !== '')
            ? Number(existing[2] || 0) + Number(existing[3] || 0) : existing[2],
          production: existing[legacy ? 4 : 3],
          cut: existing[legacy ? 5 : 4],
          newOrder: existing[legacy ? 7 : 6],
        };
      }
    });
  }

  limparControlado_(sheet);
  if (sheet.getMaxColumns() < 9) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), 9 - sheet.getMaxColumns());
  }
  sheet.setTabColor(ESTOQUE_TEMA.ordersTab);
  cabecalho_(sheet, [
    'PRODUTO',
    'ESTOQUE ATUAL',
    'FINALIZAÇÃO',
    'PRODUÇÃO',
    'CORTE',
    'TOTAL',
    'NOVO PEDIDO',
    'ID DO PRODUTO',
  ]);
  sheet.getRange(1, 2).setNote(notaAtualizacao_(updatedAt));
  sheet.getRange(1, 3).setNote('Quantidade em finalização. Editável somente na planilha.');
  sheet.getRange(1, 4).setNote('Quantidade em produção. Editável somente na planilha.');
  sheet.getRange(1, 5).setNote('Quantidade em corte. Editável somente na planilha.');
  sheet.getRange(1, 6).setNote('Soma do estoque atual, finalização, produção e corte.');
  sheet.getRange(1, 7).setNote('Campo livre editável. Não entra no Total.');

  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 8).setValues(rows.map(function(item) {
      const manual = Object.prototype.hasOwnProperty.call(manualByProduct, item.product_id)
        ? manualByProduct[item.product_id]
        : {};
      return [
        item.product,
        item.stock,
        Object.prototype.hasOwnProperty.call(manual, 'final1') ? manual.final1 : '',
        Object.prototype.hasOwnProperty.call(manual, 'production') ? manual.production : '',
        Object.prototype.hasOwnProperty.call(manual, 'cut') ? manual.cut : '',
        '',
        Object.prototype.hasOwnProperty.call(manual, 'newOrder') ? manual.newOrder : '',
        item.product_id,
      ];
    }));
    sheet.getRange(2, 6, rows.length, 1).setFormulasR1C1(rows.map(function() {
      return ['=SUM(RC[-4]:RC[-1])'];
    }));

    estilizarCorpo_(sheet, rows.length, 7);
    estilizarProdutos_(sheet, rows.length);
    const stockRange = sheet.getRange(2, 2, rows.length, 1);
    const inputRange = sheet.getRange(2, 3, rows.length, 3);
    const totalRange = sheet.getRange(2, 6, rows.length, 1);
    const newOrderRange = sheet.getRange(2, 7, rows.length, 1);
    stockRange.setBackground(ESTOQUE_TEMA.systemBase).setFontWeight('bold');
    inputRange.setBackground(ESTOQUE_TEMA.ordersInput).setFontWeight('bold');
    totalRange.setBackground(ESTOQUE_TEMA.ordersTotal).setFontWeight('bold');
    newOrderRange.setBackground(ESTOQUE_TEMA.ordersInput).setFontWeight('bold');
    sheet.getRange(2, 2, rows.length, 5).setNumberFormat('#,##0');
    inputRange.setDataValidation(
      SpreadsheetApp.newDataValidation()
        .requireNumberGreaterThanOrEqualTo(0)
        .setAllowInvalid(false)
        .setHelpText('Informe uma quantidade numérica maior ou igual a zero.')
        .build()
    );

  }

  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1, 1, 7).setValues([['TOTAL', '', '', '', '', '', '']]);
  if (rows.length) {
    const lastDataRow = rows.length + 1;
    sheet.getRange(totalRow, 2, 1, 5).setFormulas([[
      '=SUM(B2:B' + lastDataRow + ')',
      '=SUM(C2:C' + lastDataRow + ')',
      '=SUM(D2:D' + lastDataRow + ')',
      '=SUM(E2:E' + lastDataRow + ')',
      '=SUM(F2:F' + lastDataRow + ')',
    ]]);
  } else {
    sheet.getRange(totalRow, 2, 1, 5).setValues([[0, 0, 0, 0, 0]]);
  }
  sheet.getRange(totalRow, 2, 1, 5).setNumberFormat('#,##0').setHorizontalAlignment('right');
  estilizarTotal_(sheet, totalRow, 7);
  contornarGrupos_(sheet, rows, 7);
  escreverTotaisCategorias_(sheet, rows, totalRow, 7, 6);
  sheet.setColumnWidth(1, 330);
  sheet.setColumnWidth(2, 155);
  sheet.setColumnWidth(3, 130);
  sheet.setColumnWidth(4, 130);
  sheet.setColumnWidth(5, 115);
  sheet.setColumnWidth(6, 105);
  sheet.setColumnWidth(7, 130);
  sheet.hideColumns(8, 2);
  if (rows.length) sheet.getRange(1, 1, rows.length + 1, 9).createFilter();

  const rules = [];
  if (rows.length) {
    rules.push.apply(rules, regrasEstoque_([
      sheet.getRange(2, 2, rows.length, 1),
      sheet.getRange(2, 6, rows.length, 1),
    ]));
  }
  sheet.setConditionalFormatRules(rules);
  proteger_(sheet, rows.length ? [
    sheet.getRange(2, 3, rows.length, 3),
    sheet.getRange(2, 7, rows.length, 1),
  ] : []);
}

function escreverMes_(sheet, month, rows, updatedAt) {
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
  sheet.setTabColor(month.is_current ? ESTOQUE_TEMA.currentMonthTab : ESTOQUE_TEMA.historyTab);
  cabecalho_(sheet, [
    'PRODUTO',
    'ESTOQUE DO SISTEMA',
    'CONTAGEM',
    'DIFERENÇA',
    'ESTOQUE FINAL',
    'ID DO PRODUTO',
    'MOVIMENTOS APÓS CONTAGEM',
    'MÊS',
  ]);
  sheet.getRange(1, 2).setNote(notaAtualizacao_(updatedAt));
  sheet.getRange(1, 3).setNote('Digite a contagem física nesta coluna. Ela fica somente nesta planilha e não altera o aplicativo.');
  if (rows.length) {
    sheet.getRange(2, 1, rows.length, 8).setValues(rows.map(function(item) {
      const count = Object.prototype.hasOwnProperty.call(localCounts, item.productId) ? localCounts[item.productId] : '';
      return [item.product, item.systemStock, count, '', '', item.productId, '', item.month];
    }));
    // O Planilhas em português usa ponto e vírgula como separador de argumentos.
    sheet.getRange(2, 4, rows.length, 1).setFormulasR1C1(rows.map(function() {
      return ['=IF(RC[-1]="";"";RC[-1]-RC[-2])'];
    }));
    sheet.getRange(2, 5, rows.length, 1).setFormulasR1C1(rows.map(function() {
      return ['=IF(RC[-2]="";RC[-3];RC[-2])'];
    }));
    estilizarCorpo_(sheet, rows.length, 5);
    estilizarProdutos_(sheet, rows.length);
    const systemRange = sheet.getRange(2, 2, rows.length, 1);
    const countRange = sheet.getRange(2, 3, rows.length, 1);
    const differenceRange = sheet.getRange(2, 4, rows.length, 1);
    const finalRange = sheet.getRange(2, 5, rows.length, 1);
    systemRange.setBackground(ESTOQUE_TEMA.systemBase).setFontWeight('bold');
    countRange.setBackground(ESTOQUE_TEMA.input).setFontWeight('bold');
    differenceRange.setBackground(ESTOQUE_TEMA.calculated);
    finalRange.setBackground(ESTOQUE_TEMA.calculated).setFontWeight('bold');
    sheet.getRange(2, 2, rows.length, 4).setNumberFormat('#,##0');
    countRange.setDataValidation(
      SpreadsheetApp.newDataValidation()
        .requireNumberGreaterThanOrEqualTo(0)
        .setAllowInvalid(false)
        .setHelpText('Informe uma contagem numérica maior ou igual a zero.')
        .build()
    );
  }

  const totalRow = rows.length + 2;
  sheet.getRange(totalRow, 1, 1, 5).setValues([['TOTAL', '', '', '', '']]);
  sheet.getRange(totalRow, 5)
    .setFormula(rows.length ? '=SUM(E2:E' + (rows.length + 1) + ')' : '=0')
    .setNumberFormat('#,##0')
    .setHorizontalAlignment('right');
  estilizarTotal_(sheet, totalRow, 5);
  contornarGrupos_(sheet, rows, 5);
  escreverTotaisCategorias_(sheet, rows, totalRow, 5, 5);
  sheet.setColumnWidth(1, 330);
  sheet.setColumnWidth(2, 170);
  sheet.setColumnWidth(3, 120);
  sheet.setColumnWidth(4, 125);
  sheet.setColumnWidth(5, 140);
  sheet.hideColumns(6, 3);
  if (rows.length) sheet.getRange(1, 1, rows.length + 1, 5).createFilter();

  const rules = [];
  if (rows.length) {
    rules.push.apply(rules, regrasEstoque_([
      sheet.getRange(2, 2, rows.length, 1),
      sheet.getRange(2, 5, rows.length, 1),
    ]));
    rules.push.apply(rules, regrasDiferenca_(sheet.getRange(2, 4, rows.length, 1)));
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
