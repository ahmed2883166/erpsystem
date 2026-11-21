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
function csrfToken(): string { const item = document.cookie.split("; ").find((part) => part.startsWith("csrftoken=")); return item ? decodeURIComponent(item.split("=").slice(1).join("=")) : ""; }
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" ? { "X-CSRFToken": csrfToken() } : {}), ...options.headers } });
  if (response.status === 403 || response.status === 401) {
    throw new Error("يلزم تسجيل الدخول ومنحك صلاحية عرض بيانات المحاسبة. افتح لوحة الإدارة ثم أعد المحاولة.");
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
};
