export type Company = { id: number; code: string; name_ar: string; base_currency_code: string };
export type Branch = { id: number; company: number; code: string; name_ar: string; is_active: boolean };
export type FiscalYear = { id: number; company: number; name: string; start_date: string; end_date: string; status: string };
export type Account = {
  id: number; company: number; code: string; name_ar: string; parent: number | null;
  parent_code: string | null; nature: string; is_group: boolean; allow_posting: boolean; is_active: boolean;
};
export type JournalLine = { id: number; account: number; account_code: string; account_name: string; debit: string; credit: string; description: string };
export type TrialBalance = { company_id: number; debit_total: string; credit_total: string; difference: string; accounts: { account_id: number; code: string; name_ar: string; nature: string; debit_total: string; credit_total: string; net_balance: string }[] };
export type Journal = {
  id: number; company: number; branch: number; branch_code: string; fiscal_year: number;
  fiscal_year_name: string; entry_number: string; entry_date: string; description: string;
  source_type: string; status: string; lines: JournalLine[];
};
export type Customer = {
  id: number; company: number; code: string; customer_type: "company" | "individual";
  name_ar: string; name_en: string; tax_number: string; phone: string; mobile: string;
  email: string; address: string; credit_limit: string; payment_terms_days: number;
  notes: string; is_active: boolean; created_at: string; updated_at: string;
};
export type PurchaseOrder = {
  id: number; company: number; supplier: number; supplier_name: string; warehouse: number; warehouse_name: string;
  order_number: string; order_date: string; expected_date: string | null; status: "draft" | "confirmed" | "partially_received" | "received" | "cancelled";
  notes: string; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string;
    received_quantity: string; remaining_quantity: string; unit_price: string; tax_rate: string }[];
  created_at: string; confirmed_at: string | null;
};
export type PurchaseReceipt = { id: number; company: number; supplier: number; supplier_name: string; warehouse: number; warehouse_name: string; receipt_number: string; receipt_date: string; supplier_delivery_ref: string; notes: string; movement_status: "draft" | "posted"; movement_number: string; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string; unit_cost: string; cost_amount: string; expiry_date: string | null }[]; created_at: string };
export type PurchaseInvoice = { id: number; company: number; supplier: number; supplier_name: string; receipt: number; receipt_number: string; invoice_number: string; invoice_date: string; status: "draft" | "posted"; net_total: string; tax_total: string; grand_total: string; paid_total: string; balance_due: string; notes: string; journal_entry: number | null; journal_number: string | null; invoice_lines: { id: number; receipt_line: number; product_code: string; product_name: string; quantity: string; unit_price: string; tax_rate: string; net_amount: string; tax_amount: string; total_amount: string }[]; created_at: string; posted_at: string | null };
export type Supplier = { id: number; company: number; code: string; supplier_type: "company" | "individual"; name_ar: string; name_en: string; tax_number: string; phone: string; mobile: string; email: string; address: string; credit_limit: string; payment_terms_days: number; notes: string; is_active: boolean; created_at: string; updated_at: string };
function csrfToken(): string { const item = document.cookie.split("; ").find((part) => part.startsWith("csrftoken=")); return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : ""; }
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}), ...options.headers } });
  if (response.status === 403 || response.status === 401) {
    throw new Error("يلزم تسجيل الدخول ومنحك الصلاحية المناسبة. افتح لوحة الإدارة ثم أعد المحاولة.");
  }
  if (!response.ok) {
    let message = `تعذر تنفيذ الطلب (HTTP ${response.status}).`;
    try {
      const body = await response.json() as Record<string, unknown>;
      if (typeof body.detail === "string") message = body.detail;
      else {
        const parts = Object.entries(body).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join("، ") : typeof value === "string" ? value : JSON.stringify(value)}`);
        if (parts.length) message = parts.join(" | ");
      }
    } catch { /* keep the HTTP fallback message */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export type SupplierPayment = { id: number; company: number; supplier: number; supplier_name: string; invoice: number; invoice_number: string; payment_number: string; payment_date: string; method: "cash" | "bank"; amount: string; reference: string; status: "draft" | "posted"; journal_entry: number | null; journal_number: string | null; created_at: string; posted_at: string | null };
export type SupplierStatement = {
  company: number; supplier: number; supplier_name: string; date_from: string | null; date_to: string | null;
  opening_balance: string; closing_balance: string;
  rows: { date: string; document_type: "invoice" | "payment"; document_number: string; description: string; debit: string; credit: string; balance: string }[];
};
export type SupplierAging = {
  company: number; as_of: string;
  totals: { not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string };
  suppliers: { supplier: number; supplier_code: string; supplier_name: string; not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string;
    invoices: { invoice_number: string; invoice_date: string; due_date: string; overdue_days: number; bucket: string; balance_due: string }[] }[];
};
export const purchasingApi = {
  orders: (companyId: number) => request<PurchaseOrder[]>(`/api/purchasing/orders/?company=${companyId}&ordering=-order_date`),
  createOrder: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>("/api/purchasing/orders/", { method: "POST", body: JSON.stringify(payload) }); },
  confirmOrder: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>(`/api/purchasing/orders/${id}/confirm/`, { method: "POST" }); },

  supplierAging: (companyId: number, asOf = "") => request<SupplierAging>(`/api/purchasing/invoices/supplier-aging/?company=${companyId}${asOf ? `&as_of=${asOf}` : ""}`),
  supplierStatement: (companyId: number, supplierId: number, dateFrom = "", dateTo = "") => request<SupplierStatement>(`/api/purchasing/invoices/supplier-statement/?company=${companyId}&supplier=${supplierId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),

  receipts: (companyId: number) => request<PurchaseReceipt[]>(`/api/purchasing/receipts/?company=${companyId}&ordering=-receipt_date`),
  invoices: (companyId: number) => request<PurchaseInvoice[]>(`/api/purchasing/invoices/?company=${companyId}&ordering=-invoice_date`),
  payments: (companyId: number) => request<SupplierPayment[]>(`/api/purchasing/payments/?company=${companyId}&ordering=-payment_date`),
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>("/api/purchasing/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>(`/api/purchasing/receipts/${id}/post/`, { method: "POST" }); },
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>("/api/purchasing/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>(`/api/purchasing/invoices/${id}/post/`, { method: "POST" }); },
  createPayment: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>("/api/purchasing/payments/", { method: "POST", body: JSON.stringify(payload) }); },
  postPayment: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>(`/api/purchasing/payments/${id}/post/`, { method: "POST" }); },
};


export type TreasuryAccount = {
  id: number; company: number; branch: number | null; branch_name: string; account: number;
  account_code: string; account_name: string; kind: "cash" | "bank"; code: string; name_ar: string;
  bank_name: string; account_number: string; iban: string; currency_code: string;
  opening_balance: string; is_active: boolean;
};
export type TreasuryTransaction = {
  id: number; company: number; branch: number; fiscal_year: number; source_account: number; source_name: string;
  destination_account: number | null; destination_name: string; counter_account: number | null; counter_account_code: string;
  kind: "receipt" | "payment" | "transfer"; status: "draft" | "posted"; document_number: string;
  transaction_date: string; amount: string; description: string; reference: string; journal_entry: number | null;
  journal_number: string; cash_flow_category: "operating" | "investing" | "financing"; created_at: string; posted_at: string | null;
};
export type CashFlowReport = {
  company: number; date_from: string | null; date_to: string | null; net_cash_flow: string; note: string;
  categories: {category:"operating"|"investing"|"financing";label:string;inflows:string;outflows:string;net:string}[];
  rows: {id:number;date:string;document_number:string;description:string;branch:string;category:string;kind:string;amount:string;signed_amount:string;treasury_account:string;journal_entry:number|null}[];
};
export type IndirectCashFlowReport = {
  company: number; branch: number | null; date_from: string; date_to: string;
  net_profit: string; non_cash_adjustments: { depreciation: string; disposal_loss: string; disposal_gain: string; net: string };
  working_capital: { role: string; account_code: string; account_name: string; opening_balance: string; closing_balance: string; change: string; cash_flow_adjustment: string }[];
  missing_account_roles: string[]; operating_adjustments: string; operating_cash_flow: string;
  investing_cash_flow: string; financing_cash_flow: string; net_change_in_cash: string;
  ledger_cash_change: string; reconciliation_difference: string;
  investing_transactions: { id: number; transaction_date: string; document_number: string; description: string; kind: string; amount: string }[];
  financing_transactions: { id: number; transaction_date: string; document_number: string; description: string; kind: string; amount: string }[];
  note: string;
};
export const treasuryApi = {
  accounts: (companyId: number) => request<TreasuryAccount[]>(`/api/treasury/accounts/?company=${companyId}&ordering=kind,code`),
  transactions: (companyId: number) => request<TreasuryTransaction[]>(`/api/treasury/transactions/?company=${companyId}&ordering=-transaction_date`),
  createAccount: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryAccount>("/api/treasury/accounts/", {method:"POST", body:JSON.stringify(payload)}); },
  createTransaction: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>("/api/treasury/transactions/", {method:"POST", body:JSON.stringify(payload)}); },
  postTransaction: async (id: number) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>(`/api/treasury/transactions/${id}/post/`, {method:"POST"}); },
  indirectCashFlow: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => {
    const q = new URLSearchParams({company:String(companyId), date_from:dateFrom, date_to:dateTo});
    if (branch) q.set("branch", String(branch));
    return request<IndirectCashFlowReport>(`/api/accounting/journal-entries/indirect-cash-flow/?${q.toString()}`);
  },
  cashFlow: (companyId: number, dateFrom?: string, dateTo?: string, branch?: number) => {
    const q = new URLSearchParams({company:String(companyId)});
    if (dateFrom) q.set("date_from", dateFrom);
    if (dateTo) q.set("date_to", dateTo);
    if (branch) q.set("branch", String(branch));
    return request<CashFlowReport>(`/api/treasury/transactions/cash-flow/?${q.toString()}`);
  },
};

export type BalanceSheetReport = {
  company: number; as_of: string; assets: {account_id:number|null;code:string;name_ar:string;balance:string}[];
  liabilities: {account_id:number|null;code:string;name_ar:string;balance:string}[];
  equity: {account_id:number|null;code:string;name_ar:string;balance:string}[];
  total_assets: string; total_liabilities: string; total_equity: string;
  liabilities_and_equity: string; difference: string; current_year_profit: string;
};
export type IncomeStatementReport = {
  company: number; date_from: string; date_to: string;
  revenues: {account_id:number;code:string;name_ar:string;amount:string}[];
  cost_of_sales: {account_id:number;code:string;name_ar:string;amount:string}[];
  operating_and_other_expenses: {account_id:number;code:string;name_ar:string;amount:string}[];
  revenue_total: string; cost_of_sales_total: string; gross_profit: string; expenses_total: string; net_profit: string; note: string;
};
export type GeneralLedgerReport = {
  company:number; account:number; account_code:string; account_name:string; date_from:string; date_to:string;
  opening_balance:string; closing_balance:string;
  rows:{line_id:number;date:string;entry_id:number;entry_number:string;description:string;source_type:string;branch:string;debit:string;credit:string;running_balance:string}[];
};
export type EquityChangesReport = {
  company:number; date_from:string; date_to:string; opening_equity:string; equity_movements:string;
  period_net_profit:string; closing_equity:string;
  accounts:{account_id:number;code:string;name_ar:string;opening_balance:string;period_movement:string;closing_balance_before_profit:string}[];
  note:string;
};
export const api = {
  companies: () => request<Company[]>("/api/setup/companies/"),
  branches: (id: number) => request<Branch[]>("/api/setup/branches/?company=" + id + "&is_active=true"),
  fiscalYears: (id: number) => request<FiscalYear[]>("/api/setup/fiscal-years/?company=" + id),
  createJournal: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/", { method: "POST", body: JSON.stringify(payload) }); },
  postJournal: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/" + id + "/post/", { method: "POST" }); },
  accounts: (companyId: number) => request<Account[]>(`/api/accounting/accounts/?company=${companyId}&ordering=code`),
  journals: (companyId: number) => request<Journal[]>(`/api/accounting/journal-entries/?company=${companyId}&ordering=-entry_date`),
  journalDetail: (id: number) => request<Journal>(`/api/accounting/journal-entries/${id}/`),
  trialBalance: (companyId: number, dateFrom?: string, dateTo?: string, excludeClosing = false) => {
    const q = new URLSearchParams({company:String(companyId)});
    if (dateFrom) q.set("date_from", dateFrom);
    if (dateTo) q.set("date_to", dateTo);
    if (excludeClosing) q.set("exclude_closing", "true");
    return request<TrialBalance>(`/api/accounting/journal-entries/trial-balance/?${q.toString()}`);
  },
  balanceSheet: (companyId: number, asOf: string, branch?: number) => request<BalanceSheetReport>(`/api/accounting/journal-entries/balance-sheet/?company=${companyId}&as_of=${asOf}${branch ? `&branch=${branch}` : ""}`),
  incomeStatement: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => request<IncomeStatementReport>(`/api/accounting/journal-entries/income-statement/?company=${companyId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  generalLedger: (companyId: number, accountId: number, dateFrom: string, dateTo: string, branch?: number) => request<GeneralLedgerReport>(`/api/accounting/journal-entries/general-ledger/?company=${companyId}&account=${accountId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  changesInEquity: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => request<EquityChangesReport>(`/api/accounting/journal-entries/changes-in-equity/?company=${companyId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  customers: (companyId: number) => request<Customer[]>(`/api/sales/customers/?company=${companyId}&ordering=name_ar`),
  suppliers: (companyId: number) => request<Supplier[]>(`/api/purchases/suppliers/?company=${companyId}&ordering=name_ar`),
  createSupplier: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Supplier>("/api/purchases/suppliers/", { method: "POST", body: JSON.stringify(payload) }); },
  createCustomer: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Customer>("/api/sales/customers/", { method: "POST", body: JSON.stringify(payload) }); },
};


export type InventoryUnit = { id: number; company: number; code: string; name_ar: string; symbol: string; decimal_places: number; is_active: boolean };
export type InventoryCategory = { id: number; company: number; code: string; name_ar: string; parent: number | null; is_active: boolean };
export type Product = { id: number; company: number; category: number | null; category_name: string | null; unit: number; unit_name: string; code: string; barcode: string; name_ar: string; name_en: string; product_type: "goods" | "service"; purchase_price: string; sale_price: string; costing_method: string; min_stock: string; track_expiry: boolean; is_active: boolean };
export type Warehouse = { id: number; company: number; branch: number | null; branch_code: string; code: string; name_ar: string; address: string; is_active: boolean };
export const inventoryApi = {
  units: (companyId: number) => request<InventoryUnit[]>(`/api/inventory/units/?company=${companyId}&ordering=code`),
  categories: (companyId: number) => request<InventoryCategory[]>(`/api/inventory/categories/?company=${companyId}&ordering=code`),
  products: (companyId: number) => request<Product[]>(`/api/inventory/products/?company=${companyId}&ordering=code`),
  warehouses: (companyId: number) => request<Warehouse[]>(`/api/inventory/warehouses/?company=${companyId}&ordering=code`),
  locations: (companyId: number) => request<InventoryLocation[]>(`/api/inventory/locations/?warehouse__company=${companyId}&ordering=code`),
  createUnit: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryUnit>("/api/inventory/units/", { method: "POST", body: JSON.stringify(payload) }); },
  createCategory: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCategory>("/api/inventory/categories/", { method: "POST", body: JSON.stringify(payload) }); },
  createProduct: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Product>("/api/inventory/products/", { method: "POST", body: JSON.stringify(payload) }); },
  createWarehouse: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Warehouse>("/api/inventory/warehouses/", { method: "POST", body: JSON.stringify(payload) }); },
  createLocation: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryLocation>("/api/inventory/locations/", { method: "POST", body: JSON.stringify(payload) }); },
  warehouseProductSettings: (companyId: number) => request<WarehouseProductSetting[]>(`/api/inventory/warehouse-product-settings/?warehouse__company=${companyId}&ordering=warehouse__code`),
  createWarehouseProductSetting: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<WarehouseProductSetting>("/api/inventory/warehouse-product-settings/", { method: "POST", body: JSON.stringify(payload) }); },
  updateWarehouseProductSetting: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<WarehouseProductSetting>(`/api/inventory/warehouse-product-settings/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
};

export type WarehouseProductSetting = { id: number; product: number; product_code: string; product_name: string; warehouse: number; warehouse_name: string; min_stock: string; is_active: boolean; updated_at: string };
export type InventoryLocation = { id: number; warehouse: number; warehouse_name: string; code: string; name_ar: string; location_type: "floor" | "area" | "rack" | "bin"; parent: number | null; is_active: boolean };


export type StockMovementLine = { id: number; product: number; product_code: string; product_name: string; source_location: number | null; source_location_name: string | null; destination_location: number | null; destination_location_name: string | null; expiry_date: string | null; quantity: string; unit_cost: string; cost_amount: string; adjustment_direction: "in" | "out" | "" };
export type StockMovement = {
  id: number; company: number; movement_number: string; movement_type: "receipt" | "issue" | "transfer" | "adjustment";
  movement_date: string; source_warehouse: number | null; source_warehouse_name: string | null;
  destination_warehouse: number | null; destination_warehouse_name: string | null;
  status: "draft" | "posted"; notes: string; lines: StockMovementLine[]; journal_entries: { id: number; entry_number: string; branch_code: string; status: string }[]; created_at: string; posted_at: string | null;
};
export type StockCard = { product: number; product_code: string; product_name: string; warehouse: number; warehouse_name: string; date_from: string | null; date_to: string | null; opening_quantity: string; opening_value: string; closing_quantity: string; closing_value: string; rows: { date: string; movement_number: string; movement_type: string; direction: "in" | "out"; quantity_in: string; quantity_out: string; unit_cost: string; value_in: string; value_out: string; balance_quantity: string; balance_value: string }[] };
export type InventoryReconciliation = { company: number; branch: number | null; branch_code: string; branch_name: string; scope_branch_ids: number[]; scope_note: string; account_id: number; account_code: string; account_name: string; stock_value: string; gl_balance: string; difference: string; is_reconciled: boolean; basis: string };
export type StockBalance = {
  id: number; product: number; product_code: string; product_name: string; warehouse: number;
  warehouse_name: string; location: number | null; location_name: string | null; expiry_date: string | null; quantity: string; average_cost: string; inventory_value: string; updated_at: string;
};
export const stockApi = {
  movements: (companyId: number) => request<StockMovement[]>(`/api/inventory/movements/?company=${companyId}&ordering=-movement_date`),
  balances: (companyId: number) => request<StockBalance[]>(`/api/inventory/balances/?warehouse__company=${companyId}&ordering=product__code`),
  createMovement: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>("/api/inventory/movements/", { method: "POST", body: JSON.stringify(payload) }); },
  postMovement: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>(`/api/inventory/movements/${id}/post/`, { method: "POST" }); },
  reconciliation: (companyId: number, branchId?: number) => request<InventoryReconciliation>(`/api/inventory/balances/reconciliation/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  stockCard: (productId: number, warehouseId: number, dateFrom: string, dateTo: string) => request<StockCard>(`/api/inventory/movements/stock-card/?product=${productId}&warehouse=${warehouseId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),
};


export type AccountRole = { id: number; company: number; role: string; account: number; account_code: string };
export const accountRoleApi = {
  list: (companyId: number) => request<AccountRole[]>(`/api/accounting/account-roles/?company=${companyId}`),
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>("/api/accounting/account-roles/", { method: "POST", body: JSON.stringify(payload) }); },
  update: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>(`/api/accounting/account-roles/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
};


export type SalesDelivery = { id: number; company: number; customer: number; customer_name: string; warehouse: number; warehouse_name: string; delivery_number: string; delivery_date: string; customer_reference: string; notes: string; movement_status: "draft" | "posted"; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string; expiry_date: string | null }[]; created_at: string };
export type SalesInvoice = { id: number; company: number; customer: number; customer_name: string; delivery: number; delivery_number: string; invoice_number: string; invoice_date: string; status: "draft" | "posted"; net_total: string; tax_total: string; grand_total: string; paid_total: string; balance_due: string; notes: string; journal_entry: number | null; journal_number: string | null; invoice_lines: { id: number; delivery_line: number; product_code: string; product_name: string; quantity: string; unit_price: string; tax_rate: string; net_amount: string; tax_amount: string; total_amount: string }[]; created_at: string; posted_at: string | null };
export type CustomerReceipt = { id: number; company: number; customer: number; customer_name: string; invoice: number; invoice_number: string; receipt_number: string; receipt_date: string; method: "cash" | "bank"; amount: string; reference: string; status: "draft" | "posted"; journal_entry: number | null; journal_number: string | null; created_at: string; posted_at: string | null };
export type CustomerAging = {
  company: number; as_of: string; customer_filter: number | null; branch_filter: number | null;
  totals: { not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string };
  customers: { customer: number; customer_code: string; customer_name: string; not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string;
    invoices: { invoice: number; invoice_number: string; invoice_date: string; due_date: string; overdue_days: number; bucket: string; grand_total: string; paid_as_of: string; balance_due: string }[] }[];
};
export const salesApi = {
  deliveries: (companyId: number) => request<SalesDelivery[]>(`/api/sales-documents/deliveries/?company=${companyId}&ordering=-delivery_date`),
  invoices: (companyId: number) => request<SalesInvoice[]>(`/api/sales-documents/invoices/?company=${companyId}&ordering=-invoice_date`),
  receipts: (companyId: number) => request<CustomerReceipt[]>(`/api/sales-documents/receipts/?company=${companyId}&ordering=-receipt_date`),
  customerAging: (companyId: number, asOf = "", customerId?: number, branchId?: number) => request<CustomerAging>(`/api/sales-documents/invoices/customer-aging/?company=${companyId}${asOf ? `&as_of=${asOf}` : ""}${customerId ? `&customer=${customerId}` : ""}${branchId ? `&branch=${branchId}` : ""}`),
  createDelivery: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>("/api/sales-documents/deliveries/", { method: "POST", body: JSON.stringify(payload) }); },
  postDelivery: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>(`/api/sales-documents/deliveries/${id}/post/`, { method: "POST" }); },
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>("/api/sales-documents/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>(`/api/sales-documents/invoices/${id}/post/`, { method: "POST" }); },
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>("/api/sales-documents/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>(`/api/sales-documents/receipts/${id}/post/`, { method: "POST" }); },
};


export type BankReconciliation = { id: number; company: number; bank_account: number; bank_name: string; bank_code: string; statement_number: string; statement_date: string; opening_balance: string; closing_balance: string; status: "draft" | "reconciled"; line_count: number; created_at: string; reconciled_at: string | null };
export type BankStatementLine = { id: number; reconciliation: number; line_number: number; transaction_date: string; description: string; reference: string; amount: string; matched_transaction: number | null; matched_document: string; matched_amount: string | null; matched_at: string | null };
export type BankReconciliationSummary = { reconciliation: BankReconciliation; statement_calculated: string; statement_difference: string; book_balance: string; outstanding_total: string; expected_book_balance: string; unexplained_difference: string; matched_lines: number; unmatched_lines: number; lines: BankStatementLine[]; outstanding_transactions: { id: number; date: string; document_number: string; description: string; amount: string }[] };
export const bankReconciliationApi = {
  list: (companyId: number) => request<BankReconciliation[]>(`/api/treasury/reconciliations/?company=${companyId}&ordering=-statement_date`),
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>("/api/treasury/reconciliations/", { method: "POST", body: JSON.stringify(payload) }); },
  summary: (id: number) => request<BankReconciliationSummary>(`/api/treasury/reconciliations/${id}/summary/`),
  addLine: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/add_line/`, { method: "POST", body: JSON.stringify(payload) }); },
  match: async (id: number, lineId: number, transactionId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/match/`, { method: "POST", body: JSON.stringify({line_id:lineId,transaction_id:transactionId}) }); },
  unmatch: async (id: number, lineId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/unmatch/`, { method: "POST", body: JSON.stringify({line_id:lineId}) }); },
  reconcile: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>(`/api/treasury/reconciliations/${id}/reconcile/`, { method: "POST" }); },
};

export const importBankStatementCsv = async (id: number, file: File): Promise<{imported:number;message:string}> => {
  await request<{detail:string}>("/api/csrf/");
  const form = new FormData(); form.append("file", file);
  const response = await fetch(`/api/treasury/reconciliations/${id}/import_csv/`, {
    method:"POST", credentials:"same-origin", headers:{Accept:"application/json","X-CSRFToken":csrfToken()}, body:form
  });
  if (!response.ok) {
    let message = `تعذر استيراد الملف (HTTP ${response.status}).`;
    try { const body=await response.json() as Record<string,unknown>; message=Object.entries(body).map(([k,v])=>`${k}: ${Array.isArray(v)?v.join("، "):typeof v==="string"?v:JSON.stringify(v)}`).join(" | ")||message; } catch {}
    throw new Error(message);
  }
  return response.json() as Promise<{imported:number;message:string}>;
};


export type FixedAsset = {
  id: number; company: number; branch: number; code: string; name_ar: string; description: string;
  acquisition_date: string; depreciation_start_date: string; original_cost: string; salvage_value: string;
  useful_life_months: number; depreciation_method: "straight_line"; asset_account: number;
  accumulated_depreciation_account: number; depreciation_expense_account: number;
  acquisition_counter_account: number; disposal_gain_loss_account: number; disposal_loss_account: number | null; acquisition_journal: number | null; acquisition_treasury_transaction: number | null;
  status: "active" | "disposed"; disposed_at: string | null; disposal_proceeds: string | null; disposal_journal: number | null; disposal_treasury_transaction: number | null;
  accumulated_depreciation: string; net_book_value: string; created_at: string;
};
export type AssetSchedule = { asset_id: number; original_cost: string; accumulated_depreciation: string; net_book_value: string;
  rows: { id: number; asset: number; period_date: string; amount: string; is_partial_period: boolean; journal_entry: number; created_at: string }[] };
export type AssetRegisterReport = {
  company:number;as_of:string;
  totals:{original_cost:string;accumulated_depreciation:string;net_book_value_active:string;asset_count:number};
  rows:{asset_id:number;code:string;name_ar:string;branch_id:number;branch_name:string;acquisition_date:string;original_cost:string;salvage_value:string;useful_life_months:number;accumulated_depreciation:string;net_book_value:string;status:"active"|"disposed";disposed_at:string|null;acquisition_journal:number|null;disposal_journal:number|null}[];
};
export type AssetDepreciationReport = {company:number;date_from:string|null;date_to:string|null;total_depreciation:string;branch_totals:{branch_name:string;amount:string}[];rows:{asset_id:number;asset_code:string;asset_name:string;branch_name:string;period_date:string;amount:string;is_partial_period:boolean;journal_entry:number}[]};
export type AssetMovementReport = {company:number;date_from:string|null;date_to:string|null;rows:{asset_id:number;asset_code:string;asset_name:string;branch_name:string;date:string;event_type:string;event_label:string;amount:string;journal_entry:number|null;treasury_transaction:number|null}[]};
export const fixedAssetsApi = {
  list: (companyId: number) => request<FixedAsset[]>(`/api/assets/fixed-assets/?company=${companyId}`),
  register: (companyId:number, asOf:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),as_of:asOf}); if(branch) q.set("branch",String(branch));
    return request<AssetRegisterReport>(`/api/assets/fixed-assets/register/?${q.toString()}`);
  },
  depreciationReport: (companyId:number, dateFrom?:string, dateTo?:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId)}); if(dateFrom) q.set("date_from",dateFrom); if(dateTo) q.set("date_to",dateTo); if(branch) q.set("branch",String(branch));
    return request<AssetDepreciationReport>(`/api/assets/fixed-assets/depreciation-report/?${q.toString()}`);
  },
  movementReport: (companyId:number, dateFrom?:string, dateTo?:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId)}); if(dateFrom) q.set("date_from",dateFrom); if(dateTo) q.set("date_to",dateTo); if(branch) q.set("branch",String(branch));
    return request<AssetMovementReport>(`/api/assets/fixed-assets/movement-report/?${q.toString()}`);
  },
  create: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<FixedAsset>("/api/assets/fixed-assets/", {method:"POST",body:JSON.stringify(payload)}); },
  depreciate: async (id: number, periodDate: string, fiscalYear: number) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;depreciation_amount:string}>(`/api/assets/fixed-assets/${id}/depreciate/`, {method:"POST",body:JSON.stringify({period_date:periodDate,fiscal_year:fiscalYear})}); },
  schedule: (id: number) => request<AssetSchedule>(`/api/assets/fixed-assets/${id}/schedule/`),
  dispose: async (id: number, disposalDate: string, proceeds: string, treasuryAccount: number, fiscalYear: number) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;disposal_journal:number;accumulated_depreciation:string;net_book_value:string;proceeds:string;gain:string;loss:string}>(`/api/assets/fixed-assets/${id}/dispose/`, {method:"POST",body:JSON.stringify({disposal_date:disposalDate,proceeds,treasury_account:treasuryAccount,fiscal_year:fiscalYear})}); },
};
