export type Company = { id: number; code: string; name_ar: string; base_currency_code: string };
export type Account = {
  id: number; company: number; code: string; name_ar: string; parent: number | null;
  parent_code: string | null; nature: string; is_group: boolean; allow_posting: boolean; is_active: boolean;
};
export type JournalLine = { id: number; account: number; account_code: string; account_name: string; debit: string; credit: string; description: string };
export type Journal = {
  id: number; company: number; branch: number; branch_code: string; fiscal_year: number;
  fiscal_year_name: string; entry_number: string; entry_date: string; description: string;
  source_type: string; status: string; lines: JournalLine[];
};
async function request<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (response.status === 403 || response.status === 401) {
    throw new Error("يلزم تسجيل الدخول ومنحك صلاحية عرض بيانات المحاسبة. افتح لوحة الإدارة ثم أعد المحاولة.");
  }
  if (!response.ok) throw new Error(`تعذر تحميل البيانات (HTTP ${response.status}).`);
  return response.json() as Promise<T>;
}
export const api = {
  companies: () => request<Company[]>("/api/setup/companies/"),
  accounts: (companyId: number) => request<Account[]>(`/api/accounting/accounts/?company=${companyId}&ordering=code`),
  journals: (companyId: number) => request<Journal[]>(`/api/accounting/journal-entries/?company=${companyId}&ordering=-entry_date`),
};
