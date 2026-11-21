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
export const purchasingApi = {
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

export const api = {
  companies: () => request<Company[]>("/api/setup/companies/"),
  branches: (id: number) => request<Branch[]>("/api/setup/branches/?company=" + id + "&is_active=true"),
  fiscalYears: (id: number) => request<FiscalYear[]>("/api/setup/fiscal-years/?company=" + id),
  createJournal: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/", { method: "POST", body: JSON.stringify(payload) }); },
  postJournal: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/" + id + "/post/", { method: "POST" }); },
  accounts: (companyId: number) => request<Account[]>(`/api/accounting/accounts/?company=${companyId}&ordering=code`),
  journals: (companyId: number) => request<Journal[]>(`/api/accounting/journal-entries/?company=${companyId}&ordering=-entry_date`),
  trialBalance: (companyId: number) => request<TrialBalance>(`/api/accounting/journal-entries/trial-balance/?company=${companyId}`),
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
export type InventoryReconciliation = { company: number; account_id: number; account_code: string; account_name: string; stock_value: string; gl_balance: string; difference: string; is_reconciled: boolean; basis: string };
export type StockBalance = {
  id: number; product: number; product_code: string; product_name: string; warehouse: number;
  warehouse_name: string; location: number | null; location_name: string | null; expiry_date: string | null; quantity: string; average_cost: string; inventory_value: string; updated_at: string;
};
export const stockApi = {
  movements: (companyId: number) => request<StockMovement[]>(`/api/inventory/movements/?company=${companyId}&ordering=-movement_date`),
  balances: (companyId: number) => request<StockBalance[]>(`/api/inventory/balances/?warehouse__company=${companyId}&ordering=product__code`),
  createMovement: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>("/api/inventory/movements/", { method: "POST", body: JSON.stringify(payload) }); },
  postMovement: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>(`/api/inventory/movements/${id}/post/`, { method: "POST" }); },
  reconciliation: (companyId: number) => request<InventoryReconciliation>(`/api/inventory/balances/reconciliation/?company=${companyId}`),
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
export const salesApi = {
  deliveries: (companyId: number) => request<SalesDelivery[]>(`/api/sales-documents/deliveries/?company=${companyId}&ordering=-delivery_date`),
  invoices: (companyId: number) => request<SalesInvoice[]>(`/api/sales-documents/invoices/?company=${companyId}&ordering=-invoice_date`),
  receipts: (companyId: number) => request<CustomerReceipt[]>(`/api/sales-documents/receipts/?company=${companyId}&ordering=-receipt_date`),
  createDelivery: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>("/api/sales-documents/deliveries/", { method: "POST", body: JSON.stringify(payload) }); },
  postDelivery: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>(`/api/sales-documents/deliveries/${id}/post/`, { method: "POST" }); },
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>("/api/sales-documents/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>(`/api/sales-documents/invoices/${id}/post/`, { method: "POST" }); },
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>("/api/sales-documents/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>(`/api/sales-documents/receipts/${id}/post/`, { method: "POST" }); },
};
