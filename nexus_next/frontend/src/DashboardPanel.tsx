/*
 * لوحة المؤشرات المالية: تلخيص نتائج الفترة والميزانية وأعمار مديونيات العملاء والموردين.
 * الفلاتر تحدد الفترة، والنقر على المؤشرات ينقل المستخدم إلى الشاشة التفصيلية المرتبطة.
 */
import { useEffect, useMemo, useState } from "react";
import { api, salesApi, purchasingApi, type BalanceSheetReport, type IncomeStatementReport, type CustomerAging, type SupplierAging } from "./api";

type Props = { companyId: number | null; currency: string; onNavigate: (section: "reports" | "sales-workflow" | "purchasing" | "treasury") => void };

const money = (value?: string | number, currency = "") => `${Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 })} ${currency}`.trim();
const localDate = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const monthStart = (date: Date) => localDate(new Date(date.getFullYear(), date.getMonth(), 1));
const previousMonth = (date: Date) => {
  const start = new Date(date.getFullYear(), date.getMonth() - 1, 1);
  const end = new Date(date.getFullYear(), date.getMonth(), 0);
  return { from: localDate(start), to: localDate(end) };
};

export default function DashboardPanel({ companyId, currency, onNavigate }: Props) {
  const today = localDate(new Date());
  const [from, setFrom] = useState(monthStart(new Date()));
  const [to, setTo] = useState(today);
  const [income, setIncome] = useState<IncomeStatementReport | null>(null);
  const [priorIncome, setPriorIncome] = useState<IncomeStatementReport | null>(null);
  const [balance, setBalance] = useState<BalanceSheetReport | null>(null);
  const [customers, setCustomers] = useState<CustomerAging | null>(null);
  const [suppliers, setSuppliers] = useState<SupplierAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const prior = useMemo(() => previousMonth(new Date(from + "T12:00:00")), [from]);
  const load = async () => {
    if (!companyId || !from || !to || from > to) return;
    setBusy(true); setError("");
    try {
      const [incomeNow, incomeBefore, balanceNow, customerAging, supplierAging] = await Promise.all([
        api.incomeStatement(companyId, from, to),
        api.incomeStatement(companyId, prior.from, prior.to),
        api.balanceSheet(companyId, to),
        salesApi.customerAging(companyId, to),
        purchasingApi.supplierAging(companyId, to),
      ]);
      setIncome(incomeNow); setPriorIncome(incomeBefore); setBalance(balanceNow);
      setCustomers(customerAging); setSuppliers(supplierAging);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل مؤشرات الأداء المالي.");
    } finally { setBusy(false); }
  };

  useEffect(() => { void load(); }, [companyId]);

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
    <div className="work-panel-head"><div><h2>لوحة المؤشرات المالية</h2><p>المؤشرات محسوبة من التقارير المحاسبية والفواتير المرحلة فقط</p></div><span className="coming-soon">{busy ? "جارٍ التحديث…" : "بيانات فعلية"}</span></div>
    <div className="asset-report-filters dashboard-filters">
      <label>من تاريخ<input type="date" value={from} max={to} onChange={e => setFrom(e.target.value)} /></label>
      <label>إلى تاريخ<input type="date" value={to} min={from} max={today} onChange={e => setTo(e.target.value)} /></label>
      <button className="primary-button" type="button" disabled={busy || !companyId || from > to} onClick={() => void load()}>{busy ? "جارٍ التحميل…" : "تحديث المؤشرات"}</button>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {!companyId && <div className="empty-state">اختر شركة لعرض المؤشرات المالية.</div>}
    <div className="metrics-grid finance-kpi-grid">
      {cards.map(card => <button type="button" className="metric-card finance-kpi" key={card.label} onClick={card.action}>
        <div className="metric-top"><span>{card.label}</span><i className={`metric-icon ${card.tone}`}>↗</i></div>
        <strong className="metric-value">{busy ? "…" : money(card.value, currency)}</strong>
        {card.previous !== undefined && <small>الفترة السابقة: {money(card.previous, currency)}</small>}
        {card.previous === undefined && <small>اضغط لفتح التفاصيل</small>}
      </button>)}
    </div>
    {balance && <div className="finance-balance-strip">
      <div><span>حقوق الملكية</span><strong>{money(balance.total_equity, currency)}</strong></div>
      <div><span>الالتزامات وحقوق الملكية</span><strong>{money(balance.liabilities_and_equity, currency)}</strong></div>
      <div><span>فرق الميزانية</span><strong>{money(balance.difference, currency)}</strong></div>
    </div>}
    {(customers || suppliers) && <div className="finance-aging-grid">
      <div className="activity-panel"><div className="activity-title"><div><h2>أعمار مديونية العملاء</h2><p>حتى {to}</p></div><button className="subtle-button" onClick={() => onNavigate("sales-workflow")}>التفاصيل ←</button></div>
        <div className="aging-buckets"><span>غير مستحق {money(customers?.totals.not_due, currency)}</span><span>1–30 يوم {money(customers?.totals["1_30"], currency)}</span><span>31–60 يوم {money(customers?.totals["31_60"], currency)}</span><span>أكثر من 90 {money(customers?.totals.over_90, currency)}</span></div>
      </div>
      <div className="activity-panel"><div className="activity-title"><div><h2>أعمار مستحقات الموردين</h2><p>حتى {to}</p></div><button className="subtle-button" onClick={() => onNavigate("purchasing")}>التفاصيل ←</button></div>
        <div className="aging-buckets"><span>غير مستحق {money(suppliers?.totals.not_due, currency)}</span><span>1–30 يوم {money(suppliers?.totals["1_30"], currency)}</span><span>31–60 يوم {money(suppliers?.totals["31_60"], currency)}</span><span>أكثر من 90 {money(suppliers?.totals.over_90, currency)}</span></div>
      </div>
    </div>}
    <p className="asset-report-note">مقارنة الإيرادات والأرباح والمصروفات بالفترة السابقة للشهر الذي يبدأ فيه نطاق التقرير. لا تعرض اللوحة أرقامًا تقديرية أو بيانات تجريبية؛ عند تعذر تحميل تقرير يظهر الخطأ بدلًا من قيمة صفرية.</p>
  </section>;
}
