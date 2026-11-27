/*
 * طبقة الاتصال الخلفية للواجهة: تعريف أنواع البيانات ودوال طلب واجهات Django API.
 * توحّد مسارات الطلبات ومعالجة الاستجابات المستخدمة في شاشات النظام.
 */
export type Currency = { id: number; code: string; name_ar: string; name_en: string; symbol: string; decimal_places: number; is_active: boolean };
export type Company = { id: number; code: string; name_ar: string; name_en?: string; country: "EG" | "SA"; activity?: "commercial" | "service" | "medical" | "restaurants" | "industrial"; activities?: ("commercial" | "service" | "medical" | "restaurants" | "industrial")[]; activity_assignments?: { id: number; activity: "commercial" | "service" | "medical" | "restaurants" | "industrial"; is_active: boolean }[]; base_currency: number; base_currency_code: string; tax_registration_number?: string; address?: string; phone?: string; email?: string; is_active?: boolean; created_at?: string; updated_at?: string };
export type Branch = { id: number; company: number; company_code?: string; code: string; name_ar: string; address?: string; phone?: string; is_head_office?: boolean; is_active: boolean; created_at?: string };
export type CompanyMembership = { id: number; company: number; company_code: string; user: number; username: string; display_name?: string; role: "admin" | "accountant" | "viewer"; all_branches: boolean; branches: number[]; is_active: boolean; created_at: string };
export type CompanyUserLookup = { id: number; username: string; display_name: string };
export type FiscalYear = { id: number; company: number; name: string; start_date: string; end_date: string; status: string };
export type FiscalPeriod = { id:number; name:string; start_date:string; end_date:string; status:"open"|"closed"; closed_at:string|null; closed_by:number|null; reopened_at:string|null; reopened_by:number|null; reopen_reason:string };
export type FiscalYearClosingReview = {
  fiscal_year: number;
  status: string;
  draft_entries: number;
  unbalanced_entries: number;
  closing_entry_required: boolean;
  closing_entry_exists: boolean;
  closing_entry_posted: boolean;
  unclosed_income_statement_accounts: number;
  next_fiscal_year: number | null;
  opening_entry_required: boolean;
  opening_entry_exists: boolean;
  opening_entry_posted: boolean;
  opening_balance_mismatches: number;
  can_close: boolean;
  checks: { no_drafts: boolean; balanced_entries: boolean; closing_entry: boolean; next_year_opening: boolean };
};
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
export type JournalLine = { id: number; account: number; account_code: string; account_name: string; debit: string; credit: string; description: string; cost_center: number | null };
export type TrialBalance = { company_id: number; debit_total: string; credit_total: string; difference: string; accounts: { account_id: number; code: string; name_ar: string; nature: string; opening_debit: string; opening_credit: string; opening_balance: string; debit_total: string; credit_total: string; net_balance: string; closing_balance: string }[]; opening_debit_total: string; opening_credit_total: string; closing_debit_credit_difference: string };
export type SubledgerReconciliation = {
  company:number; as_of:string; branch:number|null; branch_name:string; scope_branch_ids:number[];
  currency:string; currency_decimal_places:number; matched_count:number; partial_reconciliation_count:number; difference_count:number;
  pending_count:number; pending_documents_count:number; pending_documents:{sales:number;purchasing:number;payroll:number;inventory:number};
  posted_documents_missing_journal:{sales:number;purchasing:number;payroll:number;inventory:number}; posted_documents_missing_journal_count:number;
  configuration_issue_count:number; unavailable_count:number; is_reconciled:boolean; note:string;
  rows:{key:string;label:string;status:"matched"|"partial_reconciliation"|"difference"|"pending_documents"|"not_configured"|"invalid_account"|"historical_snapshot_unavailable";
    accounts:{id:number;code:string;name:string}[];party_balances?:{party_id:number;code:string;name:string;balance:string}[];party_count?:number;subledger_balance:string|null;gl_balance:string|null;difference:string|null;
    source_journal_count:number;pending_documents_count:number;unmatched_entry_count:number;detail:string;
    unmatched_entries:{journal_id:number;entry_number:string;entry_date:string;description:string;account_code:string;debit:string;credit:string}[];
  }[];
};
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

export type ApiPage<T> = { count: number; next: string | null; previous: string | null; results: T[] };
export type JournalSummary = { total: number; drafts: number; posted: number };
export type Journal = {
  id: number; company: number; branch: number; branch_code: string; fiscal_year: number;
  fiscal_year_name: string; entry_number: string; entry_date: string; description: string;
  source_type: string; status: string; reversal_of: number | null; lines: JournalLine[];
};
export type Customer = {
  id: number; company: number; code: string; customer_type: "company" | "individual";
  name_ar: string; name_en: string; tax_number: string; phone: string; mobile: string;
  email: string; address: string; credit_limit: string; payment_terms_days: number;
  default_discount_percent: string; discount_approval_limit: string | null;
  notes: string; is_active: boolean; created_at: string; updated_at: string;
};
export type PurchaseOrder = {
  id: number; company: number; supplier: number; supplier_name: string; warehouse: number; warehouse_name: string;
  order_number: string; order_date: string; expected_date: string | null; grand_total: string; approval_status: "pending" | "approved" | "rejected" | "superseded" | null; status: "draft" | "confirmed" | "partially_received" | "received" | "cancelled";
  notes: string; line_items: { id: number; product: number; product_code: string; product_name: string; product_type: "goods" | "service"; quantity: string;
    received_quantity: string; remaining_quantity: string; unit_price: string; tax_rate: string }[];
  created_at: string; confirmed_at: string | null;
};
export type PurchaseRequisition = {
  id: number; company: number; request_number: string; request_date: string; required_date: string | null;
  department: string; purpose: string; notes: string;
  status: "draft" | "submitted" | "approved" | "rejected" | "converted" | "cancelled";
  requested_by: number; requested_by_name: string; reviewed_by: number | null; reviewed_by_name: string | null;
  reviewed_at: string | null; review_note: string; converted_order: number | null;
  line_items: { id: number; product: number; product_code: string; product_name: string; product_type: "goods" | "service";
    requested_quantity: string; estimated_unit_price: string | null; notes: string }[];
  created_at: string;
};


export type PurchaseServiceAcceptance = {
  id: number; company: number; order: number; order_number: string; supplier: number; supplier_name: string; branch: number | null;
  acceptance_number: string; acceptance_date: string; status: "draft" | "accepted" | "rejected"; notes: string;
  accepted_by: number | null; accepted_by_name: string | null; accepted_at: string | null;
  line_items: { id: number; order_line: number; product: number; product_code: string; product_name: string; quantity: string; notes: string }[];
  created_at: string;
};

export type PurchaseRFQ = {
  id: number; company: number; source_requisition: number | null; rfq_number: string; issue_date: string; closing_date: string;
  status: "draft" | "open" | "closed" | "awarded" | "cancelled"; terms: string;
  line_items: {id:number;product:number;product_code:string;product_name:string;product_type:"goods"|"service";quantity:string;notes:string}[];
  supplier_list: {id:number;code:string;name:string;status:string}[];
  awarded_quotation: number | null; converted_order: number | null; award_reason: string; awarded_by: number | null; awarded_at: string | null; created_at: string;
};

export type GRNIAgingReport = {
  company:number; as_of:string; grand_total:string;
  aging: { "0_30":string; "31_60":string; "61_90":string; over_90:string };
  monthly_summary:{month:string;amount:string}[];
  rows:{receipt_id:number;receipt_number:string;receipt_date:string;age_days:number;aging_bucket:string;supplier_id:number;supplier_name:string;warehouse_id:number;warehouse_name:string;branch_id:number|null;amount:string;invoiced_amount:string;invoice_status:string;invoice_number:string|null}[];
};

export type SupplierQuotation = {
  id:number; rfq:number; supplier:number; supplier_name:string; quotation_number:string; quotation_date:string;
  valid_until:string; delivery_days:number; payment_terms:string; notes:string; status:"submitted"|"withdrawn";
  line_items:{id:number;rfq_line:number;product_code:string;product_name:string;quantity:string;unit_price:string;discount_percent:string;tax_rate:string;net_amount:string;tax_amount:string;total_amount:string}[];
  total_before_tax:string; tax_total:string; grand_total:string; submitted_at:string;
};



export type PurchaseAnalysisReport = {
  company:number; date_from:string|null; date_to:string|null; branch:number|null;supplier:number|null;product:number|null;activity:number|null;
  totals:{purchase_net:string;returned_net:string;net_purchases:string;nonrecoverable_tax:string;inventory_nonrecoverable_tax:string;total_cost:string;tax_amount:string;invoice_count:number;line_count:number};
  rows:{
    kind:"inventory"|"direct_expense";supplier_id:number;supplier_name:string;
    product_id:number|null;product_code:string;product_name:string;
    expense_account_id:number|null;expense_account_code:string;expense_account_name:string;
    branch_id:number|null;branch_name:string;warehouse_id:number|null;warehouse_name:string;
    activity_id:number|null;activity_name:string;invoice_count:number;line_count:number;
    quantity:string;returned_quantity:string;purchase_net:string;returned_net:string;
    nonrecoverable_tax:string;net_purchases:string;total_cost:string;tax_amount:string;
  }[];
};

export type PurchaseMatchingReport = {
  company:number; date_from:string|null; date_to:string|null; branch:number|null; supplier:number|null; product:number|null;
  totals:{line_count:number;unreceived_line_count:number;uninvoiced_line_count:number;overinvoiced_line_count:number;order_value:string;invoiced_net_amount:string;price_variance:string};
  rows:{
    order_id:number;order_number:string;order_date:string;order_status:string;
    supplier_id:number;supplier_name:string;warehouse_id:number;warehouse_name:string;branch_id:number|null;branch_name:string;
    order_line_id:number;product_id:number;product_code:string;product_name:string;
    ordered_quantity:string;received_quantity:string;recorded_received_quantity:string;receipt_link_discrepancy:string;
    invoiced_quantity:string;returned_quantity:string;uninvoiced_received_quantity:string;unreceived_quantity:string;overinvoiced_quantity:string;
    order_unit_price:string;expected_order_value:string;invoiced_net_amount:string;price_variance:string;
    receipt_numbers:string[];invoice_numbers:string[];
  }[];
};

export type LandedCostDocument = {
  id:number; company:number; source_invoice:number; source_invoice_number:string; supplier_name:string;
  document_number:string; document_date:string; status:"draft"|"posted"|"reversed"; total_amount:string;
  notes:string; allocation_lines:{
    id:number; source_invoice_line:number; source_description:string; expense_account:number;
    receipt_line:number; product:number; product_code:string; product_name:string; quantity:string;
    basis:"value"|"quantity"|"weight"|"volume"|"manual"; amount:string;
    distributions:{branch:number;branch_name:string;inventory_amount:string;cogs_amount:string;adjustment_loss_amount:string;journal_entry:number|null}[];
  }[]; created_at:string; posted_at:string|null; reversed_at:string|null;
};
export type LandedCostReport = {
  company:number|string; date_from:string|null; date_to:string|null; branch:number|null; supplier:number|null; product:number|null;
  totals:{allocated:string;inventory:string;cogs:string;adjustment_loss:string;reversed:string};
  rows:{
    document_id:number;document_number:string;document_date:string;document_status:string;
    source_invoice_id:number;source_invoice_number:string;supplier_id:number;supplier_name:string;
    expense_line_id:number;expense_description:string;expense_account_code:string;
    receipt_line_id:number;product_id:number;product_code:string;product_name:string;warehouse_id:number;
    branch_id:number;branch_name:string;allocation_basis:string;allocation_amount:string;
    source_line_capitalizable_amount:string;source_line_allocated_to_date:string;source_line_unallocated:string;
    inventory_amount:string;cogs_amount:string;adjustment_loss_amount:string;
    journal_entry:number|null;reversal_journal_entry:number|null;
  }[];
};

export type PurchaseReceipt = { id: number; company: number; supplier: number; supplier_name: string; warehouse: number; warehouse_name: string; receipt_number: string; receipt_date: string; supplier_delivery_ref: string; notes: string; movement_status: "draft" | "posted"; movement_number: string; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string; unit_cost: string; cost_amount: string; expiry_date: string | null }[]; created_at: string };
export type PurchaseInvoice = {
  id:number; company:number; supplier:number; supplier_name:string; activity:number|null;
  invoice_type:"inventory"|"direct_expense"; service_acceptance:number|null; service_acceptance_number:string|null;
  branch:number|null; branch_name:string|null; receipt:number|null; receipt_number:string|null; invoice_number:string; invoice_date:string;
  transaction_currency:number|null; exchange_rate:string|null; exchange_rate_date:string|null;
  transaction_net_total:string|null; transaction_tax_total:string|null; transaction_grand_total:string|null;
  status:"draft"|"posted"; net_total:string; tax_total:string; grand_total:string; paid_total:string; balance_due:string;
  notes:string; journal_entry:number|null; journal_number:string|null;
  invoice_lines:{
    id:number; receipt_line:number|null; product_code:string|null; product_name:string|null;
    description:string; expense_account:number|null; expense_account_name:string|null;
    cost_center:number|null; cost_center_name:string|null; quantity:string; billed_quantity:string;
    receipt_quantity:string|null; unit_price:string; transaction_unit_price:string|null;
    transaction_net_amount:string|null; transaction_tax_amount:string|null; transaction_total_amount:string|null;
    tax_rate:string; net_amount:string; tax_amount:string; total_amount:string;
  }[];
  created_at:string; posted_at:string|null;
};
export type Supplier = { id: number; company: number; code: string; supplier_type: "company" | "individual"; name_ar: string; name_en: string; tax_number: string; phone: string; mobile: string; email: string; address: string; credit_limit: string; payment_terms_days: number; notes: string; is_active: boolean; created_at: string; updated_at: string };
// قراءة رمز CSRF من ملفات تعريف الارتباط لاستخدامه في الطلبات التي تغيّر البيانات.
function csrfToken(): string { const item = document.cookie.split("; ").find((part) => part.startsWith("csrftoken=")); return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : ""; }
// عميل HTTP موحد: يرسل الطلب بنفس جلسة الدخول، يضيف CSRF، ويحوّل أخطاء الخادم إلى رسائل مفهومة.
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...(options.body && !isFormData ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}), ...options.headers } });
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
export type SupplierPayment = { id: number; company: number; supplier: number; supplier_name: string; invoice: number; invoice_number: string; payment_number: string; payment_date: string; method: "cash" | "bank"; payment_account: number | null; payment_account_name: string | null; amount: string; transaction_currency: number | null; transaction_amount: string | null; exchange_rate: string | null; exchange_rate_date: string | null; carrying_base_amount: string | null; historical_carrying_base_amount: string | null; realized_fx_gain: string; realized_fx_loss: string; reference: string; status: "draft" | "posted" | "reversed"; journal_entry: number | null; journal_number: string | null; reversal_journal_entry: number | null; created_at: string; posted_at: string | null };
export type SupplierReturn = {
  id: number; company: number; supplier: number; supplier_name: string; invoice: number; invoice_number: string;
  return_number: string; return_date: string; status: "draft" | "posted" | "reversed"; net_amount: string; tax_amount: string;
  grand_total: string; transaction_currency_code: string | null; transaction_exchange_rate: string | null;
  transaction_net_amount: string | null; transaction_tax_amount: string | null; transaction_grand_total: string | null;
  movement_status: "draft" | "posted"; journal_entry: number | null; journal_number: string | null;
  reversal_date: string | null; reversal_journal_entry: number | null; reversal_journal_number: string | null; reversal_movement_status: "draft" | "posted" | null;
  notes: string; return_lines: { id: number; invoice_line: number; product_code: string; product_name: string;
    quantity: string; net_amount: string; tax_amount: string; recoverable_tax_amount: string; nonrecoverable_tax_amount: string }[];
  created_at: string; posted_at: string | null;
};
export type SupplierRefund = {
  id: number; company: number; supplier: number; supplier_name: string; return_document: number; return_number: string;
  refund_number: string; refund_date: string; method: "cash" | "bank"; payment_account: number; payment_account_name: string;
  amount: string; reference: string; notes: string; status: "draft" | "posted" | "reversed"; journal_entry: number | null;
  journal_number: string | null; reversal_journal_entry: number | null; reversal_journal_number: string | null; reversal_date: string | null; reversed_at: string | null; reversed_by: number | null; created_at: string; posted_at: string | null;
};
export type SupplierStatement = {
  company: number; supplier: number; supplier_name: string; date_from: string | null; date_to: string | null;
  opening_balance: string; closing_balance: string;
  rows: { date: string; document_type: "invoice" | "payment" | "supplier_return" | "supplier_return_reversal" | "supplier_refund" | "asset_invoice" | "asset_payment"; document_number: string; description: string; debit: string; credit: string; balance: string }[];
};
export type SupplierAging = {
  company: number; as_of: string;
  totals: { not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string };
  suppliers: { supplier: number; supplier_code: string; supplier_name: string; not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string;
    invoices: { invoice_number: string; invoice_date: string; due_date: string; overdue_days: number; bucket: string; balance_due: string; document_type?: "asset_purchase_invoice"; asset_name?: string }[] }[];
};
// واجهات المشتريات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const purchasingApi = {
  requisitions: (companyId: number) => request<PurchaseRequisition[]>(`/api/purchasing/requisitions/?company=${companyId}&ordering=-request_date`),
  createRequisition: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRequisition>("/api/purchasing/requisitions/", { method: "POST", body: JSON.stringify(payload) }); },
  updateRequisition: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRequisition>(`/api/purchasing/requisitions/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  submitRequisition: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRequisition>(`/api/purchasing/requisitions/${id}/submit/`, { method: "POST" }); },
  reviewRequisition: async (id: number, decision: "approve" | "reject", note = "") => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRequisition>(`/api/purchasing/requisitions/${id}/review/`, { method: "POST", body: JSON.stringify({ decision, note }) }); },
  convertRequisition: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<{ requisition: PurchaseRequisition; purchase_order_id: number }>(`/api/purchasing/requisitions/${id}/convert/`, { method: "POST", body: JSON.stringify(payload) }); },
  cancelRequisition: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRequisition>(`/api/purchasing/requisitions/${id}/cancel/`, { method: "POST" }); },

  grniAging: (companyId:number, asOf:string) => request<GRNIAgingReport>(`/api/purchasing/receipts/grni-aging/?company=${companyId}&as_of=${asOf}`),
  landedCosts: (companyId:number) => request<LandedCostDocument[]>(`/api/purchasing/landed-costs/?company=${companyId}&ordering=-document_date`),
  createLandedCost: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<LandedCostDocument>("/api/purchasing/landed-costs/", {method:"POST",body:JSON.stringify(payload)}); },
  postLandedCost: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<LandedCostDocument>(`/api/purchasing/landed-costs/${id}/post/`, {method:"POST"}); },
  reverseLandedCost: async (id:number,entryDate:string) => { await request<{detail:string}>("/api/csrf/"); return request<LandedCostDocument>(`/api/purchasing/landed-costs/${id}/reverse/`, {method:"POST",body:JSON.stringify({entry_date:entryDate})}); },
  purchaseAnalysisReport: (companyId:number,filters:{date_from?:string;date_to?:string;branch?:number;supplier?:number;product?:number;activity?:number}={}) => {
    const params=new URLSearchParams({company:String(companyId)});
    Object.entries(filters).forEach(([key,value])=>{if(value!==undefined&&value!==null&&value!=="")params.set(key,String(value));});
    return request<PurchaseAnalysisReport>(`/api/purchasing/invoices/analysis-report/?${params.toString()}`);
  },
  purchaseMatchingReport: (companyId:number,filters:{date_from?:string;date_to?:string;branch?:number;supplier?:number;product?:number}={}) => {
    const params=new URLSearchParams({company:String(companyId)});
    Object.entries(filters).forEach(([key,value])=>{if(value!==undefined&&value!==null&&value!=="")params.set(key,String(value));});
    return request<PurchaseMatchingReport>(`/api/purchasing/orders/matching-report/?${params.toString()}`);
  },
  landedCostReport: (companyId:number,filters:{date_from?:string;date_to?:string;branch?:number;supplier?:number;product?:number}={}) => {
    const params=new URLSearchParams({company:String(companyId)});
    Object.entries(filters).forEach(([key,value])=>{if(value!==undefined&&value!==null&&value!=="")params.set(key,String(value));});
    return request<LandedCostReport>(`/api/purchasing/landed-costs/report/?${params.toString()}`);
  },
  rfqs: (companyId: number) => request<PurchaseRFQ[]>(`/api/purchasing/rfqs/?company=${companyId}&ordering=-issue_date`),
  quotations: (companyId: number) => request<SupplierQuotation[]>(`/api/purchasing/quotations/?rfq__company=${companyId}&ordering=-quotation_date`),
  createRFQ: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseRFQ>("/api/purchasing/rfqs/", { method:"POST", body:JSON.stringify(payload) }); },
  openRFQ: async (id:number) => { await request<{ detail:string }>("/api/csrf/"); return request<PurchaseRFQ>(`/api/purchasing/rfqs/${id}/open/`, {method:"POST"}); },
  closeRFQ: async (id:number) => { await request<{ detail:string }>("/api/csrf/"); return request<PurchaseRFQ>(`/api/purchasing/rfqs/${id}/close/`, {method:"POST"}); },
  awardRFQ: async (id:number, quotation:number, reason:string) => { await request<{ detail:string }>("/api/csrf/"); return request<PurchaseRFQ>(`/api/purchasing/rfqs/${id}/award/`, {method:"POST",body:JSON.stringify({quotation,reason})}); },
  convertAward: async (id:number,payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<{rfq:PurchaseRFQ;purchase_order_id:number}>(`/api/purchasing/rfqs/${id}/convert-award/`,{method:"POST",body:JSON.stringify(payload)}); },
  createQuotation: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SupplierQuotation>("/api/purchasing/quotations/", {method:"POST",body:JSON.stringify(payload)}); },
  serviceAcceptances: (companyId: number) => request<PurchaseServiceAcceptance[]>(`/api/purchasing/service-acceptances/?company=${companyId}&ordering=-acceptance_date`),
  createServiceAcceptance: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseServiceAcceptance>("/api/purchasing/service-acceptances/", { method: "POST", body: JSON.stringify(payload) }); },
  acceptServiceAcceptance: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseServiceAcceptance>(`/api/purchasing/service-acceptances/${id}/accept/`, { method: "POST" }); },

  // جلب أوامر الشراء مرتبة حسب تاريخ الأمر للشركة المحددة.
  orders: (companyId: number) => request<PurchaseOrder[]>(`/api/purchasing/orders/?company=${companyId}&ordering=-order_date`),
  // إنشاء أمر شراء جديد وإرسال بياناته إلى الخادم.
  createOrder: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>("/api/purchasing/orders/", { method: "POST", body: JSON.stringify(payload) }); },
  // تأكيد أمر الشراء المحدد للانتقال إلى مرحلة التنفيذ.
  confirmOrder: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder | {status:"pending_approval";detail:string;approval_request_id:number;order:PurchaseOrder}>(`/api/purchasing/orders/${id}/confirm/`, { method: "POST" }); },
  // إلغاء أمر شراء مسودة أو معتمد قبل تسجيل أي استلام.
  cancelOrder: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseOrder>(`/api/purchasing/orders/${id}/cancel/`, { method: "POST" }); },

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
  // جلب مرتجعات الموردين للشركة.
  returns: (companyId: number) => request<SupplierReturn[]>(`/api/purchasing/returns/?company=${companyId}&ordering=-return_date`),
  // إنشاء مسودة مرتجع مورد مرتبطة ببنود فاتورة المورد الأصلية.
  createReturn: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierReturn>("/api/purchasing/returns/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل المرتجع وتحديث المخزون والمورد والضريبة.
  postReturn: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierReturn>(`/api/purchasing/returns/${id}/post/`, { method: "POST" }); },
  // جلب استردادات الموردين.
  refunds: (companyId: number) => request<SupplierRefund[]>(`/api/purchasing/refunds/?company=${companyId}&ordering=-refund_date`),
  createRefund: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierRefund>("/api/purchasing/refunds/", { method: "POST", body: JSON.stringify(payload) }); },
  postRefund: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierRefund>(`/api/purchasing/refunds/${id}/post/`, { method: "POST" }); },
  reverseRefund: async (id:number, entryDate?:string) => { await request<{detail:string}>("/api/csrf/"); return request<SupplierRefund>(`/api/purchasing/refunds/${id}/reverse/`, {method:"POST",body:JSON.stringify(entryDate?{entry_date:entryDate}:{})}); },
  // عكس مرتجع مرحّل بإرجاع طبقات التكلفة الأصلية وإنشاء قيد عكسي.
  reverseReturn: async (id: number, entryDate?: string) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierReturn>(`/api/purchasing/returns/${id}/reverse/`, { method: "POST", body: JSON.stringify(entryDate ? { entry_date: entryDate } : {}) }); },
  // إنشاء مستند استلام مشتريات جديد.
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>("/api/purchasing/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل سند القبض وتحديث رصيد العميل والخزينة.
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseReceipt>(`/api/purchasing/receipts/${id}/post/`, { method: "POST" }); },
  // إنشاء فاتورة مورد وربطها ببيانات الاستلام.
  createInvoice: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>("/api/purchasing/invoices/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل فاتورة البيع وتثبيت الإيراد والضريبة والذمم.
  postInvoice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<PurchaseInvoice>(`/api/purchasing/invoices/${id}/post/`, { method: "POST" }); },
  // إعادة تقييم رصيد فاتورة المورد الأجنبية المفتوح.
  revalueInvoice: async (id:number,payload:{revaluation_date:string;rate_date:string;rate:string}) => { await request<{detail:string}>("/api/csrf/"); return request<FxSubledgerRevaluation>(`/api/purchasing/invoices/${id}/revalue/`,{method:"POST",body:JSON.stringify(payload)}); },
  fxRevaluations: (id:number) => request<FxSubledgerRevaluation[]>(`/api/purchasing/invoices/${id}/revaluations/`),
  // إنشاء دفعة مورد جديدة.
  createPayment: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>("/api/purchasing/payments/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل دفعة المورد وتسجيل أثرها المحاسبي.
  postPayment: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>(`/api/purchasing/payments/${id}/post/`, { method: "POST" }); },
  // عكس آخر سند صرف مرحّل مع إنشاء قيد عكسي مرتبط.
  reversePayment: async (id: number, entryDate?: string) => { await request<{ detail: string }>("/api/csrf/"); return request<SupplierPayment>(`/api/purchasing/payments/${id}/reverse/`, { method: "POST", body: JSON.stringify(entryDate ? { entry_date: entryDate } : {}) }); },
};


export type CashCommitmentPayment = {
  id:number; payment_date:string; amount:string; base_amount:string; journal_entry:number;
  journal_number:string; reversal_journal_entry:number|null; reversal_journal_number:string; reference:string; notes:string; created_at:string;
};
export type CashCommitment = {
  id:number; company:number; company_name:string; branch:number|null; branch_name:string;
  commitment_type:"tax"|"recurring_expense"|"friday_overtime"|"other"; title:string; period_label:string;
  amount:string; currency_code:string; exchange_rate:string; base_amount:string; due_date:string;
  payable_account:number; payable_account_code:string; payable_account_name:string;
  origin_type:string; origin_id:number|null; status:"open"|"partially_paid"|"paid"|"void";
  notes:string; paid_base_amount:string; remaining_base_amount:string; payments:CashCommitmentPayment[];
  created_at:string; updated_at:string;
};
export type CashCommitmentSchedule = {
  id:number; company:number; company_name:string; branch:number|null; title:string; amount:string;
  currency_code:string; exchange_rate:string; base_amount:string;
  frequency:"monthly"|"quarterly"|"semiannual"|"annual"; interval_months:number; anchor_day:number;
  next_due_date:string; end_date:string|null; payable_account:number; payable_account_code:string;
  active:boolean; notes:string; created_at:string; updated_at:string;
};
export type TreasuryLiquiditySummary = {
  company:number; as_of:string; base_currency:string; branch:number|null; scope:string;
  total_base_balance:string; note:string;
  accounts:{id:number;code:string;name:string;kind:"cash"|"bank";branch_id:number|null;branch_name:string;
    currency:string;balance:string;foreign_amount:string|null;base_balance:string;is_foreign:boolean}[];
};

export type TreasuryLiquidityForecast = {
  company:number; as_of:string; horizon_days:number; end_date:string; base_currency:string;
  branch:number|null; actual_cash_balance:string; expected_collections:string;
  expected_supplier_payments:string; expected_payroll:string; expected_other_commitments:string; expected_payments:string;
  expected_net:string; projected_closing_balance:string; note:string; coverage_notes:string[];
  daily:{date:string;collections:string;supplier_payments:string;payroll:string;other_commitments:string;payments:string;net:string;projected_balance:string}[];
  items:{kind:"collections"|"supplier_payments"|"payroll"|"other_commitments";document:string;party:string;due_date:string;forecast_date:string;balance:string;status:string;date_basis:string}[];
};
export type TreasuryAccount = {
  id: number; company: number; branch: number | null; branch_name: string; account: number;
  account_code: string; account_name: string; kind: "cash" | "bank"; code: string; name_ar: string;
  bank_name: string; account_number: string; iban: string; currency_code: string;
  opening_balance: string; opening_foreign_amount: string; opening_exchange_rate: string | null; opening_exchange_rate_date: string | null;
  fx_position_foreign_amount: string; fx_position_carrying_base_amount: string; is_active: boolean;
};
export type FxSubledgerRevaluation = {
  id:number; revaluation_date:string; rate_date:string; rate:string; foreign_balance:string;
  previous_carrying_amount:string; new_carrying_amount:string; difference:string;
  journal_entry:number|null; journal_number:string|null; status:"posted"|"reversed"|"no_difference";
};
export type TreasuryFxRevaluation = {
  id:number; account:number; currency:string; revaluation_date:string; rate_date:string; rate:string;
  foreign_amount:string; previous_carrying_amount:string; new_carrying_amount:string; difference:string;
  journal_entry:number|null; journal_number:string; status:"posted"|"reversed"|"no_difference";
  can_reverse:boolean; reversal_journal_entry:number|null; reversal_journal_number:string;
  reversed_at:string|null; reversed_by_name:string;
};
export type TreasuryTransaction = {
  id: number; company: number; branch: number; fiscal_year: number; source_account: number; source_name: string;
  destination_account: number | null; destination_name: string; counter_account: number | null; counter_account_code: string;
  kind: "receipt" | "payment" | "transfer"; status: "draft" | "posted" | "void" | "reversed"; document_number: string;
  transaction_date: string; amount: string; description: string; reference: string; journal_entry: number | null;
  journal_number: string; reversal_journal_entry: number | null; reversal_journal_number: string; reversed_at: string | null; reversed_by: number | null; transaction_currency: number|null; transaction_amount: string|null; exchange_rate: string|null; exchange_rate_date: string|null; carrying_amount: string|null; cash_flow_category: "operating" | "investing" | "financing"; created_at: string; posted_at: string | null;
};
export type TreasuryCheque = {
  id:number; company:number; branch:number; fiscal_year:number; bank_account:number; bank_name:string;
  direction:"incoming"|"outgoing"; cheque_number:string; counterparty:string; amount:string;
  issue_date:string; due_date:string; status:"received"|"deposited"|"under_collection"|"issued"|"presented"|"cleared"|"bounced"|"cancelled";
  treasury_transaction:number|null; transaction_number:string; deposit_date:string|null; clearing_date:string|null;
  bounce_date:string|null; notes:string; created_at:string; updated_at:string;
};
export type CashFlowReport = {
  company: number; date_from: string | null; date_to: string | null; net_cash_flow: string; note: string;
  categories: {category:"operating"|"investing"|"financing";label:string;inflows:string;outflows:string;net:string}[];
  rows: {id:number;date:string;document_number:string;description:string;branch:string;category:string;kind:string;amount:string;signed_amount:string;treasury_account:string;journal_entry:number|null}[];
};
export type DirectCashFlowReport = {
  company: number; branch: number | null; date_from: string; date_to: string; method: "direct";
  sections: { category: "operating"|"investing"|"financing"; label: string; cash_receipts: string; cash_payments: string; net_cash_flow: string;
    details: { counter_account_id: number|null; counter_account_code: string|null; counter_account_name: string; kind: "receipt"|"payment"; inflows: string; outflows: string }[] }[];
  operating_cash_flow: string; investing_cash_flow: string; financing_cash_flow: string;
  opening_cash_balance: string; closing_cash_balance: string; ledger_closing_cash_balance: string;
  closing_balance_difference: string; net_change_in_cash: string; effect_of_exchange_rate_changes: string;
  net_increase_decrease_in_cash: string; ledger_cash_change: string; reconciliation_difference: string;
  exchange_rate_effect_details: {account_id:number;account_code:string;account_name:string;currency:string;
    event_type:"revaluation"|"reversal";event_date:string;original_revaluation_date:string;rate:string;
    foreign_amount:string;difference:string;journal_number:string;original_journal_number:string}[];
  uncategorized_counterparty_transactions: {id:number;transaction_date:string;document_number:string;kind:string;amount:string}[];
  non_cash_investing_transactions: {document_type:string;invoice_number:string;asset_name:string;supplier_name:string;recognition_date:string;gross_amount:string;cash_paid_during_period:string;unpaid_at_period_end:string}[];
  note: string;
};
export type IndirectCashFlowReport = {
  company: number; branch: number | null; date_from: string; date_to: string;
  net_profit: string; non_cash_adjustments: { depreciation: string; disposal_loss: string; disposal_gain: string; net: string };
  working_capital: { role: string; account_code: string; account_name: string; opening_balance: string; closing_balance: string; change: string; cash_flow_adjustment: string }[];
  missing_account_roles: string[]; operating_adjustments: string; operating_cash_flow: string;
  investing_cash_flow: string; financing_cash_flow: string; net_change_in_cash: string;
  effect_of_exchange_rate_changes: string; net_increase_decrease_in_cash: string;
  exchange_rate_effect_details: {account_id:number;account_code:string;account_name:string;currency:string;
    event_type:"revaluation"|"reversal";event_date:string;original_revaluation_date:string;rate:string;
    foreign_amount:string;difference:string;journal_number:string;original_journal_number:string}[];
  opening_cash_balance: string; closing_cash_balance: string; ledger_closing_cash_balance: string;
  closing_balance_difference: string; ledger_cash_change: string; reconciliation_difference: string;
  investing_transactions: { id: number; transaction_date: string; document_number: string; description: string; kind: string; amount: string }[];
  financing_transactions: { id: number; transaction_date: string; document_number: string; description: string; kind: string; amount: string }[];
  note: string;
};

export type PettyCashFund = {
  id:number; company:number; company_name:string; branch:number; branch_name:string; code:string; name:string;
  custodian:number; custodian_name:string; funding_account:number; funding_account_name:string;
  petty_cash_account:number; petty_cash_account_name:string; authorized_amount:string;
  status:"active"|"suspended"|"closed"; created_at:string; updated_at:string;
};
export type PettyCashExpense = {
  id:number; fund:number; fund_name:string; expense_date:string; description:string; amount:string;
  expense_account:number; expense_account_code:string; expense_account_name:string;
  receipt_number:string; receipt_file:string|null; vendor_name:string;
  status:"draft"|"pending_approval"|"approved"|"rejected"|"settled"|"void";
  settlement:number|null; settlement_journal_number:string; submitted_at:string|null;
  approved_by:number|null; approved_at:string|null; approval_note:string;
  rejected_by:number|null; rejected_at:string|null; rejection_note:string;
  created_at:string; updated_at:string;
};
export type PettyCashCount = {
  id:number; fund:number; count_date:string; expected_balance:string; actual_balance:string;
  variance:string; notes:string; status:"pending"|"approved"|"rejected";
  counted_by:number; counted_by_name:string; reviewed_by:number|null; reviewed_at:string|null;
  review_note:string; adjustment_journal_entry:number|null; created_at:string;
};
export type PettyCashCustodianHandover = {
  id:number; fund:number; previous_custodian:number; previous_custodian_name:string;
  new_custodian:number; new_custodian_name:string; handover_date:string;
  expected_balance:string; actual_balance:string; notes:string; handed_over_by:number;
  handed_over_by_name:string; created_at:string;
};
export type PettyCashStatement = {
  fund:number; fund_code:string; fund_name:string; branch:number; branch_name:string;
  date_from:string; date_to:string; opening_balance:string; total_inflows:string;
  total_outflows:string; net_movement:string; closing_balance:string;
  reconciliation_difference:string;
  rows:{date:string;type:string;description:string;document_number:string;journal_number:string;
    account_code:string;account_name:string;amount:string;reference_id:number}[];
};
export type PettyCashExpenseReport = {
  company:number; branch:number|null; date_from:string|null; date_to:string|null;
  total_expenses:string;
  by_branch_account:{fund__branch_id:number;fund__branch__name_ar:string;
    expense_account_id:number;expense_account__code:string;expense_account__name_ar:string;
    total_amount:string;expense_count:number}[];
  unsettled_count:number; unsettled_total:string;
  unsettled_expenses:{id:number;fund:number;fund_name:string;branch:number;branch_name:string;
    expense_date:string;description:string;expense_account:number;expense_account_code:string;
    expense_account_name:string;amount:string;status:string;receipt_number:string}[];
};

// واجهات الخزينة والتدفقات النقدية: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const treasuryApi = {
  // جلب حسابات الخزينة والبنوك للشركة.
  accounts: (companyId: number) => request<TreasuryAccount[]>(`/api/treasury/accounts/?company=${companyId}&ordering=kind,code`),
  // جلب المركز النقدي الدفتري الحالي مع عرض الأرصدة الأجنبية الاسمية والقيم الأساسية.
  liquiditySummary: (companyId:number, asOf?:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId)});
    if(asOf) q.set("as_of",asOf);
    if(branch) q.set("branch",String(branch));
    return request<TreasuryLiquiditySummary>(`/api/treasury/accounts/liquidity-summary/?${q.toString()}`);
  },
  // توقع التدفقات من الذمم المفتوحة ودورات المرتبات المعتمدة مع فصلها عن الرصيد الفعلي.
  liquidityForecast: (companyId:number, days=90, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),days:String(days)});
    if(branch) q.set("branch",String(branch));
    return request<TreasuryLiquidityForecast>(`/api/treasury/accounts/liquidity-forecast/?${q.toString()}`);
  },
  // جلب حركات الخزينة مرتبة من الأحدث.
  transactions: (companyId: number) => request<TreasuryTransaction[]>(`/api/treasury/transactions/?company=${companyId}&ordering=-transaction_date`),
  commitments: (companyId:number) => request<CashCommitment[]>(`/api/treasury/commitments/?company=${companyId}&ordering=due_date`),

  pettyCashFunds: (companyId:number) => request<PettyCashFund[]>(`/api/treasury/petty-cash-funds/?company=${companyId}`),
  pettyCashExpenses: (companyId:number) => request<PettyCashExpense[]>(`/api/treasury/petty-cash-expenses/?company=${companyId}`),
  pettyCashStatement: (id:number,dateFrom:string,dateTo:string) => request<PettyCashStatement>(`/api/treasury/petty-cash-funds/${id}/statement/?date_from=${dateFrom}&date_to=${dateTo}`),
  pettyCashExpenseReport: (companyId:number,dateFrom:string,dateTo:string,branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),date_from:dateFrom,date_to:dateTo});
    if(branch) q.set("branch",String(branch));
    return request<PettyCashExpenseReport>(`/api/treasury/petty-cash-expenses/report/?${q.toString()}`);
  },
  createPettyCashFund: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashFund>("/api/treasury/petty-cash-funds/",{method:"POST",body:JSON.stringify(payload)}); },
  createPettyCashExpense: async (payload:FormData) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashExpense>("/api/treasury/petty-cash-expenses/",{method:"POST",body:payload}); },
  updatePettyCashExpense: async (id:number,payload:FormData) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashExpense>(`/api/treasury/petty-cash-expenses/${id}/`,{method:"PATCH",body:payload}); },
  submitPettyCashExpense: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashExpense>(`/api/treasury/petty-cash-expenses/${id}/submit/`,{method:"POST",body:"{}"}); },
  approvePettyCashExpense: async (id:number,approval_note="") => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashExpense>(`/api/treasury/petty-cash-expenses/${id}/approve/`,{method:"POST",body:JSON.stringify({approval_note})}); },
  rejectPettyCashExpense: async (id:number,rejection_note:string) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashExpense>(`/api/treasury/petty-cash-expenses/${id}/reject/`,{method:"POST",body:JSON.stringify({rejection_note})}); },
  pettyCashCounts: (id:number) => request<PettyCashCount[]>(`/api/treasury/petty-cash-funds/${id}/counts/`),
  pettyCashHandovers: (id:number) => request<PettyCashCustodianHandover[]>(`/api/treasury/petty-cash-funds/${id}/handovers/`),
  approvePettyCashCount: async (id:number,count_id:number,counter_account?:number,review_note="") => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashCount>(`/api/treasury/petty-cash-funds/${id}/approve-count/`,{method:"POST",body:JSON.stringify({count_id,counter_account,review_note})}); },
  rejectPettyCashCount: async (id:number,count_id:number,review_note:string) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashCount>(`/api/treasury/petty-cash-funds/${id}/reject-count/`,{method:"POST",body:JSON.stringify({count_id,review_note})}); },
  handoverPettyCash: async (id:number,payload:{new_custodian:number;handover_date:string;actual_balance:string;notes?:string}) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashCustodianHandover>(`/api/treasury/petty-cash-funds/${id}/handover/`,{method:"POST",body:JSON.stringify(payload)}); },
  closePettyCash: async (id:number,actual_balance:string) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashFund & {returned_amount:string;book_balance_after_close:string}>(`/api/treasury/petty-cash-funds/${id}/close/`,{method:"POST",body:JSON.stringify({actual_balance})}); },
  pettyCashBalance: (id:number) => request<{fund:number;as_of:string;authorized_amount:string;book_balance:string;unsettled_expenses:string;available_for_expenses:string}>(`/api/treasury/petty-cash-funds/${id}/balance/`),
  fundPettyCash: async (id:number,amount:string,transaction_date:string) => { await request<{detail:string}>("/api/csrf/"); return request<{transfer_id:number;journal_entry:number;amount:string}>(`/api/treasury/petty-cash-funds/${id}/fund/`,{method:"POST",body:JSON.stringify({amount,transaction_date})}); },
  settlePettyCash: async (id:number,expense_ids:number[],settlement_date:string) => { await request<{detail:string}>("/api/csrf/"); return request<{settlement_id:number;journal_entry:number;journal_number:string;total_amount:string}>(`/api/treasury/petty-cash-funds/${id}/settle/`,{method:"POST",body:JSON.stringify({expense_ids,settlement_date})}); },
  replenishPettyCash: async (id:number,amount:string,transaction_date:string) => { await request<{detail:string}>("/api/csrf/"); return request<{transfer_id:number;journal_entry:number;amount:string}>(`/api/treasury/petty-cash-funds/${id}/replenish/`,{method:"POST",body:JSON.stringify({amount,transaction_date})}); },
  countPettyCash: async (id:number,payload:{count_date:string;actual_balance:string;notes?:string}) => { await request<{detail:string}>("/api/csrf/"); return request<PettyCashCount>(`/api/treasury/petty-cash-funds/${id}/count/`,{method:"POST",body:JSON.stringify(payload)}); },

  createCommitment: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitment>("/api/treasury/commitments/",{method:"POST",body:JSON.stringify(payload)}); },
  updateCommitment: async (id:number,payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitment>(`/api/treasury/commitments/${id}/`,{method:"PATCH",body:JSON.stringify(payload)}); },
  voidCommitment: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitment>(`/api/treasury/commitments/${id}/void/`,{method:"POST"}); },
  recordCommitmentPayment: async (id:number,payload:{payment_date:string;amount:string;journal_entry:number;reference?:string;notes?:string}) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitmentPayment>(`/api/treasury/commitments/${id}/record-payment/`,{method:"POST",body:JSON.stringify(payload)}); },
  reverseCommitmentPayment: async (id:number,paymentId:number,reversalJournalEntry:number) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitmentPayment>(`/api/treasury/commitments/${id}/reverse-payment/`,{method:"POST",body:JSON.stringify({payment_id:paymentId,reversal_journal_entry:reversalJournalEntry})}); },
  commitmentSchedules: (companyId:number) => request<CashCommitmentSchedule[]>(`/api/treasury/commitment-schedules/?company=${companyId}`),
  createCommitmentSchedule: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitmentSchedule>("/api/treasury/commitment-schedules/",{method:"POST",body:JSON.stringify(payload)}); },
  setCommitmentScheduleActive: async (id:number,active:boolean) => { await request<{detail:string}>("/api/csrf/"); return request<CashCommitmentSchedule>(`/api/treasury/commitment-schedules/${id}/`,{method:"PATCH",body:JSON.stringify({active})}); },
  generateCommitmentSchedule: async (id:number,throughDate:string) => { await request<{detail:string}>("/api/csrf/"); return request<{generated:number;next_due_date:string;active:boolean}>(`/api/treasury/commitment-schedules/${id}/generate/`,{method:"POST",body:JSON.stringify({through_date:throughDate})}); },
  // جلب سجل الشيكات الواردة والصادرة للشركة.
  cheques: (companyId:number) => request<TreasuryCheque[]>(`/api/treasury/cheques/?company=${companyId}&ordering=due_date`),
  // تسجيل شيك جديد.
  createCheque: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryCheque>("/api/treasury/cheques/",{method:"POST",body:JSON.stringify(payload)}); },
  // تنفيذ إجراء دورة الشيك.
  chequeAction: async (id:number,action:"deposit"|"under_collection"|"present"|"clear"|"bounce"|"cancel",payload:Record<string,unknown>={}) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryCheque>(`/api/treasury/cheques/${id}/${action}/`,{method:"POST",body:JSON.stringify(payload)}); },
  // إنشاء حساب خزينة أو بنك جديد.
  createAccount: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryAccount>("/api/treasury/accounts/", {method:"POST", body:JSON.stringify(payload)}); },
  // إنشاء حركة قبض أو صرف أو تحويل.
  createTransaction: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>("/api/treasury/transactions/", {method:"POST", body:JSON.stringify(payload)}); },
  // جلب سجل إعادة تقييم العملة الأجنبية للحساب المحدد.
  fxRevaluations: (id:number) => request<TreasuryFxRevaluation[]>(`/api/treasury/accounts/${id}/revaluations/`),
  // إعادة تقييم مركز عملة أجنبية وإنشاء قيد الفروق غير المحققة.
  revalueAccount: async (id:number, payload:{fiscal_year:number;revaluation_date:string;rate_date:string}) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryFxRevaluation>(`/api/treasury/accounts/${id}/revalue/`, {method:"POST", body:JSON.stringify(payload)}); },
  // عكس إعادة تقييم الخزينة مع قيد عكسي واستعادة القيمة الدفترية.
  reverseRevaluation: async (accountId:number,revaluationId:number,entryDate?:string) => { await request<{detail:string}>("/api/csrf/"); return request<{id:number;status:"reversed";reversal_journal_number:string}>(`/api/treasury/accounts/${accountId}/reverse-revaluation/`,{method:"POST",body:JSON.stringify({revaluation_id:revaluationId,...(entryDate?{entry_date:entryDate}:{})})}); },
  // ترحيل حركة الخزينة المحددة.
  postTransaction: async (id: number) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>(`/api/treasury/transactions/${id}/post/`, {method:"POST"}); },
  reverseTransaction: async (id:number,entryDate?:string) => { await request<{detail:string}>("/api/csrf/"); return request<TreasuryTransaction>(`/api/treasury/transactions/${id}/reverse/`, {method:"POST",body:JSON.stringify(entryDate?{entry_date:entryDate}:{})}); },
  // جلب تقرير التدفقات النقدية بالطريقة غير المباشرة مع فلتر الفرع.
  directCashFlow: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => {
    const q = new URLSearchParams({company:String(companyId), date_from:dateFrom, date_to:dateTo});
    if (branch) q.set("branch", String(branch));
    return request<DirectCashFlowReport>(`/api/accounting/journal-entries/direct-cash-flow/?${q.toString()}`);
  },
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

export type ExpenseAnalysisReport = {
  company:number; date_from:string; date_to:string; currency:string;
  total:string; direct_total:string; operating_total:string;
  rows:{account_id:number;code:string;name_ar:string;branch_id:number;branch_code:string;branch_name:string;cost_center_id:number|null;cost_center_code:string;cost_center_name:string;debit:string;credit:string;amount:string;category:"direct_cost"|"operating_expense"}[];
  monthly_rows:{month:string;amount:string}[];
  note:string;
};
export type CostCenter = {id:number;company:number;code:string;name_ar:string;parent:number|null;is_active:boolean};
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
  pagination:{page:number;page_size:number;total_rows:number;total_pages:number;has_next:boolean;has_previous:boolean};
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
  tax_rate: string; taxable_base: string; tax_amount: string; recoverable_tax_amount: string; gross_amount: string; line_count: number;
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
  ledger_reconciliation: {
    output_tax_from_invoices: string; output_tax_gl_net_credit: string; output_tax_difference: string;
    recoverable_input_tax_from_invoices: string; input_tax_gl_net_debit: string; input_tax_difference: string;
    invoice_net_tax_payable: string; ledger_net_tax_payable: string; net_tax_difference: string;
    output_account_configured: boolean; input_account_configured: boolean;
  };
  unclassified_tax: { sales_amount: string; sales_groups: number; purchase_amount: string; purchase_groups: number; purchase_recoverable_amount: string };
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
  // جلب تقرير مطابقة دفاتر العملاء والموردين والمرتبات والمخزون مع الأستاذ العام.
  subledgerReconciliation: (companyId:number, asOf:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),as_of:asOf});
    if (branch) q.set("branch",String(branch));
    return request<SubledgerReconciliation>(`/api/accounting/subledger-reconciliation/?${q.toString()}`);
  },
  // جلب سجل التدقيق مع فلاتر الشركة والفترة ونوع الإجراء.
  auditEvents: (companyId: number, dateFrom: string, dateTo: string, action = "", limit = 50, offset = 0) => {
    const q = new URLSearchParams({ company: String(companyId), date_from: dateFrom, date_to: dateTo, limit: String(limit), offset: String(offset) });
    if (action) q.set("action", action);
    return request<AuditEventResponse>(`/api/audit/events/?${q.toString()}`);
  },
  // جلب أكواد الضرائب المعرفة للشركة.
  taxCodes: (companyId: number) => request<TaxCode[]>(`/api/accounting/tax-codes/?company=${companyId}&ordering=code`),
  // جلب مراكز التكلفة مع ترتيبها حسب الكود.
  allCostCenters: (companyId: number) => request<CostCenter[]>(`/api/accounting/cost-centers/?company=${companyId}&ordering=code`),
  // إنشاء مركز تكلفة جديد.
  createCostCenter: async (payload: Partial<CostCenter>) => { await request<{ detail: string }>("/api/csrf/"); return request<CostCenter>("/api/accounting/cost-centers/", { method: "POST", body: JSON.stringify(payload) }); },
  // تحديث بيانات مركز تكلفة أو حالته دون حذف السجل.
  updateCostCenter: async (id: number, payload: Partial<CostCenter>) => { await request<{ detail: string }>("/api/csrf/"); return request<CostCenter>(`/api/accounting/cost-centers/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // إضافة كود ضريبي جديد للشركة.
  createTaxCode: async (payload: Partial<TaxCode>) => { await request<{ detail: string }>("/api/csrf/"); return request<TaxCode>("/api/accounting/tax-codes/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء أكواد ضريبة القيمة المضافة الابتدائية حسب بلد الشركة دون تعديل الأكواد الموجودة.
  initializeTaxPresets: async (companyId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<{country:"EG"|"SA";country_label:string;created:{id:number;code:string;rate:string;is_active:boolean}[];existing:{id:number;code:string;rate:string;is_active:boolean}[];note:string}>("/api/accounting/tax-codes/initialize-country-presets/", {method:"POST",body:JSON.stringify({company:companyId})}); },
  // تعطيل الكود الضريبي دون حذف السجل التاريخي.
  deactivateTaxCode: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<TaxCode>(`/api/accounting/tax-codes/${id}/`, { method: "PATCH", body: JSON.stringify({ is_active: false }) }); },
  // جلب العملات النشطة المتاحة في النظام.
  currencies: () => request<Currency[]>("/api/setup/currencies/?is_active=true"),
  // جلب الشركات المتاحة للمستخدم.
  companies: () => request<Company[]>("/api/setup/companies/"),
  // إنشاء سجل شركة من شاشة إعداد النظام؛ الخادم يطبق صلاحية الإنشاء المناسبة.
  createCompany: async (payload: Record<string, unknown>) => { await request<{ detail: string }>("/api/csrf/"); return request<Company>("/api/setup/companies/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء الشركة والفرع الرئيسي وحساب مديرها وربطهما ذريًا في طلب واحد.
  createCompanyWithAdmin: async (payload: { company: Record<string, unknown>; manager: { username: string; display_name: string; email: string; password: string } }) => { await request<{ detail: string }>("/api/csrf/"); return request<{ company: Company; manager: CompanyMembership }>("/api/setup/companies/create-with-admin/", { method: "POST", body: JSON.stringify(payload) }); },
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
  // نقل صفة المركز الرئيسي إلى فرع نشط داخل الشركة نفسها.
  makeHeadOffice: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<Branch>(`/api/setup/branches/${id}/make-head-office/`, { method: "POST", body: JSON.stringify({}) }); },
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
  // تعديل بيانات سنة مالية قبل تسجيل أي قيود عليها.
  updateFiscalYear: async (id: number, payload: Partial<Pick<FiscalYear, "name" | "start_date" | "end_date">>) => { await request<{ detail: string }>("/api/csrf/"); return request<FiscalYear>(`/api/setup/fiscal-years/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // مراجعة جاهزية الإقفال دون إجراء أي تغيير على البيانات.
  fiscalYearClosingReview: (id: number) => request<FiscalYearClosingReview>(`/api/setup/fiscal-years/${id}/closing_review/`),
  // تنفيذ الإقفال بعد اجتياز ضوابط الخادم.
  closeFiscalYear: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<{ detail: string; review: FiscalYearClosingReview }>(`/api/setup/fiscal-years/${id}/close/`, { method: "POST", body: JSON.stringify({}) }); },
  // جلب الفترات الشهرية التابعة للسنة المالية.
  fiscalPeriods: (id:number) => request<FiscalPeriod[]>(`/api/setup/fiscal-years/${id}/periods/`),
  // إنشاء الفترات الشهرية الناقصة وفق حدود السنة المالية.
  generateFiscalPeriods: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<{detail:string;created:number;periods:number}>(`/api/setup/fiscal-years/${id}/generate-periods/`, {method:"POST",body:JSON.stringify({})}); },
  // إقفال فترة شهرية بعد التحقق من عدم وجود قيود مسودة.
  closeFiscalPeriod: async (id:number, period_id:number) => { await request<{detail:string}>("/api/csrf/"); return request<{detail:string;period_id:number}>(`/api/setup/fiscal-years/${id}/close-period/`, {method:"POST",body:JSON.stringify({period_id})}); },
  // إعادة فتح فترة شهرية مع تسجيل سبب إلزامي.
  reopenFiscalPeriod: async (id:number, period_id:number, reason:string) => { await request<{detail:string}>("/api/csrf/"); return request<{detail:string;period_id:number}>(`/api/setup/fiscal-years/${id}/reopen-period/`, {method:"POST",body:JSON.stringify({period_id,reason})}); },
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
  journals: (companyId: number, page = 1, search = "") => {
    const q = new URLSearchParams({ company: String(companyId), ordering: "-entry_date", page: String(page), page_size: "100" });
    if (search.trim()) q.set("search", search.trim());
    return request<ApiPage<Journal>>(`/api/accounting/journal-entries/?${q.toString()}`);
  },
  journalSummary: (companyId: number) => request<JournalSummary>(`/api/accounting/journal-entries/summary/?company=${companyId}`),
  // جلب تفاصيل قيد يومية محدد بما فيها سطوره.
  journalDetail: (id: number) => request<Journal>(`/api/accounting/journal-entries/${id}/`),
  // جلب تقرير مطابقة ضريبة القيمة المضافة للفترة والفرع.
  vatReturn: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => {
    const q = new URLSearchParams({ company: String(companyId), date_from: dateFrom, date_to: dateTo });
    if (branch) q.set("branch", String(branch));
    return request<VATReturnReport>(`/api/accounting/journal-entries/vat-return/?${q.toString()}`);
  },
  // جلب تحليل المصروفات حسب الحساب والفرع ومركز التكلفة والشهر.
  expenseAnalysis: (companyId:number, dateFrom:string, dateTo:string, branch?:number, costCenter?:number) => {
    const q = new URLSearchParams({company:String(companyId), date_from:dateFrom, date_to:dateTo});
    if (branch) q.set("branch", String(branch));
    if (costCenter) q.set("cost_center", String(costCenter));
    return request<ExpenseAnalysisReport>(`/api/accounting/journal-entries/expense-analysis/?${q.toString()}`);
  },
  costCenters: (companyId:number) => request<CostCenter[]>(`/api/accounting/cost-centers/?company=${companyId}&is_active=true`),
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
  generalLedger: (companyId: number, accountId: number, dateFrom: string, dateTo: string, branch?: number, page = 1, pageSize = 100) => request<GeneralLedgerReport>(`/api/accounting/journal-entries/general-ledger/?company=${companyId}&account=${accountId}&date_from=${dateFrom}&date_to=${dateTo}&page=${page}&page_size=${pageSize}${branch ? `&branch=${branch}` : ""}`),
  // جلب تقرير التغيرات في حقوق الملكية للفترة.
  changesInEquity: (companyId: number, dateFrom: string, dateTo: string, branch?: number) => request<EquityChangesReport>(`/api/accounting/journal-entries/changes-in-equity/?company=${companyId}&date_from=${dateFrom}&date_to=${dateTo}${branch ? `&branch=${branch}` : ""}`),
  // جلب قائمة العملاء للشركة.

  // جلب قواعد الخصم التجارية للشركة.
  discountRules: (companyId: number) => request<CustomerDiscountRule[]>(`/api/sales/customer-discount-rules/?company=${companyId}&ordering=-priority`),
  // إنشاء قاعدة خصم جديدة.
  createDiscountRule: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerDiscountRule>("/api/sales/customer-discount-rules/", {method:"POST",body:JSON.stringify(payload)}); },
  // تعديل قاعدة خصم قائمة.
  updateDiscountRule: async (id: number, payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerDiscountRule>(`/api/sales/customer-discount-rules/${id}/`, {method:"PATCH",body:JSON.stringify(payload)}); },
  customers: (companyId: number) => request<Customer[]>(`/api/sales/customers/?company=${companyId}&ordering=name_ar`),
  // جلب قائمة الموردين للشركة.
  suppliers: (companyId: number) => request<Supplier[]>(`/api/purchases/suppliers/?company=${companyId}&ordering=name_ar`),
  // إنشاء سجل مورد جديد.
  createSupplier: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Supplier>("/api/purchases/suppliers/", { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء سجل عميل جديد.
  createCustomer: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Customer>("/api/sales/customers/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعديل بيانات العميل أو إيقافه مع الحفاظ على سجله التاريخي.
  updateCustomer: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Customer>(`/api/sales/customers/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // تعديل بيانات المورد أو إيقافه مع الحفاظ على سجله التاريخي.
  updateSupplier: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<Supplier>(`/api/purchases/suppliers/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
};


export type InventoryUnit = { id: number; company: number; code: string; name_ar: string; symbol: string; decimal_places: number; is_active: boolean };
export type InventoryCategory = { id: number; company: number; code: string; name_ar: string; parent: number | null; is_active: boolean };
export type Product = { id: number; company: number; category: number | null; category_name: string | null; unit: number; unit_name: string; code: string; barcode: string; name_ar: string; name_en: string; product_type: "goods" | "service"; stock_class: "raw_material" | "finished_goods" | "operating_supplies" | "spare_parts" | null; purchase_price: string; sale_price: string; costing_method: string; min_stock: string; track_expiry: boolean; is_active: boolean };
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
export type InventoryCountLine = {
  id: number; count: number; product: number; product_code: string; product_name: string; unit_name: string;
  location: number | null; location_name: string | null; expiry_date: string | null;
  system_quantity: string; system_value: string; counted_quantity: string | null; variance_quantity: string | null;
  adjustment_unit_cost: string; notes: string; counted_by: number | null; counted_by_name: string | null;
  counted_at: string | null; created_at: string; updated_at: string;
};
export type InventoryCount = {
  id: number; company: number; warehouse: number; warehouse_name: string; count_number: string;
  count_date: string; status: "draft" | "posted" | "cancelled"; notes: string;
  lines: InventoryCountLine[]; lines_count: number; created_by: number | null; created_by_name: string | null;
  posted_by: number | null; posted_by_name: string | null; posted_at: string | null;
  gain_movement: number | null; gain_movement_number: string | null;
  loss_movement: number | null; loss_movement_number: string | null;
  created_at: string; updated_at: string;
};


export type StockMovementLine = { id: number; product: number; product_code: string; product_name: string; source_location: number | null; source_location_name: string | null; destination_location: number | null; destination_location_name: string | null; expiry_date: string | null; quantity: string; unit_cost: string; cost_amount: string; adjustment_direction: "in" | "out" | "" };
export type StockMovement = {
  id: number; company: number; movement_number: string; movement_type: "receipt" | "issue" | "sup_return" | "sup_rev" | "sal_rev" | "transfer" | "adjustment"; adjustment_reason: "count" | "damage" | "expired" | "write_off" | "other" | "";
  movement_date: string; source_warehouse: number | null; source_warehouse_name: string | null;
  destination_warehouse: number | null; destination_warehouse_name: string | null;
  status: "draft" | "posted"; notes: string; lines: StockMovementLine[]; journal_entries: { id: number; entry_number: string; branch_code: string; status: string }[]; created_at: string; posted_at: string | null;
};
export type InventoryStockAging = {
  company: number; branch: number | null; as_of: string;
  summary: Record<"active" | "slow" | "very_slow" | "stagnant" | "unknown", number>;
  total_rows: number;
  rows: { product: number; product_code: string; product_name: string; warehouse: number; warehouse_name: string; quantity: string; value: string; last_movement: string | null; age_days: number | null; age_band: "active" | "slow" | "very_slow" | "stagnant" | "unknown" }[];
  truncated: boolean; scope_note: string;
};
export type StockCard = { product: number; product_code: string; product_name: string; warehouse: number; warehouse_name: string; date_from: string | null; date_to: string | null; opening_quantity: string; opening_value: string; closing_quantity: string; closing_value: string; rows: { date: string; posted_at: string | null; movement_number: string; movement_type: string; direction: "in" | "out"; quantity_in: string; quantity_out: string; unit_cost: string; value_in: string; value_out: string; balance_quantity: string; balance_value: string }[]; pagination: { page: number; page_size: number; total_rows: number; total_pages: number; has_next: boolean; has_previous: boolean } };
export type InventoryCostAging = {
  company: number; branch: number | null; branch_name: string; as_of: string; basis: string; costing_method: "FIFO";
  totals: { layers: number; quantity: string; value: string };
  buckets: Record<"active" | "slow" | "very_slow" | "stagnant", { layers: number; quantity: string; value: string }>;
  rows: { id: number; product: number; product_code: string; product_name: string; warehouse: number; warehouse_name: string; location_name: string; source_movement_number: string; receipt_date: string; age_days: number; quantity_remaining: string; unit_cost: string; layer_value: string; expiry_date: string | null; expiry_status: "none" | "expired" | "expiring" | "valid" }[];
  truncated: boolean; untraceable_legacy_layers_count: number; scope_branch_ids: number[]; scope_note: string;
};
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
export type StockTransferStatus = "draft" | "in_transit" | "partially_received" | "received" | "cancelled";
export type StockTransferLine = {
  id: number; product: number; product_code: string; product_name: string; expiry_date: string | null;
  quantity: string; received_quantity: string; remaining_quantity: string;
};
export type StockTransfer = {
  id: number; company: number; transfer_number: string; transfer_date: string;
  source_warehouse: number; source_warehouse_name: string;
  destination_warehouse: number; destination_warehouse_name: string;
  status: StockTransferStatus; notes: string; lines: StockTransferLine[];
  dispatch_movement: number | null; created_by: number | null; created_by_name: string | null;
  dispatched_by: number | null; dispatched_by_name: string | null; dispatched_at: string | null;
  completed_at: string | null; receipts_count: number; created_at: string; updated_at: string;
};
export type StockTransferInTransit = {
  id: number; transfer_number: string; transfer_date: string;
  source_warehouse: string; destination_warehouse: string; status: StockTransferStatus;
  lines: { transfer_line: number; product: number; product_code: string; product_name: string; unit: string;
    dispatched_quantity: string; received_quantity: string; remaining_quantity: string; expiry_date: string | null }[];
};
export type StockTransferReceipt = {
  id: number; transfer: number; receipt_number: string; receipt_date: string;
  movement: number; movement_number: string; received_by: number | null; received_at: string;
  lines: { id: number; transfer_line: number; product_code: string; product_name: string; quantity: string; movement_line: number }[];
};
// واجهات حركات المخزون وتقاريره: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const stockApi = {
  // جلب حركات المخزون مرتبة من الأحدث.
  movements: (companyId: number) => request<StockMovement[]>(`/api/inventory/movements/recent/?company=${companyId}&limit=100`),
  // تحليل أعمار الحركة من قاعدة البيانات دون تنزيل سجل الحركات الكامل.
  stockAging: (companyId: number, branchId?: number) => request<InventoryStockAging>(`/api/inventory/balances/stock-aging/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب مستندات الجرد الفعلي للشركة.
  counts: (companyId: number) => request<InventoryCount[]>(`/api/inventory/counts/?company=${companyId}&ordering=-count_date`),
  // بدء جرد جديد وأخذ لقطة من أرصدة المخزن الحالية.
  createCount: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCount>("/api/inventory/counts/", { method: "POST", body: JSON.stringify(payload) }); },
  // إدخال صنف ظهر أثناء العد ولم يكن له رصيد دفتري.
  createCountLine: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCountLine>("/api/inventory/count-lines/", { method: "POST", body: JSON.stringify(payload) }); },
  // حفظ الكمية الفعلية وتكلفة الزيادة وملاحظات بند الجرد.
  updateCountLine: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCountLine>(`/api/inventory/count-lines/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
  // حذف بند إضافي أُدخل أثناء الجرد ولم يكن ضمن لقطة رصيد الدفاتر.
  removeExtraCountLine: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<{ detail: string }>(`/api/inventory/count-lines/${id}/remove/`, { method: "POST" }); },
  // ترحيل فروق الجرد بعد التأكد من عدم تغير المخزون منذ أخذ اللقطة.
  postCount: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCount>(`/api/inventory/counts/${id}/post-count/`, { method: "POST" }); },
  // إلغاء مستند جرد مسودة.
  cancelCount: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<InventoryCount>(`/api/inventory/counts/${id}/cancel/`, { method: "POST" }); },

  // جلب أرصدة الأصناف حسب المخازن.
  balances: (companyId: number) => request<StockBalance[]>(`/api/inventory/balances/?warehouse__company=${companyId}&ordering=product__code`),
  // إنشاء مستند حركة مخزنية.
  createMovement: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>("/api/inventory/movements/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل الحركة المخزنية وتحديث الأرصدة.
  postMovement: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockMovement>(`/api/inventory/movements/${id}/post/`, { method: "POST" }); },
  // جلب تقرير مطابقة قيمة المخزون مع الحسابات.
  reconciliation: (companyId: number, branchId?: number) => request<InventoryReconciliation>(`/api/inventory/balances/reconciliation/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب فحص سلامة تقييم المخزون والحركات المرتبطة.
  costAging: (companyId: number, branchId?: number) => request<InventoryCostAging>(`/api/inventory/balances/cost-aging/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  valuationIntegrity: (companyId: number, branchId?: number) => request<InventoryValuationIntegrity>(`/api/inventory/balances/valuation-integrity/?company=${companyId}${branchId ? `&branch=${branchId}` : ""}`),
  // جلب كارت الصنف وحركاته داخل مخزن وفترة محددين.
  stockCard: (productId: number, warehouseId: number, dateFrom: string, dateTo: string, page = 1, pageSize = 50) => request<StockCard>(`/api/inventory/movements/stock-card/?product=${productId}&warehouse=${warehouseId}&page=${page}&page_size=${pageSize}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),
  // جلب مستندات التحويل بين الفروع.
  transfers: (companyId: number) => request<StockTransfer[]>(`/api/inventory/transfers/?company=${companyId}&ordering=-transfer_date`),
  // جلب الكميات التي ما زالت في عهدة البضاعة بالطريق.
  inTransitTransfers: (companyId: number) => request<StockTransferInTransit[]>(`/api/inventory/transfers/in-transit/?company=${companyId}`),
  // إنشاء مسودة تحويل بين مخزنين.
  createTransfer: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockTransfer>("/api/inventory/transfers/", { method: "POST", body: JSON.stringify(payload) }); },
  // شحن التحويل ونقل المخزون إلى مخزن البضاعة بالطريق.
  dispatchTransfer: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<StockTransfer>(`/api/inventory/transfers/${id}/dispatch/`, { method: "POST" }); },
  // تسجيل استلام كلي أو جزئي من البضاعة بالطريق.
  receiveTransfer: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<StockTransferReceipt>(`/api/inventory/transfers/${id}/receive/`, { method: "POST", body: JSON.stringify(payload) }); },
};


export type AccountRole = { id: number; company: number; role: string; account: number; account_code: string; activity: number | null; activity_code?: Company["activity"] | null };
// واجهات أدوار الحسابات: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const accountRoleApi = {
  // جلب أدوار الحسابات المعرفة للشركة.
  list: (companyId: number) => request<AccountRole[]>(`/api/accounting/account-roles/?company=${companyId}`),
  // إنشاء ربط دور محاسبي بحساب في دليل الحسابات.
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>("/api/accounting/account-roles/", { method: "POST", body: JSON.stringify(payload) }); },
  // تعديل الحساب المرتبط بالدور المحاسبي المحدد.
  update: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<AccountRole>(`/api/accounting/account-roles/${id}/`, { method: "PATCH", body: JSON.stringify(payload) }); },
};


export type SalesDelivery = { id: number; company: number; customer: number; customer_name: string; warehouse: number; warehouse_name: string; order:number|null; order_number:string|null; order_snapshot?: {order_id:number;order_number:string;order_date:string;lines:{order_line_id:number;product:number;quantity:string;unit_price:string;tax_rate:string;tax_code:number|null;discount_percent:string;discount_rule:null}[]}; delivery_number: string; delivery_date: string; customer_reference: string; notes: string; movement_status: "draft" | "posted"; quotation_snapshot?: { quotation_id:number; quotation_number:string; quotation_date:string; valid_until:string; lines:{quotation_line_id:number;product:number;quantity:string;unit_price:string;tax_rate:string;tax_code:number|null;discount_percent:string;discount_rule:number|null}[] }; line_items: { id: number; product: number; product_code: string; product_name: string; quantity: string; expiry_date: string | null; order_line:number|null }[]; created_at: string };
export type SalesInvoice = { print_template_snapshot?: {template_id:number|null;name:string;style:"classic"|"modern"|"compact";config:SalesDocumentTemplate["config"]}; id: number; company: number; customer: number; customer_name: string; delivery: number | null; delivery_number: string | null; branch: number | null; invoice_number: string; invoice_date: string; transaction_currency: number | null; transaction_currency_code: string | null; exchange_rate: string | null; exchange_rate_date: string | null; transaction_net_total: string | null; transaction_discount_total: string | null; transaction_tax_total: string | null; transaction_grand_total: string | null; status: "draft" | "posted"; net_total: string; discount_total: string; discount_approval_limit: string | null; invoice_approval_limit: string | null; approval_status: "not_required" | "pending" | "approved" | "rejected" | "superseded"; tax_total: string; grand_total: string; paid_total: string; balance_due: string; notes: string; journal_entry: number | null; journal_number: string | null; invoice_lines: { id: number; delivery_line: number | null; product: number; product_code: string; product_name: string; quantity: string; unit_price: string; transaction_unit_price: string | null; transaction_net_amount: string | null; transaction_discount_amount: string | null; transaction_tax_amount: string | null; transaction_total_amount: string | null; tax_rate: string; tax_code: number | null; discount_percent: string; discount_amount: string; net_amount: string; tax_amount: string; total_amount: string }[]; created_at: string; posted_at: string | null };

export type SalesDocumentTemplate = {
  id:number; company:number; company_name:string;
  document_type:"quotation"|"invoice"|"delivery"|"receipt"|"return"|"purchase_req"|"purchase_rfq"|"supplier_quote"|"purchase_order"|"service_accept"|"purchase_receipt"|"purchase_invoice"|"supplier_payment"|"supplier_return"|"supplier_refund";
  name:string; style:"classic"|"modern"|"compact";
  config:{accent_color?:string;show_logo?:boolean;show_tax_number?:boolean;show_discount?:boolean;show_tax?:boolean;show_bank_details?:boolean;bank_details?:string;footer_text?:string;language?:"ar"|"en"|"bilingual";font_scale?:"small"|"normal"|"large"};
  is_default:boolean;is_active:boolean;created_at:string;updated_at:string;
};
export type SalesQuotation = {
  id:number; company:number; customer:number; customer_name:string; quotation_number:string;
  quotation_date:string; valid_until:string; status:"draft"|"sent"|"accepted"|"rejected"|"expired"|"converted";
  net_total:string; discount_total:string; tax_total:string; grand_total:string; notes:string; print_template_snapshot?: {template_id:number|null;name:string;style:"classic"|"modern"|"compact";config:SalesDocumentTemplate["config"]};
  converted_delivery:number|null; converted_delivery_number:string|null; created_at:string; updated_at:string;
  sent_at:string|null; accepted_at:string|null; converted_at:string|null;
  quotation_lines:{id:number;product:number;product_code:string;product_name:string;quantity:string;unit_price:string;
    tax_rate:string;tax_code:number|null;discount_percent:string;discount_rule:number|null;
    discount_amount:string;net_amount:string;tax_amount:string;total_amount:string}[];
};
export type SalesReturn = { id:number; company:number; invoice:number; invoice_number:string; customer_name:string; warehouse:number|null; branch:number|null; return_number:string; return_date:string; status:"draft"|"posted"|"reversed"; net_total:string; tax_total:string; grand_total:string; transaction_currency_code:string|null; transaction_net_total:string|null; transaction_tax_total:string|null; transaction_grand_total:string|null; movement_status:string|null; journal_entry:number|null; journal_number:string|null; cost_journal_entry:number|null; cost_journal_number:string|null; reversal_stock_movement:number|null; reversal_journal_entry:number|null; reversal_journal_number:string|null; reversal_date:string|null; reversed_at:string|null; reversed_by:number|null; notes:string; line_items:{id:number;invoice_line:number;product_code:string;product_name:string;quantity:string;net_amount:string;tax_amount:string;total_amount:string;transaction_net_amount:string|null;transaction_tax_amount:string|null;transaction_total_amount:string|null;cost_amount:string}[]; created_at:string; posted_at:string|null };

export type DiscountApprovalPolicy = { id: number; company: number; company_name: string; max_discount_percent: string | null; max_sales_invoice_amount: string | null; max_purchase_invoice_amount: string | null; max_purchase_order_amount: string | null; max_payment_amount: string | null; created_at: string; updated_at: string };
export type SalesInvoiceApprovalLineSnapshot = {
  delivery_line_id: number | null; product_id: number; product_code: string; product_name: string;
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
  document_type: "purchase_invoice" | "purchase_order" | "supplier_payment" | "treasury_payment" | "asset_purchase_invoice" | "asset_supplier_payment";
  purchase_invoice: number | null; purchase_order: number | null; supplier_payment: number | null; treasury_transaction: number | null;
  asset_purchase_invoice: number | null; asset_supplier_payment: number | null;
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


export type CustomerDiscountRule = {
  id: number; company: number; customer: number | null; customer_name: string | null;
  customer_type: "" | "company" | "individual"; product: number | null; product_name: string | null;
  category: number | null; category_name: string | null; min_quantity: string | null;
  min_invoice_amount: string | null; discount_percent: string; priority: number;
  effective_from: string; effective_to: string | null; is_active: boolean; notes: string;
  created_at: string; updated_at: string;
};

export type CustomerReceipt = { id: number; company: number; customer: number; customer_name: string; invoice: number; invoice_number: string; receipt_number: string; receipt_date: string; method: "cash" | "bank"; payment_account: number | null; payment_account_name: string | null; amount: string; transaction_currency: number | null; transaction_currency_code: string | null; transaction_amount: string | null; exchange_rate: string | null; exchange_rate_date: string | null; carrying_base_amount: string | null; realized_fx_gain: string; realized_fx_loss: string; reference: string; status: "draft" | "posted" | "reversed"; journal_entry: number | null; journal_number: string | null; reversal_journal_entry: number | null; created_at: string; posted_at: string | null };
export type CustomerStatement = {
  company:number; customer:number; customer_name:string; date_from:string|null; date_to:string|null;
  branch_filter:number|null; opening_balance:string; closing_balance:string;
  rows:{date:string;document_type:"invoice"|"receipt"|"receipt_reversal"|"receipt_batch"|"receipt_batch_reversal"|"sales_return"|"sales_return_reversal";document_number:string;description:string;debit:string;credit:string;balance:string}[];
};
export type CustomerAging = {
  company: number; as_of: string; customer_filter: number | null; branch_filter: number | null;
  totals: { not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string; unallocated_advances: string };
  customers: { customer: number; customer_code: string; customer_name: string; not_due: string; "1_30": string; "31_60": string; "61_90": string; over_90: string; total_due: string; unallocated_advances: string;
    invoices: { invoice: number; invoice_number: string; invoice_date: string; due_date: string; overdue_days: number; bucket: string; grand_total: string; paid_as_of: string; returned_as_of: string; balance_due: string }[] }[];
};
// واجهات المبيعات والتحصيل: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export type SalesOrder = {
  id:number; company:number; customer:number; customer_name:string; warehouse:number; warehouse_name:string;
  order_number:string; order_date:string; requested_delivery_date:string|null;
  status:"draft"|"confirmed"|"partially_delivered"|"delivered"|"cancelled";
  net_total:string;discount_total:string;tax_total:string;grand_total:string;notes:string;
  order_lines:{id:number;product:number;product_code:string;product_name:string;quantity:string;allocated_quantity:string;remaining_quantity:string;unit_price:string;
    tax_rate:string;tax_code:number|null;discount_percent:string;discount_amount:string;net_amount:string;tax_amount:string;total_amount:string}[];
  created_at:string;updated_at:string;
};

export type CustomerReceiptBatchAllocation = {
  id:number;invoice:number;invoice_number:string;invoice_date:string;amount:string;allocation_date:string;created_at:string;
};

export type CustomerReceiptBatch = {
  id:number;company:number;customer:number;customer_name:string;branch:number;branch_name:string;
  receipt_number:string;receipt_date:string;method:"cash"|"bank";payment_account:number|null;
  payment_account_name:string|null;amount:string;allocated_amount:string;unallocated_amount:string;
  allocation_lines:CustomerReceiptBatchAllocation[];reference:string;status:"draft"|"posted"|"reversed";
  journal_entry:number|null;journal_number:string|null;reversal_journal_entry:number|null;
  reversal_journal_number:string|null;reversal_date:string|null;created_at:string;posted_at:string|null;
};

export type SalesPerformanceReport = {
  company:number;date_from:string;date_to:string;branch:number|null;customer_filter:number|null;product_filter:number|null;
  totals:{
    invoice_count:number;return_count:number;sales_net:string;discount_total:string;sales_tax:string;sales_total:string;
    return_net:string;return_tax:string;return_total:string;net_sales:string;net_cost:string;gross_profit:string;
  };
  by_customer:Array<{
    customer:number;customer_name:string;invoice_count:number;quantity_sold:string;quantity_returned:string;
    sales_net:string;return_net:string;sales_tax:string;return_tax:string;net_sales:string;net_cost:string;gross_profit:string;
  }>;
  by_product:Array<{
    product:number;product_code:string;product_name:string;invoice_count:number;quantity_sold:string;quantity_returned:string;
    sales_net:string;return_net:string;sales_tax:string;return_tax:string;net_sales:string;net_cost:string;gross_profit:string;
  }>;
};

export const salesApi = {
  salesPerformance: (companyId:number,dateFrom:string,dateTo:string,branchId?:number,customerId?:number,productId?:number) => {
    const q=new URLSearchParams({company:String(companyId),date_from:dateFrom,date_to:dateTo});
    if(branchId)q.set("branch",String(branchId));
    if(customerId)q.set("customer",String(customerId));
    if(productId)q.set("product",String(productId));
    return request<SalesPerformanceReport>(`/api/sales-documents/invoices/sales-performance/?${q.toString()}`);
  },
  // جلب كشف حساب العميل مع رصيد أول وآخر المدة والحركات المرحلة.
  customerStatement: (companyId:number, customerId:number, dateFrom="", dateTo="") => request<CustomerStatement>(`/api/sales-documents/invoices/customer-statement/?company=${companyId}&customer=${customerId}${dateFrom ? `&date_from=${dateFrom}` : ""}${dateTo ? `&date_to=${dateTo}` : ""}`),
  documentTemplates: (companyId:number) => request<SalesDocumentTemplate[]>(`/api/sales-documents/document-templates/?company=${companyId}&ordering=document_type`),
  createDocumentTemplate: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesDocumentTemplate>("/api/sales-documents/document-templates/",{method:"POST",body:JSON.stringify(payload)}); },
  updateDocumentTemplate: async (id:number,payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesDocumentTemplate>(`/api/sales-documents/document-templates/${id}/`,{method:"PATCH",body:JSON.stringify(payload)}); },
  quotations: (companyId:number) => request<SalesQuotation[]>(`/api/sales-documents/quotations/?company=${companyId}&ordering=-quotation_date`),
  createQuotation: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesQuotation>("/api/sales-documents/quotations/",{method:"POST",body:JSON.stringify(payload)}); },
  updateQuotation: async (id:number,payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesQuotation>(`/api/sales-documents/quotations/${id}/`,{method:"PATCH",body:JSON.stringify(payload)}); },
  deleteQuotation: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<void>(`/api/sales-documents/quotations/${id}/`,{method:"DELETE"}); },
  sendQuotation: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesQuotation>(`/api/sales-documents/quotations/${id}/send/`,{method:"POST",body:"{}"}); },
  acceptQuotation: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesQuotation>(`/api/sales-documents/quotations/${id}/accept/`,{method:"POST",body:"{}"}); },
  rejectQuotation: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesQuotation>(`/api/sales-documents/quotations/${id}/reject/`,{method:"POST",body:"{}"}); },
  convertQuotation: async (id:number,payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesDelivery>(`/api/sales-documents/quotations/${id}/convert-to-delivery/`,{method:"POST",body:JSON.stringify(payload)}); },
  // أوامر البيع وحالتها والكميات المتبقية.
  orders: (companyId:number) => request<SalesOrder[]>(`/api/sales-documents/orders/?company=${companyId}&ordering=-order_date`),
  createOrder: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesOrder>("/api/sales-documents/orders/",{method:"POST",body:JSON.stringify(payload)}); },
  confirmOrder: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesOrder>(`/api/sales-documents/orders/${id}/confirm/`,{method:"POST",body:"{}"}); },
  cancelOrder: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesOrder>(`/api/sales-documents/orders/${id}/cancel/`,{method:"POST",body:"{}"}); },
  // جلب مستندات تسليم المبيعات للشركة.
  deliveries: (companyId: number) => request<SalesDelivery[]>(`/api/sales-documents/deliveries/?company=${companyId}&ordering=-delivery_date`),
  // جلب فواتير الموردين للشركة.
  invoices: (companyId: number) => request<SalesInvoice[]>(`/api/sales-documents/invoices/?company=${companyId}&ordering=-invoice_date`),
  // جلب سندات القبض ومردودات المبيعات للشركة.
  receipts: (companyId: number) => request<CustomerReceipt[]>(`/api/sales-documents/receipts/?company=${companyId}&ordering=-receipt_date`),
  returns: (companyId:number) => request<SalesReturn[]>(`/api/sales-documents/returns/?company=${companyId}&ordering=-return_date`),
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
  // إعادة تقييم رصيد فاتورة العميل الأجنبية المفتوح.
  revalueInvoice: async (id:number,payload:{revaluation_date:string;rate_date:string;rate:string}) => { await request<{detail:string}>("/api/csrf/"); return request<FxSubledgerRevaluation>(`/api/sales-documents/invoices/${id}/revalue/`,{method:"POST",body:JSON.stringify(payload)}); },
  fxRevaluations: (id:number) => request<FxSubledgerRevaluation[]>(`/api/sales-documents/invoices/${id}/revaluations/`),
  // إنشاء مردود مبيعات مرتبط ببنود فاتورة أصلية.
  createReturn: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<SalesReturn>("/api/sales-documents/returns/", {method:"POST",body:JSON.stringify(payload)}); },
  postReturn: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<SalesReturn>(`/api/sales-documents/returns/${id}/post/`, {method:"POST"}); },
  reverseReturn: async (id:number, entryDate?:string) => { await request<{detail:string}>("/api/csrf/"); return request<SalesReturn>(`/api/sales-documents/returns/${id}/reverse/`, {method:"POST",body:JSON.stringify(entryDate?{entry_date:entryDate}:{})}); },
  // إنشاء سند قبض من عميل.
  createReceipt: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>("/api/sales-documents/receipts/", { method: "POST", body: JSON.stringify(payload) }); },
  // ترحيل سند القبض وتحديث رصيد العميل والخزينة.
  postReceipt: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>(`/api/sales-documents/receipts/${id}/post/`, { method: "POST" }); },
  // عكس آخر سند قبض مرحّل مع إنشاء قيد عكسي مرتبط.
  reverseReceipt: async (id: number, entryDate?: string) => { await request<{ detail: string }>("/api/csrf/"); return request<CustomerReceipt>(`/api/sales-documents/receipts/${id}/reverse/`, { method: "POST", body: JSON.stringify(entryDate ? { entry_date: entryDate } : {}) }); },
  // جلب سندات القبض المجمعة والدفعات المقدمة.
  receiptBatches: (companyId:number) => request<CustomerReceiptBatch[]>(`/api/sales-documents/receipt-batches/?company=${companyId}&ordering=-receipt_date`),
  // إنشاء سند قبض مجمع بمخصصات اختيارية على عدة فواتير.
  createReceiptBatch: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerReceiptBatch>("/api/sales-documents/receipt-batches/", {method:"POST",body:JSON.stringify(payload)}); },
  // ترحيل سند القبض المجمع إلى الخزينة وحساب العملاء.
  postReceiptBatch: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerReceiptBatch>(`/api/sales-documents/receipt-batches/${id}/post/`, {method:"POST"}); },
  // تخصيص جزء من دفعة مقدمة إلى فاتورة دون حركة نقدية إضافية.
  allocateReceiptBatch: async (id:number,invoice:number,amount:string,allocation_date:string) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerReceiptBatch>(`/api/sales-documents/receipt-batches/${id}/allocate/`, {method:"POST",body:JSON.stringify({invoice,amount,allocation_date})}); },
  // عكس سند قبض مجمع وقيد التحصيل المرتبط به.
  reverseReceiptBatch: async (id:number,entryDate?:string) => { await request<{detail:string}>("/api/csrf/"); return request<CustomerReceiptBatch>(`/api/sales-documents/receipt-batches/${id}/reverse/`, {method:"POST",body:JSON.stringify(entryDate?{entry_date:entryDate}:{})}); },
};


export type BankReconciliation = { id: number; company: number; bank_account: number; bank_name: string; bank_code: string; statement_number: string; statement_date: string; opening_balance: string; closing_balance: string; status: "draft" | "reconciled"; line_count: number; created_at: string; reconciled_at: string | null };
export type BankStatementLine = { id: number; reconciliation: number; line_number: number; transaction_date: string; description: string; reference: string; amount: string; matched_transaction: number | null; matched_document: string; matched_amount: string; remaining_amount: string; allocations: {id:number;transaction_id:number;document_number:string;transaction_date:string;allocated_amount:string}[]; adjustment_transaction: number | null; adjustment_document: string; adjustment_status: string; matched_at: string | null };
export type BankReconciliationSummary = { reconciliation: BankReconciliation; statement_calculated: string; statement_difference: string; book_balance: string; outstanding_total: string; unmatched_statement_total: string; expected_book_balance: string; unexplained_difference: string; matched_lines: number; unmatched_lines: number; lines: BankStatementLine[]; outstanding_transactions: { id: number; date: string; document_number: string; description: string; amount: string; original_amount:string; allocated_amount:string; remaining_amount:string; timing_difference_days:number }[] };
// واجهات التسوية البنكية: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export type BankStatementImportPreview = {
  file_name:string; total:number; new_count:number; duplicate_count:number;
  rows:{date:string;amount:string;description:string;reference:string}[]; rows_truncated?:boolean;
  duplicates:{date:string;amount:string;description:string;reference:string}[];
};
export type BankStatementImportResult = {imported:number;duplicate_count:number;message:string};

export const bankReconciliationApi = {
  // جلب جلسات التسوية البنكية للشركة مرتبة حسب تاريخ كشف الحساب.
  list: (companyId: number) => request<BankReconciliation[]>(`/api/treasury/reconciliations/?company=${companyId}&ordering=-statement_date`),
  // إنشاء جلسة تسوية بنكية جديدة.
  create: async (payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>("/api/treasury/reconciliations/", { method: "POST", body: JSON.stringify(payload) }); },
  // جلب ملخص التسوية البنكية والأرصدة والسطور غير المطابقة.
  summary: (id: number) => request<BankReconciliationSummary>(`/api/treasury/reconciliations/${id}/summary/`),
  // إضافة سطر كشف بنك إلى جلسة التسوية.
  addLine: async (id: number, payload: unknown) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/add_line/`, { method: "POST", body: JSON.stringify(payload) }); },
  // إنشاء مسودة سند قبض أو صرف من حركة كشف غير مسجلة مثل الفوائد أو العمولات.
  recordAdjustment: async (id:number,payload:{line_id:number;counter_account:number;fiscal_year:number;branch?:number;cash_flow_category?:string}) => { await request<{detail:string}>("/api/csrf/"); return request<{line:BankStatementLine;transaction:TreasuryTransaction;message:string}>(`/api/treasury/reconciliations/${id}/record-adjustment/`, {method:"POST",body:JSON.stringify(payload)}); },
  // مطابقة سطر كشف البنك مع حركة خزينة محددة.
  match: async (id: number, lineId: number, transactionId: number, amount?:string) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/match/`, { method: "POST", body: JSON.stringify({line_id:lineId,transaction_id:transactionId,...(amount?{amount}:{})}) }); },
  // فك مطابقة سطر كشف البنك من الحركة المرتبطة.
  unmatch: async (id: number, lineId: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankStatementLine>(`/api/treasury/reconciliations/${id}/unmatch/`, { method: "POST", body: JSON.stringify({line_id:lineId}) }); },
  // معاينة ملف كشف البنك قبل الحفظ.
  previewImport: async (id:number,file:File) => uploadBankStatementFile<BankStatementImportPreview>(id,"preview-import",file),
  // تأكيد حفظ الحركات الجديدة فقط بعد إعادة فحص التكرار على الخادم.
  confirmImport: async (id:number,file:File) => uploadBankStatementFile<BankStatementImportResult>(id,"confirm-import",file),
  // اعتماد التسوية البنكية بعد استكمال إجراءات المطابقة.
  reconcile: async (id: number) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>(`/api/treasury/reconciliations/${id}/reconcile/`, { method: "POST" }); },
  // إعادة فتح تسوية معتمدة بسبب موثق وبعد التأكد من عدم وجود تسويات لاحقة.
  reopen: async (id: number, reason: string) => { await request<{ detail: string }>("/api/csrf/"); return request<BankReconciliation>(`/api/treasury/reconciliations/${id}/reopen/`, { method: "POST", body: JSON.stringify({reason}) }); },
};

const uploadBankStatementFile = async <T,>(id:number,action:string,file:File):Promise<T> => {
  await request<{detail:string}>("/api/csrf/");
  const form=new FormData();form.append("file",file);
  const response=await fetch(`/api/treasury/reconciliations/${id}/${action}/`,{
    method:"POST",credentials:"same-origin",
    headers:{Accept:"application/json","X-CSRFToken":csrfToken()},body:form
  });
  if(!response.ok){
    let message=`تعذر معالجة كشف البنك (HTTP ${response.status}).`;
    try{const body=await response.json() as Record<string,unknown>;message=Object.entries(body).map(([k,v])=>`${k}: ${Array.isArray(v)?v.join("، "):typeof v==="string"?v:JSON.stringify(v)}`).join(" | ")||message;}catch{}
    throw new Error(message);
  }
  return response.json() as Promise<T>;
};

export type FixedAsset = {
  id: number; company: number; branch: number; code: string; name_ar: string; category: "land"|"building"|"vehicle"|"furniture"|"equipment"|"computers"|"other"; description: string; location: string; custodian: string; custodian_employee: number | null; serial_number: string; warranty_end_date: string|null; supplier_name: string; supplier_invoice: string;
  acquisition_date: string; depreciation_start_date: string; original_cost: string; acquisition_net_amount: string|null; recoverable_tax_amount:string; nonrecoverable_tax_amount:string; acquisition_tax_code:number|null; salvage_value: string;
  useful_life_months: number; depreciation_method: "straight_line"|"declining_balance"|"units_of_production"; declining_rate_percentage: string|null; production_units_total: number|null; asset_account: number;
  accumulated_depreciation_account: number; depreciation_expense_account: number;
  acquisition_counter_account: number; disposal_gain_loss_account: number; disposal_loss_account: number | null; acquisition_journal: number | null; acquisition_treasury_transaction: number | null;
  status: "active" | "disposed"; disposed_at: string | null; disposal_proceeds: string | null; disposal_journal: number | null; disposal_treasury_transaction: number | null;
  vendor_invoice_id: number | null; vendor_invoice_status: "draft" | "posted" | null;
  vendor_payments: {id:number;payment_number:string;payment_date:string;amount:string;status:"draft"|"posted"|"reversed";journal_entry:number|null}[];
  accumulated_depreciation: string; accumulated_impairment?:string; net_book_value: string; created_at: string;
};
export type AssetComponent = {
  id:number;asset:number;code:string;name_ar:string;original_cost:string;salvage_value:string;
  useful_life_months:number;depreciation_method:"straight_line"|"declining_balance"|"units_of_production";
  declining_rate_percentage:string|null;production_units_total:number|null;
  asset_account:number;accumulated_depreciation_account:number;depreciation_expense_account:number;
  status:"active"|"disposed";accumulated_depreciation:string;created_at:string;
};
export type AssetInsurancePolicy = {
  id:number;asset:number;policy_number:string;insurer_name:string;coverage_description:string;
  start_date:string;end_date:string;insured_value:string;premium_amount:string;
  document_reference:string;status:"active"|"expired"|"cancelled";notes:string;created_at:string;
};
export type AssetPhysicalInventory = {
  id:number;asset:number;count_date:string;expected_location:string;counted_location:string;
  expected_custodian:string;counted_custodian:string;
  result:"matched"|"location_mismatch"|"custodian_mismatch"|"condition_issue"|"not_found";
  condition_notes:string;notes:string;counted_by:number;created_at:string;
};
export type AssetMovement = {
  id:number; company:number; asset:number; asset_code:string; asset_name:string;
  movement_type:"branch_transfer"|"custody_change"|"location_change"; movement_date:string;
  from_branch:number; from_branch_name:string; to_branch:number; to_branch_name:string;
  from_employee:number|null; from_employee_name:string|null; to_employee:number|null; to_employee_name:string|null;
  from_custodian:string; to_custodian:string; from_location:string; to_location:string;
  document_number:string; notes:string; status:"pending"|"approved"|"rejected";
  requested_by:number; requested_by_name:string; reviewed_by:number|null; reviewed_by_name:string|null;
  reviewed_at:string|null; review_note:string; created_at:string;
};
export type AssetSchedule = { asset_id: number; original_cost: string; accumulated_depreciation: string; accumulated_impairment:string; net_book_value: string;
  rows: { id: number; asset: number; period_date: string; amount: string; is_partial_period: boolean; journal_entry: number; created_at: string }[];
  impairments:{id:number;impairment_date:string;carrying_amount_before:string;recoverable_amount:string;impairment_loss:string;journal_entry:number;status:string}[];
  impairment_reversals:{id:number;reversal_date:string;carrying_amount_before:string;recoverable_amount:string;hypothetical_carrying_amount:string;reversal_amount:string;journal_entry:number;status:string}[] };
export type AssetRegisterReport = {
  company:number;as_of:string;
  totals:{original_cost:string;accumulated_depreciation:string;accumulated_impairment:string;net_book_value_active:string;asset_count:number};
  rows:{asset_id:number;code:string;name_ar:string;branch_id:number;branch_name:string;acquisition_date:string;original_cost:string;salvage_value:string;useful_life_months:number;accumulated_depreciation:string;accumulated_impairment:string;net_book_value:string;status:"active"|"disposed";disposed_at:string|null;acquisition_journal:number|null;disposal_journal:number|null}[];
};
export type AssetDepreciationReport = {company:number;date_from:string|null;date_to:string|null;total_depreciation:string;branch_totals:{branch_name:string;amount:string}[];rows:{asset_id:number;asset_code:string;asset_name:string;branch_name:string;period_date:string;amount:string;is_partial_period:boolean;journal_entry:number}[]};
export type AssetMovementReport = {company:number;date_from:string|null;date_to:string|null;rows:{asset_id:number;asset_code:string;asset_name:string;branch_name:string;date:string;event_type:string;event_label:string;amount:string;journal_entry:number|null;treasury_transaction:number|null}[]};
export type AssetReconciliationRow = {account_id:number;code:string;name:string;expected:string;actual:string;difference:string};
export type AssetAccountReconciliation = {
  company:number;as_of:string;date_from:string;date_to:string;
  cost_accounts:AssetReconciliationRow[];
  accumulated_depreciation_accounts:AssetReconciliationRow[];
  depreciation_expense_accounts:AssetReconciliationRow[];
  accumulated_impairment_accounts:AssetReconciliationRow[];
  asset_expense_accounts:(AssetReconciliationRow & {expected_depreciation:string;expected_impairment:string})[];
  impairment_reversal_income_accounts?:AssetReconciliationRow[];
  is_reconciled:boolean;difference_count:number;note:string;
};
export type AssetDepreciationRunResult = {
  period_date:string;fiscal_year:number;processed_count:number;processed_total:string;skipped_count:number;
  processed:{asset_id:number;code:string;amount:string}[];
  skipped:{asset_id:number;code:string;reason:string|string[]}[];note:string;
};
// واجهات الأصول الثابتة: تجمع طلبات القراءة والإنشاء والتعديل والإجراءات الخاصة بهذه الوحدة.
export const fixedAssetsApi = {
  // مكونات الأصل ووثائق التأمين ومحاضر الجرد الفعلي.
  components: (assetId:number) => request<AssetComponent[]>(`/api/assets/asset-components/?asset=${assetId}`),
  createComponent: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<AssetComponent>("/api/assets/asset-components/", {method:"POST",body:JSON.stringify(payload)}); },
  insurancePolicies: (assetId:number) => request<AssetInsurancePolicy[]>(`/api/assets/asset-insurance/?asset=${assetId}`),
  createInsurancePolicy: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<AssetInsurancePolicy>("/api/assets/asset-insurance/", {method:"POST",body:JSON.stringify(payload)}); },
  physicalInventory: (assetId:number) => request<AssetPhysicalInventory[]>(`/api/assets/asset-physical-inventory/?asset=${assetId}`),
  createPhysicalInventory: async (payload:unknown) => { await request<{detail:string}>("/api/csrf/"); return request<AssetPhysicalInventory>("/api/assets/asset-physical-inventory/", {method:"POST",body:JSON.stringify(payload)}); },
  // مستندات نقل الأصول وتغيير العهدة والموقع.
  movements: (companyId:number) => request<AssetMovement[]>(`/api/assets/asset-movements/?company=${companyId}`),
  createMovement: async (payload:{asset:number;movement_type:AssetMovement["movement_type"];movement_date:string;to_branch?:number;to_employee?:number|null;to_custodian?:string;to_location?:string;document_number:string;notes?:string}) => {
    await request<{detail:string}>("/api/csrf/");
    return request<AssetMovement>("/api/assets/asset-movements/", {method:"POST",body:JSON.stringify(payload)});
  },
  approveMovement: async (id:number, review_note:string) => {
    await request<{detail:string}>("/api/csrf/");
    return request<AssetMovement>(`/api/assets/asset-movements/${id}/approve/`, {method:"POST",body:JSON.stringify({review_note})});
  },
  rejectMovement: async (id:number, review_note:string) => {
    await request<{detail:string}>("/api/csrf/");
    return request<AssetMovement>(`/api/assets/asset-movements/${id}/reject/`, {method:"POST",body:JSON.stringify({review_note})});
  },
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
  // مطابقة سجل الأصول ومجمع الإهلاك ومصروفه مع دفتر الأستاذ.
  accountReconciliation: (companyId:number, asOf:string, dateFrom:string, dateTo:string, branch?:number) => {
    const q = new URLSearchParams({company:String(companyId),as_of:asOf,date_from:dateFrom,date_to:dateTo}); if(branch) q.set("branch",String(branch));
    return request<AssetAccountReconciliation>(`/api/assets/fixed-assets/account-reconciliation/?${q.toString()}`);
  },
  // تشغيل الإهلاك الدوري دفعة واحدة للأصول النشطة في الشركة أو الفرع المحدد.
  runDepreciation: async (companyId:number, periodDate:string, fiscalYear:number, branch?:number) => {
    await request<{detail:string}>("/api/csrf/");
    const q = new URLSearchParams({company:String(companyId)}); if(branch) q.set("branch",String(branch));
    return request<AssetDepreciationRunResult>(`/api/assets/fixed-assets/depreciation-run/?${q.toString()}`, {method:"POST",body:JSON.stringify({period_date:periodDate,fiscal_year:fiscalYear})});
  },
  // إنشاء بطاقة أصل ثابت جديدة.
  create: async (payload: unknown) => { await request<{detail:string}>("/api/csrf/"); return request<FixedAsset>("/api/assets/fixed-assets/", {method:"POST",body:JSON.stringify(payload)}); },
  // تسجيل فاتورة مورد مباشرة للأصل مع معالجة ضريبة المدخلات.
  purchaseInvoice: async (id:number, payload:{supplier:number;invoice_number:string;invoice_date:string;net_amount:string;tax_code:number|null;fiscal_year:number}) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;asset:number;invoice_number:string;status:"draft"|"posted";approval_required?:boolean;approval_request?:number;grand_total:string;journal_entry:number|null;asset_cost?:string}>(`/api/assets/fixed-assets/${id}/purchase-invoice/`, {method:"POST",body:JSON.stringify(payload)});
  },
  // إنشاء سند سداد مرتبط بفاتورة الأصل.
  supplierPayment: async (id:number, payload:{payment_number:string;payment_date:string;payment_account:number;amount:string;reference?:string}) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;invoice:number;payment_number:string;status:"draft"|"posted";approval_required?:boolean;approval_request?:number;amount:string;journal_entry:number|null}>(`/api/assets/fixed-assets/${id}/supplier-payment/`, {method:"POST",body:JSON.stringify(payload)});
  },
  // ترحيل فاتورة أصل بعد الموافقة المالية.
  postPurchaseInvoice: async (id:number) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;status:"posted";journal_entry:number;grand_total:string}>(`/api/assets/fixed-assets/${id}/post-purchase-invoice/`, {method:"POST"});
  },
  // ترحيل سند سداد أصل بعد الموافقة المالية.
  postSupplierPayment: async (id:number, payment:number) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;status:"posted";journal_entry:number;amount:string}>(`/api/assets/fixed-assets/${id}/post-supplier-payment/`, {method:"POST",body:JSON.stringify({payment})});
  },
  // احتساب وترحيل إهلاك الأصل عن الفترة والسنة المالية.
  depreciate: async (id: number, periodDate: string, fiscalYear: number, unitsProduced?:number, unitsProducedByComponent?:Record<string,number>) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;depreciation_amount:string}>(`/api/assets/fixed-assets/${id}/depreciate/`, {method:"POST",body:JSON.stringify({period_date:periodDate,fiscal_year:fiscalYear,units_produced:unitsProduced,units_produced_by_component:unitsProducedByComponent})}); },
  // جلب جدول الإهلاك والانخفاض والقيمة الدفترية للأصل.
  schedule: (id: number) => request<AssetSchedule>(`/api/assets/fixed-assets/${id}/schedule/`),
  // إثبات خسارة انخفاض القيمة بعد تحديد القيمة القابلة للاسترداد وحسابات القيد.
  impair: async (id:number, payload:{impairment_date:string;recoverable_amount:string;impairment_expense_account:number;accumulated_impairment_account:number;fiscal_year:number}) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;asset_id:number;impairment_date:string;carrying_amount_before:string;recoverable_amount:string;impairment_loss:string;journal_entry:number;accounting_model:"cost";oci_impact:"none_under_cost_model";oci_note:string}>(`/api/assets/fixed-assets/${id}/impair/`, {method:"POST",body:JSON.stringify(payload)});
  },
  // عكس خسارة انخفاض القيمة وفق سقف IAS 36 للقيمة الدفترية الافتراضية.
  reverseImpairment: async (id:number, payload:{reversal_date:string;recoverable_amount:string;reversal_income_account:number;fiscal_year:number}) => {
    await request<{detail:string}>("/api/csrf/");
    return request<{id:number;asset_id:number;reversal_date:string;carrying_amount_before:string;recoverable_amount:string;hypothetical_carrying_amount:string;reversal_amount:string;journal_entry:number;accounting_model:"cost";oci_impact:"none_under_cost_model"}>(`/api/assets/fixed-assets/${id}/reverse-impairment/`, {method:"POST",body:JSON.stringify(payload)});
  },
  // تنفيذ بيع أو استبعاد الأصل وإرسال بيانات التسوية.
  dispose: async (id: number, disposalDate: string, proceeds: string, treasuryAccount: number, fiscalYear: number, unitsProduced?:number, unitsProducedByComponent?:Record<string,number>) => { await request<{detail:string}>("/api/csrf/"); return request<{asset_id:number;disposal_journal:number;accumulated_depreciation:string;net_book_value:string;proceeds:string;gain:string;loss:string}>(`/api/assets/fixed-assets/${id}/dispose/`, {method:"POST",body:JSON.stringify({disposal_date:disposalDate,proceeds,treasury_account:treasuryAccount,fiscal_year:fiscalYear,units_produced:unitsProduced,units_produced_by_component:unitsProducedByComponent})}); },
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
  // رفض طلب إجازة معلق.
  rejectLeave: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<HrLeaveRequest>(`/api/hr/leave-requests/${id}/reject/`, {method:"POST",body:"{}"}); },
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
  // تجهيز صرف السلفة أو إعادة محاولة ترحيلها بعد اعتماد طلب الصرف.
  disburseEmployeeLoan: async (id:number, treasury_account:number) => { await request<{detail:string}>("/api/csrf/"); return request<HrEmployeeLoan>(`/api/hr/employee-loans/${id}/disburse/`, {method:"POST",body:JSON.stringify({treasury_account})}); },
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
  // إلغاء إعداد الصرف قبل ترحيل أي سند، مع إعادة الدورة لحالة الاستحقاق المرحلة.
  cancelPayrollPaymentPreparation: async (id:number) => { await request<{detail:string}>("/api/csrf/"); return request<Record<string,unknown>>(`/api/hr/payroll-cycles/${id}/cancel_payment_preparation/`, {method:"POST"}); },
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
export type HrEmployeeLoan = {id:number;employee:number;employee_name:string;reference:string;issue_date:string;principal:string;installment_count:number;installment_amount:string;receivable_account:number|null;outstanding_balance:string;status:"draft"|"pending"|"active"|"settled"|"suspended";disbursement_transaction:number|null;disbursed_at:string|null;approval_pending?:boolean;detail?:string;notes:string};
