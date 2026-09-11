export type JsonRecord = Record<string, unknown>;

export type InventoryPayload = {
  format?: unknown;
  app?: unknown;
  exported_at?: unknown;
  tables?: Record<string, unknown[]>;
  photos?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SheetRow = {
  product_id: number;
  product: string;
  group_name: string;
  system_stock: number;
  counted: number | null;
  difference: number | null;
  post_count_delta: number;
  final_stock: number;
};

export type SheetMonth = {
  month: string;
  title: string;
  is_current: boolean;
  rows: SheetRow[];
};

const MONTH_NAMES = [
  "",
  "JANEIRO",
  "FEVEREIRO",
  "MARÇO",
  "ABRIL",
  "MAIO",
  "JUNHO",
  "JULHO",
  "AGOSTO",
  "SETEMBRO",
  "OUTUBRO",
  "NOVEMBRO",
  "DEZEMBRO",
];

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function rows(payload: InventoryPayload, table: string): JsonRecord[] {
  const value = payload.tables?.[table];
  return Array.isArray(value) ? value.map(record) : [];
}

function numberValue(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function integerValue(value: unknown): number {
  const result = numberValue(value, Number.NaN);
  return Number.isInteger(result) ? result : 0;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function datePart(value: unknown): string {
  return text(value).slice(0, 10);
}

function monthLastDay(month: string): string {
  const [year, monthNumber] = month.split("-").map(Number);
  const day = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  return `${year.toString().padStart(4, "0")}-${monthNumber.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

export function monthTitle(month: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(month);
  if (!match) throw new Error("Mês inválido.");
  const number = Number(match[2]);
  if (number < 1 || number > 12) throw new Error("Mês inválido.");
  return `${MONTH_NAMES[number]} ${match[1]}`;
}

function monthSequence(first: string, last: string): string[] {
  const result: string[] = [];
  let [year, month] = first.split("-").map(Number);
  const [lastYear, lastMonth] = last.split("-").map(Number);
  while ((year < lastYear || (year === lastYear && month <= lastMonth)) && result.length < 120) {
    result.push(`${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}`);
    month += 1;
    if (month === 13) {
      year += 1;
      month = 1;
    }
  }
  return result;
}

function productName(product: JsonRecord): string {
  return [product.group_name, product.name, product.variant]
    .map(text)
    .filter(Boolean)
    .join(" ")
    .toLocaleUpperCase("pt-BR");
}

function compareProducts(left: JsonRecord, right: JsonRecord): number {
  return productName(left).localeCompare(productName(right), "pt-BR", { sensitivity: "base" });
}

function stockAsOf(movements: JsonRecord[], productId: number, endDate: string): number {
  return movements.reduce((total, movement) => (
    integerValue(movement.product_id) === productId && datePart(movement.movement_date) <= endDate
      ? total + numberValue(movement.quantity)
      : total
  ), 0);
}

export function buildSheetSnapshot(payload: InventoryPayload, today: string) {
  const products = rows(payload, "products")
    .filter((product) => !(text(product.name).toLocaleLowerCase("pt-BR") === "teste" && !text(product.group_name)))
    .sort(compareProducts);
  const movements = rows(payload, "movements");
  const currentMonth = today.slice(0, 7);
  const datedValues = [
    ...products.map((item) => datePart(item.created_at).slice(0, 7)),
    ...movements.map((item) => datePart(item.movement_date).slice(0, 7)),
  ].filter((value) => /^\d{4}-\d{2}$/.test(value));
  const firstMonth = datedValues.sort()[0] ?? currentMonth;

  const months: SheetMonth[] = monthSequence(firstMonth, currentMonth).map((month) => {
    const endDate = month === currentMonth ? today : monthLastDay(month);
    const monthRows: SheetRow[] = [];
    for (const product of products) {
      const productId = integerValue(product.id);
      const existed = datePart(product.created_at) <= endDate || movements.some((movement) => (
        integerValue(movement.product_id) === productId && datePart(movement.movement_date) <= endDate
      ));
      if (!productId || !existed) continue;
      const finalStock = stockAsOf(movements, productId, endDate);
      // A aba mensal deve mostrar o estoque atual calculado pelos movimentos do app.
      // A CONTAGEM da planilha é local e independente do histórico de contagens do Supabase.
      const systemStock = finalStock;
      monthRows.push({
        product_id: productId,
        product: productName(product),
        group_name: text(product.group_name).toLocaleUpperCase("pt-BR"),
        system_stock: systemStock,
        counted: null,
        difference: null,
        post_count_delta: 0,
        final_stock: finalStock,
      });
    }
    return {
      month,
      title: monthTitle(month),
      is_current: month === currentMonth,
      rows: monthRows,
    };
  });

  const current = months.find((item) => item.is_current)?.rows.map((item) => ({
    product_id: item.product_id,
    product: item.product,
    group_name: item.group_name,
    stock: item.final_stock,
  })) ?? [];
  return { current, months };
}

export function todayInSaoPaulo(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
