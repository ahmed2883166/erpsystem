/*
 * طبقة الاتصال الخلفية للواجهة: تعريف أنواع البيانات ودوال طلب واجهات Django API.
 * توحّد مسارات الطلبات ومعالجة الاستجابات المستخدمة في شاشات النظام.
 */
export type Currency = { id: number; code: string; name_ar: string; name_en: string; symbol: string; decimal_places: number; is_active: boolean };
export type Company = { id: number; code: string; name_ar: string; name_en?: string; country: "EG" | "SA"; activity?: "commercial" | "service" | "medical" | "restaurants" | "industrial"; base_currency: number; base_currency_code: string; tax_registration_number?: string; address?: string; phone?: string; email?: string; is_active?: boolean; created_at?: string; updated_at?: string };
export type Branch = { id: number; company: number; company_code?: string; code: string; name_ar: string; address?: string; phone?: string; is_head_office?: boolean; is_active: boolean; created_at?: string };
export type CompanyMembership = { id: number; company: number; company_code: string; user: number; username: string; role: "admin" | "accountant" | "viewer"; all_branches: boolean; branches: number[]; is_active: boolean; created_at: string };
export type CompanyUserLookup = { id: number; username: string; display_name: string };
export type FiscalYear = { id: number; company: number; name: string; start_date: string; end_date: string; status: string };
export type ExchangeRate = { id: number; company: number; company_code: string; currency: number; currency_code: string; base_currency_code: string; rate_date: string; rate: string; source: string; notes: string; created_at: string };
export type Account = {
  id: number; company: number; code: string; name_ar: string; parent: number | null;
  parent_code: string | null; nature: string; is_group: boolean; allow_posting: boolean; is_active: boolean;
};
export type AccountTemplateInfo = { company: number; activity: Company["activity"]; activity_label: string; template_account_count: number; existing_account_count: number; has_accounts: boolean };
export type AccountImportPreview = {
  detail?: string;
  rows: { excel_row:number; code:string; name_ar:string; parent_code:string|null; nature:string|null; is_group:boolean; status:string; errors:string[]; warnings:string[] }[];
  errors: string[];
  summary: { total:number; ready_to_create:number; already_exists:number; blocked:number; warnings:number };
};
export type AccountImportResult = { detail:string; created_accounts:string[]; skipped_existing:number; summary:AccountImportPreview["summary"] };
export type AccountTemplateApplyResult = {
  detail: string; company: number; activity: string; created_accounts: string[]; reused_accounts: string[];
  role_assignments_created: string[]; role_conflicts: {role: string; existing_account_code: string; template_account_code: string}[];
  code_conflicts: {code: string; existing_name: string; template_name: string}[];
};
export type JournalLine = { id: number; account: number; account_code: string; account_name: string; debit: string; credit: string; description: string };
export type TrialBalance = { company_id: number; debit_total: string; credit_total: string; difference: string; accounts: { account_id: number; code: string; name_ar: string; nature: string; debit_total: string; credit_total: string; net_balance: string }[] };
export type ThemePreference = "light" | "dark" | "system";
export type SessionUser = { id: number; username: string; display_name: string; is_staff: boolean; is_superuser: boolean; is_platform_owner: boolean; theme_preference: ThemePreference };
export type SessionResponse = { authenticated: boolean; user?: SessionUser };

export const authApi = {
  current: () => request<SessionResponse>("/api/auth/session/"),
  updateThemePreference: async (theme_preference: ThemePreference) => {
    await request<{ detail: string }>("/api/csrf/");
    return request<{ theme_preference: ThemePreference }>("/api/auth/preferences/", {
      method: "PATCH", body: JSON.stringify({ theme_preference }),
    });
  },
  login: async (username: string, password: string) => {
    await request<{ detail: string }>("/api/csrf/");
    return request<SessionResponse>("/api/auth/login/", {
      method: "POST", body: JSON.stringify({ username, password }),
    });
  },
  logout: async () => {
    await request<{ detail: string }>("/api/csrf/");
    return request<SessionResponse>("/api/auth/logout/", { method: "POST", body: "{}" });
  },
};

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
// قراءة رمز CSRF من ملفات تعريف الارتباط لاستخدامه في الطلبات التي تغيّر البيانات.
function csrfToken(): string { const item = document.cookie.split("; ").find((part) => part.startsWith("csrftoken=")); return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : ""; }
// عميل HTTP موحد: يرسل الطلب بنفس جلسة الدخول، يضيف CSRF، ويحوّل أخطاء الخادم إلى رسائل مفهومة.
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}), ...options.headers } });
  if (!response.ok) {
    let message = response.status === 401 ? "انتهت جلسة الدخول. سجّل الدخول مرة أخرى." : response.status === 403 ? "ليس لديك صلاحية لتنفيذ هذا الطلب." : `تعذر تنفيذ الطلب (HTTP ${response.status}).`;
    try {
      const body = await response.json() as Record<string, unknown>;
      if (typeof body.detail === "string") message = body.detail;
      else {
        const parts = Object.entries(body).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join("، ") : typeof value === "string" ? value : JSON.stringify(value)}`);
        if (parts.length) message = parts.join(" | ");
      }
    } catch { /* بعض أخطاء الحماية تعيد صفحة HTML بدل JSON */ }
    if (response.status === 401 && !message) message = "انتهت جلسة الدخول. سجّل الدخول مرة أخرى.";
    if (response.status === 403 && !message) message = "ليس لديك صلاحية لتنفيذ هذا الطلب.";
    throw Object.assign(new Error(message), { status: response.status });
  }
  return response.json() as Promise<T>;
}
export type SupplierPayment = { id: number; company: number; supplier: number; supplier_name: string; invoice: number; invoice_number: string; payment_number: string; payment_date: string; method: "cash" | "bank"; payment_account: number | null; payment_account_name: string | null; amount: string; reference: string; status: "draft" | "posted"; journal_entry: number | null; journal_number: string | null; created_at: string; posted_at: string | null };
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
// واجهات المشتريات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const purchasingApi = {
  // جلب أوامر الشراء مرتبة حسب تاريخ الأمر للشركة المحددة.
  orders: (companyId: number) => request<PurchaseOrder[]>(`/api/purchasing/orders/?company=${companyId}&ordering=-order_date`),
  // إنشاء أمر شراء جديد وإرسال بياناته إلى الخادم.
  createOrder: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>("/api/purchasing/orders/", { method: "POST", body: JSON.stringify(payload) }); },
  // تأكيد أمر الشراء المحدد للانتقال إلى مرحلة التنفيذ.
  confirmOrder: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>(`/api/purchasing/orders/${id}/confirm/`, { method: "POST" }); },

  // جلب تقرير أعمار أرصدة الموردين في تاريخ القياس المحدد.
  supplierAging: (companyId: number, asOf = "", branchId?: number) => request<SupplierAging>(`/api/purchasing/invoices/supplier-aging/?company=${companyId}${asOf ? `&as_of=${asOf}` : ""}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب كشف حساب المورد مع رصيد أول وآخر المدة والحركات.
  supplierStatement: (companyId: number, supplierId: number, dateFrom = "", dateTo = "") => request<SupplierStatement>(`/api/purchasing/invoices/supplier-statement/?company=${companyId}&supplier=${supplierId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),

  // جلب مستندات استلام المشتريات للشركة.
  receipts: (companyId: number) => request<PurchaseReceipt[]>(`/api/purchasing/receipts/?company=${companyId}&ordering=-receipt_date`),
  // جلب فواتير الموردين للشركة.
  invoices: (companyId: number) => request<PurchaseInvoice[]>(`/api/purchasing/invoices/?company=${companyId}&ordering=-invoice_date`),
  // جلب دفعات الموردين للشركة.
  payments: (companyId: number) => request<SupplierPayment[]>(`/api/purchasing/payments/?company=${companyId}&ordering=-payment_date`),
  // إنشاء مستند استلام مشتريات جديد.
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>("/api/purchasing/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل سند القبض وتحديث رصيد العميل والخزينة.
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>(`/api/purchasing/receipts/${id}/post/`, { method: "POST" }); },
  // إنشاء فاتورة مورد وربطها ببيانات الاستلام.
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>("/api/purchasing/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل فاتورة البيع وتثبيت الإيراد والضريبة والذمم.
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>(`/api/purchasing/invoices/${id}/post/`, { method: "POST" }); },
  // إنشاء دفعة مورد جديدة.
  createPayment: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>("/api/purchasing/payments/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل دفعة المورد وتسجيل أثرها المحاسبي.
  postPayment: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>(`/api/purchasing/payments/${id}/post/`, { method: "POST" }); },
};


export type TreasuryAccount = {
  id: number; company: number; branch: number | null; branch_name: string; account: number;
  account_code: string; account_name: string; kind: "cash" | "bank"; code: string; name_ar: string;
  bank_name: string; account_number: string; iban: string; currency_code: string;
  opening_balance: string; fx_position_foreign_amount: string; fx_position_carrying_base_amount: string; is_active: boolean;
};
export type TreasuryFxRevaluation = {
  id:number; account:number; currency:string; revaluation_date:string; rate_date:string; rate:string;
  foreign_amount:string; previous_carrying_amount:string; new_carrying_amount:string; difference:string;
  journal_entry:number|null; journal_number:string;
};
export type TreasuryTransaction = {
  id: number; company: number; branch: number; fiscal_year: number; source_account: number; source_name: string;
  destination_account: number | null; destination_name: string; counter_account: number | null; counter_account_code: string;
  kind: "receipt" | "payment" | "transfer"; status: "draft" | "posted"; document_number: string;
  transaction_date: string; amount: string; description: string; reference: string; journal_entry: number | null;
  journal_number: string; transaction_currency: number|null; transaction_amount: string|null; exchange_rate: string|null; exchange_rate_date: string|null; carrying_amount: string|null; cash_flow_category: "operating" | "investing" | "financing"; created_at: string; posted_at: string | null;
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
// واجهات الخزينة والتدفقات النقدية: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const treasuryApi = {
  // جلب حسابات الخزينة والبنوك للشركة.
  accounts: (companyId: number) => request<TreasuryAccount[]>(`/api/treasury/accounts/?company=${companyId}&ordering=kind,code`),
  // جلب حركات الخزينة مرتبة من الأحدث.
  transactions: (companyId: number) => request<TreasuryTransaction[]>(`/api/treasury/transactions/?company=${companyId}&ordering=-transaction_date`),
  // إنشاء حساب خزينة أو بنك جديد.
  createAccount: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryAccount>("/api/treasury/accounts/", {method:"POST", body:JSON.stringify(payload)}); },
  // إنشاء حركة قبض أو صرف أو تحويل.
  createTransaction: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>("/api/treasury/transactions/", {method:"POST", body:JSON.stringify(payload)}); },
  // جلب سجل إعادة تقييم العملة الأجنبية للحساب المحدد.
  fxRevaluations: (id:number) => request<TreasuryFxRevaluation[]>(`/api/treasury/accounts/${id}/revaluations/`),
  // إعادة تقييم مركز عملة أجنبية وإنشاء قيد الفروق غير المحققة.
  revalueAccount: async (id:number, payload:{fiscal_year:number;revaluation_date:string;rate_date:string}) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryFxRevaluation>(`/api/treasury/accounts/${id}/revalue/`, {method:"POST", body:JSON.stringify(payload)}); },
  // ترحيل حركة الخزينة المحددة.
  postTransaction: async (id: number) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>(`/api/treasury/transactions/${id}/post/`, {method:"POST"}); },
  // جلب تقرير التدفقات النقدية بالطريقة غير المباشرة مع فلتر الفرع.
  indirectCashFlow: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => {
    const q = new URLSearchParams({company:String(companyId), date_from:dateFrom, date_to:dateTo});
    if (branch) q.set("branch", String(branch));
    return request<IndirectCashFlowReport>(`/api/accounting/journal-entries/indirect-cash-flow/?${q.toString()}`);
  },
  // جلب تقرير التدفقات النقدية المباشر للفترة والفرع.
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
export type VATReturnLine = {
  tax_code_id: number | null; tax_code: string | null; tax_name: string | null;
  category: "standard" | "zero_rated" | "exempt" | "out_of_scope" | "reverse_charge" | "legacy_unclassified";
  tax_rate: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string;
};
export type VATReturnReport = {
  company: number; country: "EG" | "SA"; currency: string; date_from: string; date_to: string;
  branch: number | null; scope_branch_ids: number[];
  sales: { taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string;
    by_rate: { tax_rate: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string }[];
    by_category: { category: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string }[];
    by_code: VATReturnLine[] };
  purchases: { taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string;
    by_rate: { tax_rate: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string }[];
    by_category: { category: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string }[];
    by_code: VATReturnLine[] };
  output_tax: string; input_tax: string; net_tax_payable: string;
  report_kind: "internal_vat_reconciliation"; note: string;
};
export type TaxCode = {
  id: number; company: number; company_name: string; country: "EG" | "SA"; code: string; name_ar: string;
  category: "standard" | "zero_rated" | "exempt" | "out_of_scope" | "reverse_charge";
  rate: string; effective_from: string; effective_to: string | null;
  input_tax_recoverable: boolean; is_active: boolean; created_at: string;
};
export type AuditEvent = {
  id: number; request_id: string; created_at: string; actor: string;
  company_id: number | null; company: string | null; branch_id: number | null; branch: string | null;
  action: "create" | "update" | "delete" | "action"; resource: string; object_id: string;
  http_method: string; status_code: number; succeeded: boolean; ip_address: string | null;
  user_agent: string; metadata: Record<string, unknown>;
};
export type AuditEventResponse = { count: number; results: AuditEvent[] };

// واجهات الإعدادات والمحاسبة والعملاء والموردون: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const api = {
  // جلب سجل التدقيق مع فلاتر الشركة والفترة ونوع الإجراء.
  auditEvents: (companyId: number, dateFrom: string, dateTo: string, action?: string) => {
    const q = new URLSearchParams({ company: String(companyId), date_from: dateFrom, date_to: dateTo, limit: "200" });
    if (action) q.set("action", action);
    return request<AuditEventResponse>(`/api/audit/events/?${q.toString()}`);
  },
  // جلب أكواد الضرائب المعرفة للشركة.
  taxCodes: (companyId: number) => request<TaxCode[]>(`/api/accounting/tax-codes/?company=${companyId}&ordering=code`),
  // إضافة كود ضريبي جديد للشركة.
  createTaxCode: async (payload: Partial<TaxCode>) => { await request<{ detail: string }>("/api/csrf/"); return request<TaxCode>("/api/accounting/tax-codes/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعطيل الكود الضريبي دون حذف السجل التاريخي.
  deactivateTaxCode: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<TaxCode>(`/api/accounting/tax-codes/${id}/`, { method: "PATCH", body: JSON.stringify({ is_active: false }) }); },
  // جلب العملات النشطة المتاحة في النظام.
  currencies: () => request<Currency[]>("/api/setup/currencies/?is_active=true"),
  // جلب الشركات المتاحة للمستخدم.
  companies: () => request<Company[]>("/api/setup/companies/"),
  // إنشاء شركة جديدة وإرسال إعداداتها الأساسية.
  createCompany: async (payload: Record<string, unknown>) => { await request<{ detail: string }>("/api/csrf/"); return request<Company>("/api/setup/companies/", { method: "POST", body: JSON.stringify(payload) }); },
  // تحديث بيانات الشركة الحالية بصلاحية مدير الشركة دون إنشاء سجل شركة جديد.
  updateCompany: async (id: number, payload: Record<string, unknown>) => { await request<{ detail: string }>("/api/csrf/"); return request<Company>(`/api/setup/companies/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // جلب الفروع النشطة للاستخدام في شاشات التشغيل.
  branches: (id: number) => request<Branch[]>("/api/setup/branches/?company=" + id + "&is_active=true"),
  // جلب جميع الفروع بما فيها الموقوفة لإدارة بيانات الشركة.
  allBranches: (id: number) => request<Branch[]>("/api/setup/branches/?company=" + id + "&include_inactive=true"),
  // إنشاء فرع جديد للشركة.
  createBranch: async (payload: Record<string, unknown>) => { await request<{ detail: string }>("/api/csrf/"); return request<Branch>("/api/setup/branches/", { method: "POST", body: JSON.stringify(payload) }); },
  // تحديث بيانات فرع قائم دون تغيير كوده أو الشركة التابعة له.
  updateBranch: async (id: number, payload: Partial<Pick<Branch, "name_ar" | "address" | "phone" | "is_active">>) => { await request<{ detail: string }>("/api/csrf/"); return request<Branch>(`/api/setup/branches/${id}/?include_inactive=true`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // جلب عضويات المستخدمين في الشركة.
  memberships: (companyId: number) => request<CompanyMembership[]>(`/api/setup/memberships/?company=${companyId}`),
  // جلب عضوية المستخدم الحالي بالشركة وصلاحياته.
  currentMembership: (companyId: number) => request<CompanyMembership>(`/api/setup/memberships/current/?company=${companyId}`),
  // البحث عن مستخدمين لإضافتهم إلى عضوية الشركة.
  searchCompanyUsers: (companyId: number, search: string) => request<CompanyUserLookup[]>(`/api/setup/memberships/users/?company=${companyId}&search=${encodeURIComponent(search)}`),
  // إنشاء عضوية مستخدم جديدة بالشركة.
  createMembership: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CompanyMembership>("/api/setup/memberships/", {method:"POST",body:JSON.stringify(payload)}); },
  // إنشاء حساب مدير شركة عادي وربطه بالشركة في عملية واحدة من لوحة مالك المنصة.
  createCompanyAdmin: async (payload: {company:number;username:string;display_name:string;email:string;password:string}) => { await request<{detail:string}>("/api/csrf/"); return request<CompanyMembership>("/api/setup/memberships/create-company-admin/", {method:"POST",body:JSON.stringify(payload)}); },
  // تعديل دور العضوية ونطاق الفروع المسموح بها.
  updateMembership: async (id: number, payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CompanyMembership>(`/api/setup/memberships/${id}/`, {method:"PATCH",body:JSON.stringify(payload)}); },
  // جلب أسعار الصرف التاريخية للشركة.
  exchangeRates: (companyId: number) => request<ExchangeRate[]>(`/api/setup/exchange-rates/?company=${companyId}`),
  // إضافة سعر صرف بتاريخ سريان؛ لا يتم تعديل السجلات السابقة.
  createExchangeRate: async (payload: { company: number; currency: number; rate_date: string; rate: string; source?: string; notes?: string }) => { await request<{ detail: string }>("/api/csrf/"); return request<ExchangeRate>("/api/setup/exchange-rates/", { method: "POST", body: JSON.stringify(payload) }); },
  // جلب السنوات المالية التابعة للشركة.
  fiscalYears: (id: number) => request<FiscalYear[]>("/api/setup/fiscal-years/?company=" + id),
  // إنشاء سنة مالية جديدة للشركة.
  createFiscalYear: async (payload: Record<string, unknown>) => { await request<{ detail: string }>("/api/csrf/"); return request<FiscalYear>("/api/setup/fiscal-years/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء قيد يومية جديد بحالته الأولية.
  createJournal: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل قيد اليومية المحدد.
  postJournal: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/" + id + "/post/", { method: "POST" }); },
  // إنشاء وترحيل قيد عكسي مطابق للقيد الأصلي بعد تأكيد المستخدم.
  reverseJournal: async (id: number, entryDate?: string) => { await request<{ detail: string }>("/api/csrf/"); return request<Journal>("/api/accounting/journal-entries/" + id + "/reverse/", { method: "POST", body: JSON.stringify(entryDate ? { entry_date: entryDate } : {}) }); },
  // جلب حسابات الخزينة والبنوك للشركة.
  accounts: (companyId: number) => request<Account[]>(`/api/accounting/accounts/?company=${companyId}&ordering=code`),
  // جلب معلومات قالب دليل الحسابات الملائم لنشاط الشركة.
  accountTemplateInfo: (companyId: number) => request<AccountTemplateInfo>(`/api/accounting/account-templates/?company=${companyId}`),
  // تطبيق قالب دليل الحسابات وربط أدواره بالشركة.
  applyAccountTemplate: async (companyId: number) => { await request<{detail:string}>("/api/csrf/"); return request<AccountTemplateApplyResult>("/api/accounting/account-templates/", {method:"POST", body:JSON.stringify({company:companyId})}); },
  // رفع ملف دليل الحسابات لمعاينته أو استيراده بعد التحقق.
  importAccountWorkbook: async (companyId: number, file: File, action: "preview" | "import") => {
    await request<{detail:string}>("/api/csrf/");
    const form = new FormData();
    form.append("company", String(companyId));
    form.append("action", action);
    form.append("file", file);
    const response = await fetch("/api/accounting/account-import/", {
      method: "POST", credentials: "same-origin", headers: { Accept: "application/json", "X-CSRFToken": csrfToken() }, body: form,
    });
    const body = await response.json();
    if (!response.ok) {
      const message = typeof body.detail === "string" ? body.detail : (body.errors || []).join(" ");
      throw Object.assign(new Error(message || `تعذر استيراد الملف (HTTP ${response.status}).`), { data: body });
    }
    return body as AccountImportPreview | AccountImportResult;
  },
  // جلب قيود اليومية للشركة مرتبة حسب التاريخ.
  journals: (companyId: number) => request<Journal[]>(`/api/accounting/journal-entries/?company=${companyId}&ordering=-entry_date`),
  // جلب تفاصيل قيد يومية محدد بما فيها سطوره.
  journalDetail: (id: number) => request<Journal>(`/api/accounting/journal-entries/${id}/`),
  // جلب تقرير مطابقة ضريبة القيمة المضافة للفترة والفرع.
  vatReturn: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => {
    const q = new URLSearchParams({ company: String(companyId), date_from: dateFrom, date_to: dateTo });
    if (branch) q.set("branch", String(branch));
    return request<VATReturnReport>(`/api/accounting/journal-entries/vat-return/?${q.toString()}`);
  },
  // جلب ميزان المراجعة مع خيارات الفترة واستبعاد قيود الإقفال.
  trialBalance: (companyId: number, dateFrom?: string, dateTo?: string, excludeClosing = false) => {
    const q = new URLSearchParams({company:String(companyId)});
    if (dateFrom) q.set("date_from", dateFrom);
    if (dateTo) q.set("date_to", dateTo);
    if (excludeClosing) q.set("exclude_closing", "true");
    return request<TrialBalance>(`/api/accounting/journal-entries/trial-balance/?${q.toString()}`);
  },
  // جلب الميزانية العمومية في تاريخ محدد.
  balanceSheet: (companyId: number, asOf: string, branch?: number) => request<BalanceSheetReport>(`/api/accounting/journal-entries/balance-sheet/?company=${companyId}&as_of=${asOf}${branch ? `&branch=${branch}` : ""}`),
  // جلب قائمة الدخل للفترة المحددة.
  incomeStatement: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => request<IncomeStatementReport>(`/api/accounting/journal-entries/income-statement/?company=${companyId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  // جلب حركة الأستاذ العام لحساب محدد خلال الفترة.
  generalLedger: (companyId: number, accountId: number, dateFrom: string, dateTo: string, branch?: number) => request<GeneralLedgerReport>(`/api/accounting/journal-entries/general-ledger/?company=${companyId}&account=${accountId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  // جلب تقرير التغيرات في حقوق الملكية للفترة.
  changesInEquity: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => request<EquityChangesReport>(`/api/accounting/journal-entries/changes-in-equity/?company=${companyId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  // جلب قائمة العملاء للشركة.
  customers: (companyId: number) => request<Customer[]>(`/api/sales/customers/?company=${companyId}&ordering=name_ar`),
  // جلب قائمة الموردين للشركة.
  suppliers: (companyId: number) => request<Supplier[]>(`/api/purchases/suppliers/?company=${companyId}&ordering=name_ar`),
  // إنشاء سجل مورد جديد.
  createSupplier: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Supplier>("/api/purchases/suppliers/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء سجل عميل جديد.
  createCustomer: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Customer>("/api/sales/customers/", { method: "POST", body: JSON.stringify(payload) }); },
};


export type InventoryUnit = { id: number; company: number; code: string; name_ar: string; symbol: string; decimal_places: number; is_active: boolean };
export type InventoryCategory = { id: number; company: number; code: string; name_ar: string; parent: number | null; is_active: boolean };
export type Product = { id: number; company: number; category: number | null; category_name: string | null; unit: number; unit_name: string; code: string; barcode: string; name_ar: string; name_en: string; product_type: "goods" | "service"; purchase_price: string; sale_price: string; costing_method: string; min_stock: string; track_expiry: boolean; is_active: boolean };
export type Warehouse = { id: number; company: number; branch: number | null; branch_code: string; code: string; name_ar: string; address: string; is_active: boolean };
// واجهات تعريفات المخزون: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const inventoryApi = {
  // جلب وحدات قياس الأصناف للشركة.
  units: (companyId: number) => request<InventoryUnit[]>(`/api/inventory/units/?company=${companyId}&ordering=code`),
  // جلب مجموعات وفئات الأصناف للشركة.
  categories: (companyId: number) => request<InventoryCategory[]>(`/api/inventory/categories/?company=${companyId}&ordering=code`),
  // جلب الأصناف المعرفة للشركة.
  products: (companyId: number) => request<Product[]>(`/api/inventory/products/?company=${companyId}&ordering=code`),
  // جلب المخازن التابعة للشركة.
  warehouses: (companyId: number) => request<Warehouse[]>(`/api/inventory/warehouses/?company=${companyId}&ordering=code`),
  // جلب مواقع التخزين داخل مخازن الشركة.
  locations: (companyId: number) => request<InventoryLocation[]>(`/api/inventory/locations/?warehouse__company=${companyId}&ordering=code`),
  // إنشاء وحدة قياس جديدة.
  createUnit: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryUnit>("/api/inventory/units/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء فئة أصناف جديدة.
  createCategory: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCategory>("/api/inventory/categories/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء بطاقة صنف جديدة.
  createProduct: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Product>("/api/inventory/products/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء مخزن جديد.
  createWarehouse: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Warehouse>("/api/inventory/warehouses/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء موقع تخزين داخل مخزن.
  createLocation: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryLocation>("/api/inventory/locations/", { method: "POST", body: JSON.stringify(payload) }); },
  // جلب إعدادات الصنف على مستوى كل مخزن.
  warehouseProductSettings: (companyId: number) => request<WarehouseProductSetting[]>(`/api/inventory/warehouse-product-settings/?warehouse__company=${companyId}&ordering=warehouse__code`),
  // إنشاء إعدادات حد إعادة الطلب للصنف بالمخزن.
  createWarehouseProductSetting: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<WarehouseProductSetting>("/api/inventory/warehouse-product-settings/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعديل إعدادات الصنف وحدوده داخل المخزن.
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
export type InventoryValuationIntegrity = {
  company: number; branch: number | null; branch_name: string; scope_branch_ids: number[];
  fifo_mismatch_count: number;
  fifo_mismatches: { product: number; product_code: string; product_name: string; warehouse: number; warehouse_code: string; warehouse_name: string; location: number | null; expiry_date: string | null; balance_quantity: string; layer_quantity: string; quantity_difference: string; balance_value: string; layer_value: string; value_difference: string }[];
  pending_movements_count: number;
  pending_movements: { id: number; movement_number: string; movement_type: string; movement_date: string; created_at: string; source_warehouse: string; destination_warehouse: string; lines_count: number; created_by: string }[];
  pending_movements_truncated: boolean; scope_note: string;
};
export type InventoryReconciliation = { company: number; currency: string; currency_decimal_places: number; rounding_tolerance: string; reconciliation_status: "matched" | "difference"; branch: number | null; branch_code: string; branch_name: string; scope_branch_ids: number[]; scope_note: string; account_id: number; account_code: string; account_name: string; stock_value: string; gl_balance: string; difference: string; is_reconciled: boolean; basis: string };
export type StockBalance = {
  id: number; product: number; product_code: string; product_name: string; warehouse: number;
  warehouse_name: string; location: number | null; location_name: string | null; expiry_date: string | null; quantity: string; average_cost: string; inventory_value: string; updated_at: string;
};
// واجهات حركات المخزون وتقاريره: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const stockApi = {
  // جلب حركات المخزون مرتبة من الأحدث.
  movements: (companyId: number) => request<StockMovement[]>(`/api/inventory/movements/?company=${companyId}&ordering=-movement_date`),
  // جلب أرصدة الأصناف حسب المخازن.
  balances: (companyId: number) => request<StockBalance[]>(`/api/inventory/balances/?warehouse__company=${companyId}&ordering=product__code`),
  // إنشاء مستند حركة مخزنية.
  createMovement: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>("/api/inventory/movements/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل الحركة المخزنية وتحديث الأرصدة.
  postMovement: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>(`/api/inventory/movements/${id}/post/`, { method: "POST" }); },
  // جلب تقرير مطابقة قيمة المخزون مع الحسابات.
  reconciliation: (companyId: number, branchId?: number) => request<InventoryReconciliation>(`/api/inventory/balances/reconciliation/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب فحص سلامة تقييم المخزون والحركات المرتبطة.
  valuationIntegrity: (companyId: number, branchId?: number) => request<InventoryValuationIntegrity>(`/api/inventory/balances/valuation-integrity/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب كارت الصنف وحركاته داخل مخزن وفترة محددين.
  stockCard: (productId: number, warehouseId: number, dateFrom: string, dateTo: string) => request<StockCard>(`/api/inventory/movements/stock-card/?product=${productId}&warehouse=${warehouseId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),
};


export type AccountRole = { id: number; company: number; role: string; account: number; account_code: string };
// واجهات أدوار الحسابات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const accountRoleApi = {
  // جلب أدوار الحسابات المعرفة للشركة.
  list: (companyId: number) => request<AccountRole[]>(`/api/accounting/account-roles/?company=${companyId}`),
  // إنشاء ربط دور محاسبي بحساب في دليل الحسابات.
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>("/api/accounting/account-roles/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعديل الحساب المرتبط بالدور المحاسبي المحدد.
  update: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>(`/api/accounting/account-roles/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
};


export type SalesDelivery = { id: number; company: number; customer: number; customer_name: string; warehouse: number; warehouse_name: string; delivery_number: string; delivery_date: string; customer_reference: string; notes: string; movement_status: "draft" | "posted"; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string; expiry_date: string | null }[]; created_at: string };
export type SalesInvoice = { id: number; company: number; customer: number; customer_name: string; delivery: number; delivery_number: string; invoice_number: string; invoice_date: string; status: "draft" | "posted"; net_total: string; discount_total: string; discount_approval_limit: string | null; invoice_approval_limit: string | null; approval_status: "not_required" | "pending" | "approved" | "rejected" | "superseded"; tax_total: string; grand_total: string; paid_total: string; balance_due: string; notes: string; journal_entry: number | null; journal_number: string | null; invoice_lines: { id: number; delivery_line: number; product_code: string; product_name: string; quantity: string; unit_price: string; tax_rate: string; tax_code: number | null; discount_percent: string; discount_amount: string; net_amount: string; tax_amount: string; total_amount: string }[]; created_at: string; posted_at: string | null };

export type DiscountApprovalPolicy = { id: number; company: number; company_name: string; max_discount_percent: string | null; max_sales_invoice_amount: string | null; max_purchase_invoice_amount: string | null; max_payment_amount: string | null; created_at: string; updated_at: string };
export type SalesInvoiceApprovalLineSnapshot = {
  delivery_line_id: number; product_id: number; product_code: string; product_name: string;
  quantity: string; unit_price: string; tax_code_id: number | null; tax_code: string | null;
  tax_category: string | null; tax_rate: string; discount_percent: string;
  discount_amount: string; net_amount: string; tax_amount: string; total_amount: string;
};
export type SalesInvoiceApproval = {
  id: number; company: number; invoice: number; invoice_number: string; customer_name: string;
  invoice_date: string; invoice_total: string; requested_by: number; requested_by_name: string;
  requested_limit: string | null; requested_invoice_limit: string | null; requested_discount_total: string; requested_max_discount_percent: string;
  requested_lines_snapshot: SalesInvoiceApprovalLineSnapshot[]; status: "pending" | "approved" | "rejected" | "superseded";
  decided_by: number | null; decided_by_name: string | null; decision_note: string;
  requested_at: string; decided_at: string | null;
};
export type FinancialApprovalRequest = {
  id: number; company: number;
  document_type: "purchase_invoice" | "supplier_payment" | "treasury_payment";
  purchase_invoice: number | null; supplier_payment: number | null; treasury_transaction: number | null;
  document_number: string; document_total: string;
  requested_by: number; requested_by_name: string; requested_limit: string; requested_amount: string;
  requested_snapshot: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "superseded";
  decided_by: number | null; decided_by_name: string | null; decision_note: string;
  requested_at: string; decided_at: string | null;
};
// واجهات الموافقات والخصومات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const approvalsApi = {
  // جلب سياسات اعتماد الخصومات للشركة.
  policies: (companyId: number) => request<DiscountApprovalPolicy[]>(`/api/approvals/discount-policies/?company=${companyId}`),
  // إنشاء سياسة جديدة لحدود الخصم والموافقة.
  createPolicy: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<DiscountApprovalPolicy>("/api/approvals/discount-policies/", {method:"POST",body:JSON.stringify(payload)}); },
  // تعديل سياسة الخصم الحالية.
  updatePolicy: async (id: number, payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<DiscountApprovalPolicy>(`/api/approvals/discount-policies/${id}/`, {method:"PATCH",body:JSON.stringify(payload)}); },
  // جلب طلبات اعتماد فواتير البيع.
  requests: (companyId: number) => request<SalesInvoiceApproval[]>(`/api/approvals/requests/?company=${companyId}&ordering=-requested_at`),
  // اعتماد طلب الخصم وإرسال ملاحظة الاعتماد.
  approve: async (id: number, note = "") => { await request<{detail:string}>("/api/csrf/"); return request<SalesInvoiceApproval>(`/api/approvals/requests/${id}/approve/`, {method:"POST",body:JSON.stringify({note})}); },
  // رفض طلب الخصم مع تسجيل سبب الرفض.
  reject: async (id: number, note: string) => { await request<{detail:string}>("/api/csrf/"); return request<SalesInvoiceApproval>(`/api/approvals/requests/${id}/reject/`, {method:"POST",body:JSON.stringify({note})}); },
  // جلب طلبات الموافقات المالية.
  financialRequests: (companyId: number) => request<FinancialApprovalRequest[]>(`/api/approvals/financial-requests/?company=${companyId}&ordering=-requested_at`),
  // اعتماد الطلب المالي مع الملاحظة.
  approveFinancial: async (id: number, note = "") => { await request<{detail:string}>("/api/csrf/"); return request<FinancialApprovalRequest>(`/api/approvals/financial-requests/${id}/approve/`, {method:"POST",body:JSON.stringify({note})}); },
  // رفض الطلب المالي مع توثيق الملاحظة.
  rejectFinancial: async (id: number, note: string) => { await request<{detail:string}>("/api/csrf/"); return request<FinancialApprovalRequest>(`/api/approvals/financial-requests/${id}/reject/`, {method:"POST",body:JSON.stringify({note})}); },
};

export type CustomerReceipt = { id: number; company: number; customer: number; customer_name: string; invoice: number; invoice_number: string; receipt_number: string; receipt_date: string; method: "cash" | "bank"; payment_account: number | null; payment_account_name: string | null; amount: string; reference: string; status: "draft" | "posted"; journal_entry: number | null; journal_number: string | null; created_at: string; posted_at: string | null };
export type CustomerStatement = {
  company:number; customer:number; customer_name:string; date_from:string|null; date_to:string|null;
  branch_filter:number|null; opening_balance:string; closing_balance:string;
  rows:{date:string;document_type:"invoice"|"receipt";document_number:string;description:string;debit:string;credit:string;balance:string}[];
};
export type CustomerAging = {
  company: number; as_of: string; customer_filter: number | null; branch_filter: number | null;
  totals: { not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string };
  customers: { customer: number; customer_code: string; customer_name: string; not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string;
    invoices: { invoice: number; invoice_number: string; invoice_date: string; due_date: string; overdue_days: number; bucket: string; grand_total: string; paid_as_of: string; balance_due: string }[] }[];
};
// واجهات المبيعات والتحصيل: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const salesApi = {
  // جلب كشف حساب العميل مع رصيد أول وآخر المدة والحركات المرحلة.
  customerStatement: (companyId:number, customerId:number, dateFrom="", dateTo="") => request<CustomerStatement>(`/api/sales-documents/invoices/customer-statement/?company=${companyId}&customer=${customerId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),
  // جلب مستندات تسليم المبيعات للشركة.
  deliveries: (companyId: number) => request<SalesDelivery[]>(`/api/sales-documents/deliveries/?company=${companyId}&ordering=-delivery_date`),
  // جلب فواتير الموردين للشركة.
  invoices: (companyId: number) => request<SalesInvoice[]>(`/api/sales-documents/invoices/?company=${companyId}&ordering=-invoice_date`),
  // جلب مستندات استلام المشتريات للشركة.
  receipts: (companyId: number) => request<CustomerReceipt[]>(`/api/sales-documents/receipts/?company=${companyId}&ordering=-receipt_date`),
  // جلب تقرير أعمار مديونيات العملاء مع فلاتر التاريخ والفرع.
  customerAging: (companyId: number, asOf = "", customerId?: number, branchId?: number) => request<CustomerAging>(`/api/sales-documents/invoices/customer-aging/?company=${companyId}${asOf ? `&as_of=${asOf}` : ""}${customerId ? `&customer=${customerId}` : ""}${branchId ? `&branch=${branchId}` : ""}`),
  // إنشاء مستند تسليم مبيعات جديد.
  createDelivery: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>("/api/sales-documents/deliveries/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل التسليم وتثبيت حركة المخزون وتكلفة المبيعات.
  postDelivery: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesDelivery>(`/api/sales-documents/deliveries/${id}/post/`, { method: "POST" }); },
  // إنشاء فاتورة بيع جديدة.
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>("/api/sales-documents/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعديل فاتورة البيع قبل ترحيلها.
  updateInvoice: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>(`/api/sales-documents/invoices/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // ترحيل فاتورة البيع وتثبيت الإيراد والضريبة والذمم.
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SalesInvoice>(`/api/sales-documents/invoices/${id}/post/`, { method: "POST" }); },
  // إنشاء سند قبض من عميل.
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>("/api/sales-documents/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل سند القبض وتحديث رصيد العميل والخزينة.
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>(`/api/sales-documents/receipts/${id}/post/`, { method: "POST" }); },
};


export type BankReconciliation = { id: number; company: number; bank_account: number; bank_name: string; bank_code: string; statement_number: string; statement_date: string; opening_balance: string; closing_balance: string; status: "draft" | "reconciled"; line_count: number; created_at: string; reconciled_at: string | null };
export type BankStatementLine = { id: number; reconciliation: number; line_number: number; transaction_date: string; description: string; reference: string; amount: string; matched_transaction: number | null; matched_document: string; matched_amount: string | null; matched_at: string | null };
export type BankReconciliationSummary = { reconciliation: BankReconciliation; statement_calculated: string; statement_difference: string; book_balance: string; outstanding_total: string; expected_book_balance: string; unexplained_difference: string; matched_lines: number; unmatched_lines: number; lines: BankStatementLine[]; outstanding_transactions: { id: number; date: string; document_number: string; description: string; amount: string }[] };
// واجهات التسوية البنكية: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const bankReconciliationApi = {
  // جلب جلسات التسوية البنكية للشركة مرتبة حسب تاريخ كشف الحساب.
  list: (companyId: number) => request<BankReconciliation[]>(`/api/treasury/reconciliations/?company=${companyId}&ordering=-statement_date`),
  // إنشاء جلسة تسوية بنكية جديدة.
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>("/api/treasury/reconciliations/", { method: "POST", body: JSON.stringify(payload) }); },
  // جلب ملخص التسوية البنكية والأرصدة والسطور غير المطابقة.
  summary: (id: number) => request<BankReconciliationSummary>(`/api/treasury/reconciliations/${id}/summary/`),
  // إضافة سطر كشف بنك إلى جلسة التسوية.
  addLine: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/add_line/`, { method: "POST", body: JSON.stringify(payload) }); },
  // مطابقة سطر كشف البنك مع حركة خزينة محددة.
  match: async (id: number, lineId: number, transactionId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/match/`, { method: "POST", body: JSON.stringify({line_id:lineId,transaction_id:transactionId}) }); },
  // فك مطابقة سطر كشف البنك من الحركة المرتبطة.
  unmatch: async (id: number, lineId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/unmatch/`, { method: "POST", body: JSON.stringify({line_id:lineId}) }); },
  // اعتماد التسوية البنكية بعد استكمال إجراءات المطابقة.
  reconcile: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>(`/api/treasury/reconciliations/${id}/reconcile/`, { method: "POST" }); },
};

// رفع كشف البنك بصيغة CSV مع التحقق من الاستجابة وإرجاع عدد السطور المستوردة.
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
// واجهات الأصول الثابتة: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const fixedAssetsApi = {
  // جلب سجل الأصول الثابتة للشركة.
  list: (companyId: number) => request<FixedAsset[]>(`/api/assets/fixed-assets/?company=${companyId}`),
  // جلب سجل الأصول الثابتة في تاريخ محدد.
  register: (companyId:number, asOf:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),as_of:asOf}); if(branch) q.set("branch",String(branch));
    return request<AssetRegisterReport>(`/api/assets/fixed-assets/register/?${q.toString()}`);
  },
  // جلب تقرير الإهلاك للفترة والفرع المحددين.
  depreciationReport: (companyId:number, dateFrom?:string, dateTo?:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId)}); if(dateFrom) q.set("date_from",dateFrom); if(dateTo) q.set("date_to",dateTo); if(branch) q.set("branch",String(branch));
    return request<AssetDepreciationReport>(`/api/assets/fixed-assets/depreciation-report/?${q.toString()}`);
  },
  // جلب تقرير حركة الأصول للفترة والفرع.
  movementReport: (companyId:number, dateFrom?:string, dateTo?:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId)}); if(dateFrom) q.set("date_from",dateFrom); if(dateTo) q.set("date_to",dateTo); if(branch) q.set("branch",String(branch));
    return request<AssetMovementReport>(`/api/assets/fixed-assets/movement-report/?${q.toString()}`);
  },
  // إنشاء بطاقة أصل ثابت جديدة.
  create: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<FixedAsset>("/api/assets/fixed-assets/", {method:"POST",body:JSON.stringify(payload)}); },
  // احتساب وترحيل إهلاك الأصل عن الفترة والسنة المالية.
  depreciate: async (id: number, periodDate: string, fiscalYear: number) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;depreciation_amount:string}>(`/api/assets/fixed-assets/${id}/depreciate/`, {method:"POST",body:JSON.stringify({period_date:periodDate,fiscal_year:fiscalYear})}); },
  // جلب جدول الإهلاك التفصيلي للأصل.
  schedule: (id: number) => request<AssetSchedule>(`/api/assets/fixed-assets/${id}/schedule/`),
  // تنفيذ بيع أو استبعاد الأصل وإرسال بيانات التسوية.
  dispose: async (id: number, disposalDate: string, proceeds: string, treasuryAccount: number, fiscalYear: number) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;disposal_journal:number;accumulated_depreciation:string;net_book_value:string;proceeds:string;gain:string;loss:string}>(`/api/assets/fixed-assets/${id}/dispose/`, {method:"POST",body:JSON.stringify({disposal_date:disposalDate,proceeds,treasury_account:treasuryAccount,fiscal_year:fiscalYear})}); },
};


export type HrEmployee = {
  id:number; company:number; code:string; full_name_ar:string; full_name_en:string;
  national_id:string; insurance_number:string; email:string; phone:string; address:string;
  branch:number; branch_name:string; department:number|null; department_name:string|null;
  position:number|null; position_name:string|null; hire_date:string; termination_date:string|null;
  status:"active"|"leave"|"unpaid_leave"|"suspended"|"resigned"|"retired";
  basic_salary:string; currency:number; currency_code:string; payment_method:"bank"|"cash";
};
export type HrPayrollRuleSet = {id:number;company:number;country:"EG"|"SA";name:string;effective_from:string;effective_to:string|null;configuration:Record<string,unknown>;is_active:boolean};
export type HrPayrollCycle = {
  id:number; company:number; period_start:string; period_end:string; payment_date:string;
  status:"draft"|"calculated"|"review"|"approved"|"posted"|"payment_pending"|"paid";
  rule_set:number|null; results_count:number; journal_entry:number|null;
};
export type HrPayrollResult = {
  id:number; employee:number; employee_code:string; employee_name:string;
  basic_salary_snapshot:string; earnings_total:string; employee_deductions_total:string;
  employer_share_total:string; employer_share_display_addition:string; employer_share_display_offset:string;
  displayed_earnings_total:string; displayed_deductions_total:string; net_pay:string; company_cost:string;
  lines:{code:string;name:string;kind:string;amount:string;display_only:boolean;calculation:Record<string,unknown>}[];
};
export type HrSalaryHistory = {
  id:number; employee:number; employee_name:string; effective_date:string;
  previous_salary:string; new_salary:string;
  change_type:"annual_raise"|"promotion"|"adjustment"; reason:string; created_at:string;
};
export type HrEmployeePayReport = {
  company:number; date_from:string; date_to:string; branch:number|null; currency_note:string; employee_count:number;
  totals:{earnings:string;employee_deductions:string;employer_share:string;net_pay:string;company_cost:string}|null;
  employees:{
    employee_id:number;employee_code:string;employee_name:string;branch:string;department:string;currency:string;cycle_count:number;
    earnings:string;employee_deductions:string;employer_share:string;net_pay:string;company_cost:string;
    components:{code:string;name:string;kind:string;amount:string}[];
    periods:{cycle_id:number;period_start:string;period_end:string;status:string;net_pay:string}[];
  }[];
};
// واجهات الموارد البشرية والمرتبات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const hrApi = {

  // جلب مكونات المرتبات والاستحقاقات والخصومات.
  payrollComponents: (companyId:number) => request<HrPayrollComponent[]>(`/api/hr/payroll-components/?company=${companyId}&ordering=kind,code`),
  // إنشاء مكون مرتبات جديد.
  createPayrollComponent: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrPayrollComponent>("/api/hr/payroll-components/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب مكونات الأجر المخصصة للموظفين.
  employeeComponents: (companyId:number) => request<HrEmployeeComponent[]>(`/api/hr/employee-components/?company=${companyId}`),
  // ربط مكون أجر بموظف وتحديد فترة سريانه.
  createEmployeeComponent: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrEmployeeComponent>("/api/hr/employee-components/", {method:"POST",body:JSON.stringify(payload)}); },
  // اعتماد طلب إجازة الموظف.
  approveLeave: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<HrLeaveRequest>(`/api/hr/leave-requests/${id}/approve/`, {method:"POST"}); },
  // جلب سجلات الحضور والانصراف للشركة.
  attendance: (companyId:number) => request<HrAttendance[]>(`/api/hr/attendance/?company=${companyId}&ordering=-work_date`),
  // إنشاء سجل حضور ليوم عمل.
  createAttendance: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrAttendance>("/api/hr/attendance/", {method:"POST",body:JSON.stringify(payload)}); },
  // تعديل حالة الحضور أو ساعات العمل لسجل محدد.
  updateAttendance: async (id:number, payload:Partial<HrAttendance>) => { await request<{detail:string}>("/api/csrf/"); return request<HrAttendance>(`/api/hr/attendance/${id}/`, {method:"PATCH",body:JSON.stringify(payload)}); },
  // جلب طلبات إجازات الموظفين.
  leaveRequests: (companyId:number) => request<HrLeaveRequest[]>(`/api/hr/leave-requests/?company=${companyId}&ordering=-start_date`),
  // تسجيل طلب إجازة جديد.
  createLeaveRequest: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrLeaveRequest>("/api/hr/leave-requests/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب سلف الموظفين وأرصدتها.
  employeeLoans: (companyId:number) => request<HrEmployeeLoan[]>(`/api/hr/employee-loans/?company=${companyId}`),
  // تسجيل سلفة موظف.
  createEmployeeLoan: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrEmployeeLoan>("/api/hr/employee-loans/", {method:"POST",body:JSON.stringify(payload)}); },
  // تسجيل سلفة موظف مع إنشاء جدول الأقساط.
  createEmployeeLoanWithSchedule: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrEmployeeLoan>("/api/hr/employee-loans/create-with-schedule/", {method:"POST",body:JSON.stringify(payload)}); },
  // تسجيل قسط من أقساط سلفة موظف.
  createLoanInstallment: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,unknown>>("/api/hr/loan-installments/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب سجل تغييرات راتب الموظف.
  salaryHistory: (employeeId:number) => request<HrSalaryHistory[]>(`/api/hr/salary-history/?employee=${employeeId}`),
  // تسجيل تغيير راتب بتاريخ سريان محدد.
  createSalaryChange: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrSalaryHistory>("/api/hr/salary-history/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب ملفات الموظفين للشركة.
  employees: (companyId:number) => request<HrEmployee[]>(`/api/hr/employees/?company=${companyId}&ordering=code`),
  // إنشاء ملف موظف جديد.
  createEmployee: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrEmployee>("/api/hr/employees/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب مجموعات قواعد المرتبات حسب تاريخ السريان.
  payrollRuleSets: (companyId:number) => request<HrPayrollRuleSet[]>(`/api/hr/payroll-rule-sets/?company=${companyId}&ordering=-effective_from`),
  // إنشاء مجموعة قواعد مرتبات وتأمينات.
  createPayrollRuleSet: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrPayrollRuleSet>("/api/hr/payroll-rule-sets/", {method:"POST",body:JSON.stringify(payload)}); },
  // جلب دورات المرتبات للشركة.
  payrollCycles: (companyId:number) => request<HrPayrollCycle[]>(`/api/hr/payroll-cycles/?company=${companyId}&ordering=-period_start`),
  // إنشاء دورة مرتبات للفترة المحددة.
  createPayrollCycle: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<HrPayrollCycle>("/api/hr/payroll-cycles/", {method:"POST",body:JSON.stringify(payload)}); },
  // تنفيذ احتساب دورة المرتبات وفق القواعد والبيانات المتاحة.
  calculatePayroll: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,string|number>>(`/api/hr/payroll-cycles/${id}/calculate/`, {method:"POST"}); },
  // إرسال دورة المرتبات للمراجعة قبل الاعتماد.
  sendPayrollToReview: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<HrPayrollCycle>(`/api/hr/payroll-cycles/${id}/send_to_review/`, {method:"POST"}); },
  // اعتماد دورة المرتبات بعد المراجعة.
  approvePayroll: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<HrPayrollCycle>(`/api/hr/payroll-cycles/${id}/approve/`, {method:"POST"}); },
  // ترحيل دورة المرتبات وإنشاء أثرها المحاسبي.
  postPayroll: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,unknown>>(`/api/hr/payroll-cycles/${id}/post/`, {method:"POST"}); },
  // تجهيز دفعة المرتبات وربطها بحساب الخزينة.
  preparePayrollPayment: async (id:number, treasuryAccountId:number) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,unknown>>(`/api/hr/payroll-cycles/${id}/prepare_payment/`, {method:"POST",body:JSON.stringify({treasury_account_id:treasuryAccountId})}); },
  // إتمام صرف دفعة المرتبات وتحديث حالة الدورة.
  completePayrollPayment: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,unknown>>(`/api/hr/payroll-cycles/${id}/complete_payment/`, {method:"POST"}); },
  // جلب تقرير مستحقات الموظفين للفترة والفرع.
  employeePayReport: (companyId:number, dateFrom:string, dateTo:string, branchId?:number) => {
    const q = new URLSearchParams({company:String(companyId),date_from:dateFrom,date_to:dateTo});
    if(branchId) q.set("branch",String(branchId));
    return request<HrEmployeePayReport>(`/api/hr/payroll-cycles/employee-pay-report/?${q.toString()}`);
  },
  // جلب نتائج احتساب دورة المرتبات وتفاصيل عناصرها.
  payrollResults: (id:number) => request<HrPayrollResult[]>(`/api/hr/payroll-cycles/${id}/results/`),
};

export type HrPayrollComponent = {id:number;company:number;code:string;name_ar:string;kind:"earning"|"deduction"|"employer_share";method:"fixed"|"percent_basic"|"per_day"|"per_hour"|"friday_double_day"|"employee_insurance"|"employer_insurance"|"progressive_tax"|"manual";default_value:string;affects_taxable_base:boolean;affects_insurance_base:boolean;requires_approval:boolean;is_active:boolean;expense_account:number|null;payable_account:number|null};
export type HrEmployeeComponent = {id:number;employee:number;component:number;component_name:string;value:string|null;effective_from:string;effective_to:string|null;is_active:boolean};
export type HrAttendance = {id:number;employee:number;employee_code:string;employee_name:string;work_date:string;status:"present"|"absent"|"leave"|"unpaid_leave"|"holiday";worked_hours:string;overtime_hours:string;friday_worked:boolean;late_minutes:number;notes:string};
export type HrLeaveRequest = {id:number;employee:number;employee_name:string;kind:"annual"|"sick"|"emergency"|"unpaid";start_date:string;end_date:string;days:string;reason:string;status:"pending"|"approved"|"rejected"};
export type HrEmployeeLoan = {id:number;employee:number;employee_name:string;reference:string;issue_date:string;principal:string;installment_count:number;installment_amount:string;receivable_account:number|null;outstanding_balance:string;status:"active"|"settled"|"suspended";notes:string};
