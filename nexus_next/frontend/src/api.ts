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
};

export type InventoryLocation = { id: number; warehouse: number; warehouse_name: string; code: string; name_ar: string; location_type: "floor" | "area" | "rack" | "bin"; parent: number | null; is_active: boolean };


export type StockMovementLine = { id: number; product: number; product_code: string; product_name: string; source_location: number | null; source_location_name: string | null; destination_location: number | null; destination_location_name: string | null; expiry_date: string | null; quantity: string; unit_cost: string; adjustment_direction: "in" | "out" | "" };
export type StockMovement = {
  id: number; company: number; movement_number: string; movement_type: "receipt" | "issue" | "transfer" | "adjustment";
  movement_date: string; source_warehouse: number | null; source_warehouse_name: string | null;
  destination_warehouse: number | null; destination_warehouse_name: string | null;
  status: "draft" | "posted"; notes: string; lines: StockMovementLine[]; created_at: string; posted_at: string | null;
};
export type StockBalance = {
  id: number; product: number; product_code: string; product_name: string; warehouse: number;
  warehouse_name: string; location: number | null; location_name: string | null; expiry_date: string | null; quantity: string; average_cost: string; inventory_value: string; updated_at: string;
};
export const stockApi = {
  movements: (companyId: number) => request<StockMovement[]>(`/api/inventory/movements/?company=${companyId}&ordering=-movement_date`),
  balances: (companyId: number) => request<StockBalance[]>(`/api/inventory/balances/?warehouse__company=${companyId}&ordering=product__code`),
  createMovement: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>("/api/inventory/movements/", { method: "POST", body: JSON.stringify(payload) }); },
  postMovement: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>(`/api/inventory/movements/${id}/post/`, { method: "POST" }); },
};
