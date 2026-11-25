/*
 * لوحة المؤشرات المالية: مؤشرات فعلية حسب الفترة والفرع مع مقارنة بفترة مساوية سابقة.
 * يمنع تجاهل الطلبات القديمة حتى لا تختلط نتائج الشركات أو الفروع عند سرعة التنقل.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { formatMoney } from "./formatters";
import { api, salesApi, purchasingApi, type BalanceSheetReport, type IncomeStatementReport, type CustomerAging, type SupplierAging, type Branch } from "./api";

type Props = {
  companyId: number | null;
  currency: string;
  branches: Branch[];
  onNavigate: (section: "reports" | "sales-workflow" | "purchasing" | "treasury") => void;
};

const money = (value?: string | number | null, currency = "") => {
  if (value === undefined || value === null || value === "") return "—";
  return `${formatMoney(value)} ${currency}`.trim();
};
const localDate = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const monthStart = (date: Date) => localDate(new Date(date.getFullYear(), date.getMonth(), 1));
const shiftDate = (value: string, days: number) => {
  const [year, month, day] = value.split("-").map(Number);
  return localDate(new Date(year, month - 1, day + days, 12));
};
// المقارنة تكون مع الفترة السابقة بنفس عدد الأيام، وليس الشهر السابق كاملًا دائمًا.
const previousPeriod = (from: string, to: string) => {
  const [fy, fm, fd] = from.split("-").map(Number);
  const [ty, tm, td] = to.split("-").map(Number);
  const days = Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86400000) + 1;
  const priorTo = shiftDate(from, -1);
  return { from: shiftDate(priorTo, -days + 1), to: priorTo };
};

// لوحة المؤشرات المالية ومقارنة الفترات وعرض اختصارات الانتقال للتفاصيل.
export default function DashboardPanel({ companyId, currency, branches, onNavigate }: Props) {
  const today = localDate(new Date());
  const [from, setFrom] = useState(monthStart(new Date()));
  const [to, setTo] = useState(today);
  const [branchId, setBranchId] = useState("");
  const [income, setIncome] = useState<IncomeStatementReport | null>(null);
  const [priorIncome, setPriorIncome] = useState<IncomeStatementReport | null>(null);
  const [balance, setBalance] = useState<BalanceSheetReport | null>(null);
  const [customers, setCustomers] = useState<CustomerAging | null>(null);
  const [suppliers, setSuppliers] = useState<SupplierAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);

  const prior = useMemo(() => previousPeriod(from, to), [from, to]);
  const selectedBranch = branches.find((branch) => branch.id === Number(branchId));

  // تحميل القوائم وأعمار الديون بنفس الشركة والفرع وتاريخ التقرير.
  const load = async () => {
    if (!companyId || !from || !to || from > to) {
      setError("راجع الشركة ونطاق التاريخ قبل تحديث المؤشرات.");
      return;
    }
    const requestId = ++requestSequence.current;
    const branch = branchId ? Number(branchId) : undefined;
    setBusy(true);
    setError("");
    try {
      const [incomeNow, incomeBefore, balanceNow, customerAging, supplierAging] = await Promise.all([
        api.incomeStatement(companyId, from, to, branch),
        api.incomeStatement(companyId, prior.from, prior.to, branch),
        api.balanceSheet(companyId, to, branch),
        salesApi.customerAging(companyId, to, undefined, branch),
        purchasingApi.supplierAging(companyId, to, branch),
      ]);
      if (requestId !== requestSequence.current) return;
      setIncome(incomeNow);
      setPriorIncome(incomeBefore);
      setBalance(balanceNow);
      setCustomers(customerAging);
      setSuppliers(supplierAging);
    } catch (reason) {
      if (requestId !== requestSequence.current) return;
      setError(reason instanceof Error ? reason.message : "تعذر تحميل مؤشرات الأداء المالي.");
    } finally {
      if (requestId === requestSequence.current) setBusy(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { requestSequence.current += 1; };
  }, [companyId]);

  const cards = [
    { label: "إيرادات الفترة", value: income?.revenue_total, previous: priorIncome?.revenue_total, tone: "blue", action: () => onNavigate("reports") },
    { label: "مجمل الربح", value: income?.gross_profit, previous: priorIncome?.gross_profit, tone: "green", action: () => onNavigate("reports") },
    { label: "صافي الربح", value: income?.net_profit, previous: priorIncome?.net_profit, tone: "violet", action: () => onNavigate("reports") },
    { label: "مصروفات الفترة", value: income?.expenses_total, previous: priorIncome?.expenses_total, tone: "orange", action: () => onNavigate("reports") },
    { label: "إجمالي الأصول", value: balance?.total_assets, tone: "cyan", action: () => onNavigate("reports") },
    { label: "الالتزامات", value: balance?.total_liabilities, tone: "rose", action: () => onNavigate("reports") },
    { label: "مديونية العملاء", value: customers?.totals.total_due, tone: "violet", action: () => onNavigate("sales-workflow") },
    { label: "مستحقات الموردين", value: suppliers?.totals.total_due, tone: "orange", action: () => onNavigate("purchasing") },
  ];

  return <section className="work-panel finance-dashboard">
    <div className="work-panel-head"><div><h2>لوحة المؤشرات المالية</h2><p>المؤشرات من القيود والفواتير المرحلة فقط — {selectedBranch ? `فرع: ${selectedBranch.name_ar}` : "كل الفروع المسموح بها"}</p></div><span className="coming-soon">{busy ? "جارٍ التحديث…" : "بيانات فعلية"}</span></div>
    <div className="asset-report-filters dashboard-filters">
      <label>من تاريخ<input type="date" value={from} max={to} onChange={e => setFrom(e.target.value)} /></label>
      <label>إلى تاريخ<input type="date" value={to} min={from} max={today} onChange={e => setTo(e.target.value)} /></label>
      <label>الفرع<select value={branchId} onChange={e => setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(branch => <option key={branch.id} value={branch.id}>{branch.name_ar} ({branch.code})</option>)}</select></label>
      <button className="primary-button" type="button" disabled={busy || !companyId || from > to} onClick={() => void load()}>{busy ? "جارٍ التحميل…" : "تحديث المؤشرات"}</button>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {!companyId && <div className="empty-state">اختر شركة لعرض المؤشرات المالية.</div>}
    <div className="metrics-grid finance-kpi-grid">
      {cards.map(card => <button type="button" className="metric-card finance-kpi" key={card.label} onClick={card.action}>
        <div className="metric-top"><span>{card.label}</span><i className={`metric-icon ${card.tone}`}>↗</i></div>
        <strong className="metric-value">{busy ? "…" : money(card.value, currency)}</strong>
        {card.previous !== undefined && <small>الفترة المقارنة ({prior.from} إلى {prior.to}): {money(card.previous, currency)}</small>}
        {card.previous === undefined && <small>اضغط لفتح التفاصيل</small>}
      </button>)}
    </div>
    {balance && <div className="finance-balance-strip">
      <div><span>حقوق الملكية</span><strong>{money(balance.total_equity, currency)}</strong></div>
      <div><span>الالتزامات وحقوق الملكية</span><strong>{money(balance.liabilities_and_equity, currency)}</strong></div>
      <div><span>فرق الميزانية</span><strong>{money(balance.difference, currency)}</strong></div>
    </div>}
    {(customers || suppliers) && <div className="finance-aging-grid">
      <div className="activity-panel"><div className="activity-title"><div><h2>أعمار مديونية العملاء</h2><p>حتى {to} · {selectedBranch ? selectedBranch.name_ar : "كل الفروع"}</p></div><button className="subtle-button" onClick={() => onNavigate("sales-workflow")}>التفاصيل ←</button></div>
        <div className="aging-buckets"><span>غير مستحق {money(customers?.totals.not_due, currency)}</span><span>1–30 يوم {money(customers?.totals["1_30"], currency)}</span><span>31–60 يوم {money(customers?.totals["31_60"], currency)}</span><span>أكثر من 90 {money(customers?.totals.over_90, currency)}</span></div>
      </div>
      <div className="activity-panel"><div className="activity-title"><div><h2>أعمار مستحقات الموردين</h2><p>حتى {to} · {selectedBranch ? selectedBranch.name_ar : "كل الفروع"}</p></div><button className="subtle-button" onClick={() => onNavigate("purchasing")}>التفاصيل ←</button></div>
        <div className="aging-buckets"><span>غير مستحق {money(suppliers?.totals.not_due, currency)}</span><span>1–30 يوم {money(suppliers?.totals["1_30"], currency)}</span><span>31–60 يوم {money(suppliers?.totals["31_60"], currency)}</span><span>أكثر من 90 {money(suppliers?.totals.over_90, currency)}</span></div>
      </div>
    </div>}
    <p className="asset-report-note">المقارنة مع الفترة السابقة مباشرة وبنفس عدد الأيام. القيم غير المحملة تظهر بشرطة بدلًا من صفر حتى لا تُفهم على أنها أرصدة فعلية.</p>
  </section>;
}
