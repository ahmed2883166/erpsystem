/*
 * نقطة تجميع واجهة Admrail ERP v2: إدارة الشركة والفرع والسنة المالية والتنقل بين الوحدات.
 * يحمل الشاشات الكبيرة عند الحاجة، ويربط مكونات المبيعات والمشتريات والتقارير والحسابات.
 */
import { lazy, Suspense, useEffect, useMemo, useState, type FormEvent } from "react";
import LoginPanel from "./LoginPanel";
import OwnerPortalPanel from "./OwnerPortalPanel";
import { api, authApi, salesApi, purchasingApi, treasuryApi, type Account, type Branch, type Company, type FiscalYear, type Journal, type TrialBalance, type CustomerAging, type SupplierAging, type CashFlowReport, type IndirectCashFlowReport, type BalanceSheetReport, type IncomeStatementReport, type GeneralLedgerReport, type EquityChangesReport, type TaxCode, type ThemePreference } from "./api";
const BackupPanel = lazy(() => import("./BackupPanel"));
const CustomersPanel = lazy(() => import("./CustomersPanel"));
const SuppliersPanel = lazy(() => import("./SuppliersPanel"));
const PurchasingPanel = lazy(() => import("./PurchasingPanel"));
const SalesPanel = lazy(() => import("./SalesPanel"));
const InventoryPanel = lazy(() => import("./InventoryPanel"));
const TreasuryPanel = lazy(() => import("./TreasuryPanel"));
const FixedAssetsPanel = lazy(() => import("./FixedAssetsPanel"));
const DashboardPanel = lazy(() => import("./DashboardPanel"));
const PartyStatementsPanel = lazy(() => import("./PartyStatementsPanel"));
const OperationalReconciliationPanel = lazy(() => import("./OperationalReconciliationPanel"));
const SystemMonitorPanel = lazy(() => import("./SystemMonitorPanel"));
const DiscountApprovalsPanel = lazy(() => import("./DiscountApprovalsPanel"));
const CompanyAccessPanel = lazy(() => import("./CompanyAccessPanel"));
const CompanySetupPanel = lazy(() => import("./CompanySetupPanel"));
const AccountsPanel = lazy(() => import("./AccountsPanel"));
const ExpenseAnalysisPanel = lazy(() => import("./ExpenseAnalysisPanel"));
const HrPayrollPanel = lazy(() => import("./HrPayrollPanel"));
const panelFallback = <section className="work-panel"><div className="table-empty">جارٍ تحميل الشاشة…</div></section>;

// استخدام التاريخ المحلي بدل UTC حتى لا يتغير اليوم عند التشغيل في مناطق زمنية مختلفة.
const localISODate = () => {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};

type Section = "overview" | "journals" | "accounts" | "reports" | "tax" | "backup" | "customers" | "sales-workflow" | "suppliers" | "purchasing" | "inventory" | "treasury" | "fixed-assets" | "hr" | "setup" | "audit" | "approvals" | "access";
type DraftLine = { account: string; debit: string; credit: string; description: string };

// تجهيز ملف CSV للتنزيل مع تنسيق القيم وحماية النصوص من كسر الأعمدة.
function downloadCsv(filename: string, headers: string[], rows: (string | number)[][]) {
  const safe = (value: string | number) => {
    const text = String(value ?? "");
    const protectedText = /^[=+@-]/.test(text) || text.startsWith("\t") || text.startsWith("\r") ? "'" + text : text;
    return '"' + protectedText.replace(/"/g, '""') + '"';
  };
  const csv = "\uFEFF" + [headers, ...rows].map(row => row.map(safe).join(";")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.style.display = "none";
  document.body.appendChild(link); link.click(); link.remove();
  URL.revokeObjectURL(url);
}

const navigation: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "نظرة عامة", icon: "◫", group: "الرئيسية" },
  { id: "journals", label: "قيود اليومية", icon: "⇄", group: "المحاسبة" },
  { id: "accounts", label: "دليل الحسابات", icon: "☷", group: "المحاسبة" },
  { id: "customers", label: "العملاء", icon: "♙", group: "المبيعات" },
  { id: "sales-workflow", label: "دورة المبيعات", icon: "↗", group: "المبيعات" },
  { id: "suppliers", label: "الموردون", icon: "♧", group: "المشتريات" },
  { id: "purchasing", label: "دورة المشتريات", icon: "↙", group: "المشتريات" },
  { id: "inventory", label: "المخزون والمستودعات", icon: "▦", group: "المخزون" },
  { id: "treasury", label: "الخزينة والبنوك", icon: "¤", group: "المالية" },
  { id: "fixed-assets", label: "الأصول الثابتة", icon: "▣", group: "المالية" },
  { id: "hr", label: "الموارد البشرية والمرتبات", icon: "♙", group: "الموارد البشرية" },
  { id: "reports", label: "التقارير المالية", icon: "▤", group: "التقارير" },
  { id: "tax", label: "الضرائب والإقرارات", icon: "%", group: "التقارير" },
  { id: "backup", label: "النسخ الاحتياطي", icon: "▣", group: "الإدارة" },
  { id: "setup", label: "إعدادات الشركة", icon: "⚙", group: "الإدارة" },
  { id: "audit", label: "مركز مراقبة النظام", icon: "⌕", group: "الإدارة" },
  { id: "approvals", label: "الموافقات وحدود الخصم", icon: "✓", group: "الإدارة" },
  { id: "access", label: "المستخدمون والصلاحيات", icon: "♙", group: "الإدارة" },
];
const modules = [
  { title: "المحاسبة العامة", detail: "القيود، دليل الحسابات، مراكز التكلفة", icon: "◈", tone: "blue", section: "accounts" as Section },
  { title: "المبيعات والعملاء", detail: "تسليم مخزني وفواتير وتحصيل وأرصدة", icon: "↗", tone: "violet", section: "sales-workflow" as Section },
  { title: "المشتريات والموردون", detail: "الموردون وأذون الاستلام وفواتير الموردين", icon: "↙", tone: "cyan", section: "purchasing" as Section },
  { title: "المخزون والمستودعات", detail: "الأصناف والمجموعات ووحدات القياس والمخازن", icon: "▦", tone: "amber", section: "inventory" as Section },
  { title: "المرتبات والموارد البشرية", detail: "ملفات الموظفين والحضور ودورات المرتبات", icon: "♙", tone: "green", section: "hr" as Section },
  { title: "الأصول الثابتة", detail: "تسجيل الأصول والإهلاك وربط القيود", icon: "▣", tone: "rose", section: "fixed-assets" as Section },
  { title: "الضرائب والإقرارات", detail: "تسوية ضريبة المدخلات والمخرجات وتجهيز الإقرار", icon: "%", tone: "cyan", section: "tax" as Section },
];
const statusNames: Record<string, string> = { draft: "مسودة", posted: "مرحّل", reversed: "معكوس" };
const sourceNames: Record<string, string> = { manual: "يدوي", sales: "مبيعات", purchases: "مشتريات", payroll: "مرتبات", inventory: "مخزون", assets: "أصول", opening: "افتتاحي", closing: "إقفال" };

// شاشة إعداد أكواد الضرائب: تحميل الأكواد وإضافة كود أو تعطيله للشركة.
function TaxCodeSettingsPanel({ companyId, country }: { companyId: number | null; country: "EG" | "SA" | undefined }) {
  const [items, setItems] = useState<TaxCode[]>([]);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState<TaxCode["category"]>("standard");
  const [rate, setRate] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState(localISODate());
  const [effectiveTo, setEffectiveTo] = useState("");
  const [recoverable, setRecoverable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = async () => {
    if (!companyId) { setItems([]); return; }
    try { setItems(await api.taxCodes(companyId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل أكواد الضرائب."); }
  };
  useEffect(() => { setError(""); setNotice(""); void load(); }, [companyId]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !country) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createTaxCode({
        company: companyId, country, code: code.trim(), name_ar: name.trim(), category,
        rate: category === "standard" || category === "reverse_charge" ? rate : "0",
        effective_from: effectiveFrom, effective_to: effectiveTo || null,
        input_tax_recoverable: recoverable, is_active: true,
      });
      setCode(""); setName(""); setRate(""); setEffectiveTo("");
      setNotice("تم حفظ كود الضريبة وفترة سريانه.");
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ كود الضريبة."); }
    finally { setBusy(false); }
  };
  const deactivate = async (item: TaxCode) => {
    if (!window.confirm("تعطيل كود الضريبة؟ سيظل محفوظًا للمراجعة التاريخية.")) return;
    setBusy(true); setError(""); setNotice("");
    try { await api.deactivateTaxCode(item.id); setNotice("تم تعطيل الكود دون حذفه."); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تعطيل الكود."); }
    finally { setBusy(false); }
  };
  const categoryNames: Record<TaxCode["category"], string> = {
    standard: "قياسية", zero_rated: "صفرية", exempt: "معفاة", out_of_scope: "خارج النطاق", reverse_charge: "احتساب عكسي",
  };
  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>أكواد وإعدادات الضرائب</h2><p>تعريف نسب الضريبة حسب بلد الشركة مع تاريخ بداية ونهاية السريان</p></div><span className="coming-soon">{country === "SA" ? "السعودية" : country === "EG" ? "مصر" : "اختر شركة"}</span></div>
    {!companyId && <div className="empty-state">اختر الشركة أولًا لإدارة أكواد الضرائب.</div>}
    {companyId && <>
      <form className="journal-form" onSubmit={submit}>
        <label>كود الضريبة<input required maxLength={32} value={code} onChange={e => setCode(e.target.value)} placeholder="VAT-STD" /></label>
        <label>اسم الضريبة<input required maxLength={160} value={name} onChange={e => setName(e.target.value)} placeholder="ضريبة قيمة مضافة قياسية" /></label>
        <label>الفئة<select value={category} onChange={e => { const next = e.target.value as TaxCode["category"]; setCategory(next); if (next === "zero_rated" || next === "exempt" || next === "out_of_scope") setRate("0"); }}><option value="standard">خاضعة بالنسبة القياسية</option><option value="zero_rated">نسبة صفرية</option><option value="exempt">معفاة</option><option value="out_of_scope">خارج النطاق</option><option value="reverse_charge">احتساب عكسي</option></select></label>
        <label>النسبة %<input required type="number" min="0" max="100" step="0.0001" value={category === "zero_rated" || category === "exempt" || category === "out_of_scope" ? "0" : rate} disabled={category === "zero_rated" || category === "exempt" || category === "out_of_scope"} onChange={e => setRate(e.target.value)} /></label>
        <label>ساري من<input required type="date" value={effectiveFrom} onChange={e => setEffectiveFrom(e.target.value)} /></label>
        <label>ساري حتى<input type="date" value={effectiveTo} min={effectiveFrom} onChange={e => setEffectiveTo(e.target.value)} /></label>
        <label className="form-wide"><span><input type="checkbox" checked={recoverable} onChange={e => setRecoverable(e.target.checked)} /> ضريبة المدخلات قابلة للخصم عند انطباق الشروط</span></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || !country}>{busy ? "جارٍ الحفظ…" : "إضافة كود ضريبي"}</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void load()}>تحديث القائمة</button></div>
      </form>
      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="success-banner">{notice}</div>}
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الاسم</th><th>الفئة</th><th>النسبة</th><th>السريان</th><th>مدخلات قابلة للخصم</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
        {items.map(item => <tr key={item.id}><td className="code-cell">{item.code}</td><td>{item.name_ar}</td><td>{categoryNames[item.category]}</td><td>{Number(item.rate).toLocaleString("ar-EG", {maximumFractionDigits:4})}%</td><td>{item.effective_from} إلى {item.effective_to || "مفتوح"}</td><td>{item.input_tax_recoverable ? "نعم" : "لا"}</td><td>{item.is_active ? "نشط" : "معطل"}</td><td>{item.is_active && <button type="button" className="subtle-button" disabled={busy} onClick={() => void deactivate(item)}>تعطيل</button>}</td></tr>)}
        {!items.length && <tr><td colSpan={8}>لا توجد أكواد ضريبية معرفة لهذه الشركة.</td></tr>}
      </tbody></table></div>
      <p className="asset-report-note">لا يتم تثبيت نسبة قانونية تلقائيًا؛ أدخل النسب المعتمدة لشركتك وتواريخ سريانها بعد مراجعة التشريعات السارية. الإعدادات لا تتصل بمصلحة الضرائب أو هيئة الزكاة.</p>
    </>}
  </section>;
}

// شاشة الإقرار الضريبي: تجميع بيانات الفترة والفرع وعرض ملخص ضريبة القيمة المضافة.
function VATReturnPanel({ companyId }: { companyId: number | null }) {
  const today = localISODate();
  const [from, setFrom] = useState(new Date().getFullYear() + "-01-01");
  const [to, setTo] = useState(today);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchId, setBranchId] = useState("");
  const [report, setReport] = useState<Awaited<ReturnType<typeof api.vatReturn>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setReport(await api.vatReturn(companyId, from, to, branchId ? Number(branchId) : undefined)); }
    catch (reason) { setReport(null); setError(reason instanceof Error ? reason.message : "تعذر تحميل تقرير ضريبة القيمة المضافة."); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!companyId) { setBranches([]); setBranchId(""); setReport(null); return; }
    api.branches(companyId).then(setBranches).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع."));
  }, [companyId]);
  useEffect(() => { if (companyId && from && to && from <= to) void load(); }, [companyId, branchId]);
  const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 });
  const categoryNames: Record<string, string> = {
    standard: "قياسية", zero_rated: "صفرية", exempt: "معفاة", out_of_scope: "خارج النطاق",
    reverse_charge: "احتساب عكسي", legacy_unclassified: "نسبة قديمة غير مصنفة",
  };
  const rows = report ? [
    ...report.sales.by_code.map(row => ["مبيعات", row.tax_code || "بدون كود", categoryNames[row.category] || row.category, row.tax_rate, row.taxable_base, row.tax_amount, "—", row.gross_amount]),
    ...report.purchases.by_code.map(row => ["مشتريات", row.tax_code || "بدون كود", categoryNames[row.category] || row.category, row.tax_rate, row.taxable_base, row.tax_amount, row.recoverable_tax_amount, row.gross_amount]),
  ] : [];
  return <div className="asset-reports">
    <div className="work-panel-head"><div><h2>تسوية ضريبة القيمة المضافة</h2><p>مراجعة المبيعات والمشتريات حسب كود الضريبة وتصنيفها ونسبة السريان والفترة والفرع</p></div><div className="report-actions">
      <button type="button" className="subtle-button" disabled={!report} onClick={() => report && downloadCsv(`vat-reconciliation-${report.date_from}-${report.date_to}.csv`, ["النوع", "كود الضريبة", "التصنيف", "النسبة %", "الوعاء", "قيمة الضريبة", "القابل للخصم", "الإجمالي"], rows)}>تصدير CSV (Excel)</button>
      <button type="button" className="subtle-button" onClick={(event) => { const target = event.currentTarget.closest(".asset-reports"); if (target) { target.classList.add("print-active"); window.print(); target.classList.remove("print-active"); } }}>طباعة / PDF</button>
      <button type="button" className="primary-button" disabled={busy || !companyId || !from || !to || from > to} onClick={() => void load()}>{busy ? "جارٍ الفحص…" : "تحديث التقرير"}</button>
    </div></div>
    <div className="asset-report-filters">
      <label>من تاريخ<input type="date" value={from} onChange={e => setFrom(e.target.value)} /></label>
      <label>إلى تاريخ<input type="date" value={to} onChange={e => setTo(e.target.value)} /></label>
      <label>الفرع<select value={branchId} onChange={e => setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(item => <option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {report && <>
      <div className="asset-cashflow-totals">
        <span>مبيعات خاضعة: {money(report.sales.taxable_base)} {report.currency}</span>
        <span>ضريبة المخرجات: {money(report.output_tax)} {report.currency}</span>
        <span>مشتريات خاضعة: {money(report.purchases.taxable_base)} {report.currency}</span>
        <span>ضريبة المدخلات القابلة للخصم: {money(report.input_tax)} {report.currency}</span>
        <b>صافي الضريبة المستحقة: {money(report.net_tax_payable)} {report.currency}</b>
      </div>
      <div className="data-table-wrap"><table><thead><tr><th>النوع</th><th>الكود</th><th>التصنيف</th><th>النسبة</th><th>الوعاء</th><th>الضريبة</th><th>القابل للخصم</th><th>الإجمالي</th></tr></thead><tbody>
        {report.sales.by_code.map((row, index) => <tr key={`s-${row.tax_code_id ?? "legacy"}-${row.tax_rate}-${index}`}><td>مبيعات</td><td>{row.tax_code || "بدون كود"}</td><td>{categoryNames[row.category] || row.category}</td><td>{row.tax_rate}%</td><td>{money(row.taxable_base)}</td><td>{money(row.tax_amount)}</td><td>—</td><td>{money(row.gross_amount)}</td></tr>)}
        {report.purchases.by_code.map((row, index) => <tr key={`p-${row.tax_code_id ?? "legacy"}-${row.tax_rate}-${index}`}><td>مشتريات</td><td>{row.tax_code || "بدون كود"}</td><td>{categoryNames[row.category] || row.category}</td><td>{row.tax_rate}%</td><td>{money(row.taxable_base)}</td><td>{money(row.tax_amount)}</td><td>{money(row.recoverable_tax_amount)}</td><td>{money(row.gross_amount)}</td></tr>)}
        {!rows.length && <tr><td colSpan={8}>لا توجد فواتير ضريبية مرحلة في الفترة المحددة.</td></tr>}
      </tbody></table></div>
      <p className="asset-report-note">{report.note}</p>
    </>}
  </div>;
}

// تقرير التدفقات النقدية: تحميل وعرض حركة النقد للفترة المختارة.
function CashFlowReportPanel({ companyId }: { companyId: number | null }) {
  const today = localISODate();
  const [from, setFrom] = useState(new Date().getFullYear()+"-01-01");
  const [to, setTo] = useState(today);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchId, setBranchId] = useState("");
  const [report, setReport] = useState<CashFlowReport|null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setReport(await treasuryApi.cashFlow(companyId, from, to, branchId ? Number(branchId) : undefined)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل التدفقات النقدية."); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!companyId) { setBranches([]); setBranchId(""); return; }
    api.branches(companyId).then(items => { setBranches(items); setBranchId(""); })
      .catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع."));
  }, [companyId]);
  useEffect(() => { void load(); }, [companyId, branchId]);
  return <div className="asset-reports">
    <div className="work-panel-head"><div><h2>التدفقات النقدية حسب النشاط</h2><p>الحركات النقدية المرحلة، مع فصل الأنشطة التشغيلية والاستثمارية والتمويلية</p></div><div className="report-actions"><button type="button" className="subtle-button" disabled={!report} onClick={()=>report&&downloadCsv(`cash-flow-${report.date_from||from}-${report.date_to||to}.csv`,["التاريخ","المستند","البيان","الفرع","النشاط","القيمة","رقم القيد"],report.rows.map(x=>[x.date,x.document_number,x.description,x.branch,x.category,x.signed_amount,x.journal_entry||""]))}>تصدير CSV (Excel)</button><button type="button" className="subtle-button" onClick={(event)=>{const target=event.currentTarget.closest(".asset-reports");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة / PDF</button><button className="primary-button" disabled={busy || !companyId} onClick={()=>void load()}>{busy?"جارٍ التحميل…":"تحديث التقرير"}</button></div></div>
    <div className="asset-report-filters">
      <label>من تاريخ<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label>
      <label>إلى تاريخ<input type="date" value={to} onChange={e=>setTo(e.target.value)}/></label>
      <label>الفرع<select value={branchId} onChange={e=>setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(item=><option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {report && <>
      <div className="asset-cashflow-totals">{report.categories.map(item=><span key={item.category}>{item.label}: صافي {Number(item.net).toLocaleString("ar-EG",{maximumFractionDigits:2})} · داخل {Number(item.inflows).toLocaleString("ar-EG",{maximumFractionDigits:2})} · خارج {Number(item.outflows).toLocaleString("ar-EG",{maximumFractionDigits:2})}</span>)}<b>صافي التدفق: {Number(report.net_cash_flow).toLocaleString("ar-EG",{maximumFractionDigits:2})}</b></div>
      <p className="asset-report-note">{report.note}</p>
      <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>المستند</th><th>البيان</th><th>الفرع</th><th>النشاط</th><th>القيمة</th><th>القيد</th></tr></thead><tbody>{report.rows.map(row=><tr key={row.id}><td>{row.date}</td><td>{row.document_number}</td><td>{row.description}</td><td>{row.branch}</td><td>{row.category==="investing"?"استثماري":row.category==="financing"?"تمويلي":"تشغيلي"}</td><td>{Number(row.signed_amount).toLocaleString("ar-EG",{maximumFractionDigits:2})}</td><td>{row.journal_entry||"—"}</td></tr>)}</tbody></table>{!report.rows.length&&<div className="table-empty">لا توجد حركات نقدية مرحلة في الفترة المحددة.</div>}</div>
    </>}
  </div>;
}


// تقرير التدفقات النقدية بالطريقة غير المباشرة مع تسوية صافي الربح إلى النقد.
function IndirectCashFlowReportPanel({ companyId }: { companyId: number | null }) {
  const today = localISODate();
  const [from, setFrom] = useState(new Date().getFullYear()+"-01-01");
  const [to, setTo] = useState(today);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchId, setBranchId] = useState("");
  const [report, setReport] = useState<IndirectCashFlowReport|null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setReport(await treasuryApi.indirectCashFlow(companyId, from, to, branchId ? Number(branchId) : undefined)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل القائمة غير المباشرة."); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!companyId) { setBranches([]); setBranchId(""); return; }
    api.branches(companyId).then(items => { setBranches(items); setBranchId(""); })
      .catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع."));
  }, [companyId]);
  useEffect(() => { void load(); }, [companyId, branchId]);
  const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
  return <div className="asset-reports">
    <div className="work-panel-head"><div><h2>قائمة التدفقات النقدية — الطريقة غير المباشرة</h2><p>صافي الربح والتعديلات غير النقدية ورأس المال العامل ثم الاستثمار والتمويل</p></div><div className="report-actions"><button type="button" className="subtle-button" disabled={!report} onClick={()=>report&&downloadCsv(`indirect-cash-flow-${report.date_from}-${report.date_to}.csv`,["البند","القيمة","ملاحظات"],[["صافي الربح",report.net_profit,""],["تعديلات غير نقدية",report.non_cash_adjustments.net,""],["التدفقات التشغيلية",report.operating_cash_flow,""],["التدفقات الاستثمارية",report.investing_cash_flow,""],["التدفقات التمويلية",report.financing_cash_flow,""],["صافي التغير النقدي",report.net_change_in_cash,""],["التغير النقدي بالدفاتر",report.ledger_cash_change,""],["فرق المطابقة",report.reconciliation_difference,""],...report.working_capital.map(x=>[`رأس مال عامل — ${x.role} — ${x.account_code}`,x.cash_flow_adjustment,x.account_name]),...report.investing_transactions.map(x=>[`استثمار — ${x.document_number}`,x.amount,x.description]),...report.financing_transactions.map(x=>[`تمويل — ${x.document_number}`,x.amount,x.description])])}>تصدير CSV (Excel)</button><button type="button" className="subtle-button" onClick={(event)=>{const target=event.currentTarget.closest(".asset-reports");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة / PDF</button><button className="primary-button" disabled={busy || !companyId} onClick={()=>void load()}>{busy?"جارٍ التحميل…":"تحديث القائمة"}</button></div></div>
    <div className="asset-report-filters">
      <label>من تاريخ<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label>
      <label>إلى تاريخ<input type="date" value={to} onChange={e=>setTo(e.target.value)}/></label>
      <label>الفرع<select value={branchId} onChange={e=>setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(item=><option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {report && <>
      <div className="asset-cashflow-totals">
        <span>صافي الربح: {money(report.net_profit)}</span>
        <span>تعديلات تشغيلية: {money(report.operating_adjustments)}</span>
        <b>صافي التشغيل: {money(report.operating_cash_flow)}</b>
        <span>الاستثمار: {money(report.investing_cash_flow)}</span>
        <span>التمويل: {money(report.financing_cash_flow)}</span>
        <b>صافي التغير النقدي: {money(report.net_change_in_cash)}</b>
      </div>
      <div className="asset-cashflow-totals">
        <span>تغير النقدية بالدفاتر: {money(report.ledger_cash_change)}</span>
        <b>فرق المطابقة: {money(report.reconciliation_difference)}</b>
      </div>
      <div className="asset-report-note">{report.note}</div>
      {!!report.missing_account_roles.length && <div className="error-banner">أدوار حسابات غير مربوطة: {report.missing_account_roles.join("، ")}</div>}
      <h3>تسويات رأس المال العامل</h3>
      <div className="data-table-wrap"><table><thead><tr><th>الدور</th><th>الحساب</th><th>رصيد أول المدة</th><th>رصيد آخر المدة</th><th>التغير</th><th>تأثيره النقدي</th></tr></thead><tbody>
        {report.working_capital.map(row=><tr key={row.role}><td>{row.role}</td><td>{row.account_code} — {row.account_name}</td><td>{money(row.opening_balance)}</td><td>{money(row.closing_balance)}</td><td>{money(row.change)}</td><td>{money(row.cash_flow_adjustment)}</td></tr>)}
      </tbody></table>{!report.working_capital.length&&<div className="table-empty">اربط أدوار الحسابات أولًا لعرض تسويات رأس المال العامل.</div>}</div>
      <h3>التعديلات غير النقدية</h3>
      <div className="asset-cashflow-totals"><span>الإهلاك: {money(report.non_cash_adjustments.depreciation)}</span><span>خسائر الاستبعاد: {money(report.non_cash_adjustments.disposal_loss)}</span><span>أرباح الاستبعاد: {money(report.non_cash_adjustments.disposal_gain)}</span><b>صافي التعديلات: {money(report.non_cash_adjustments.net)}</b></div>
    </>}
  </div>;
}


// شاشة القوائم المالية: الميزانية وقائمة الدخل والتفاصيل وروابط الأستاذ والقيود.
function FinancialStatementsPanel({ companyId }: { companyId: number | null }) {
  const today = localISODate();
  const [from, setFrom] = useState(new Date().getFullYear()+"-01-01");
  const [to, setTo] = useState(today);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchId, setBranchId] = useState("");
  const [balance, setBalance] = useState<BalanceSheetReport|null>(null);
  const [income, setIncome] = useState<IncomeStatementReport|null>(null);
  const [selectedLedger, setSelectedLedger] = useState<GeneralLedgerReport|null>(null);
  const [ledgerBusy, setLedgerBusy] = useState(false);
  const [ledgerError, setLedgerError] = useState("");
  const [selectedJournal, setSelectedJournal] = useState<Journal|null>(null);
  const [journalBusy, setJournalBusy] = useState(false);
  const [journalError, setJournalError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [balanceData, incomeData] = await Promise.all([
        api.balanceSheet(companyId, to, branchId ? Number(branchId) : undefined),
        api.incomeStatement(companyId, from, to, branchId ? Number(branchId) : undefined),
      ]);
      setBalance(balanceData); setIncome(incomeData);
      setSelectedLedger(null); setSelectedJournal(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل القوائم المالية."); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!companyId) { setBranches([]); setBranchId(""); return; }
    api.branches(companyId).then(items => { setBranches(items); setBranchId(""); })
      .catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع."));
  }, [companyId]);
  useEffect(() => { void load(); }, [companyId, branchId]);
  const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
  const openLedger = async (accountId: number | null) => {
    if (!companyId || accountId === null || !balance) return;
    setLedgerBusy(true); setLedgerError(""); setSelectedJournal(null);
    try {
      setSelectedLedger(await api.generalLedger(companyId, accountId, from, balance.as_of, branchId ? Number(branchId) : undefined));
    } catch (reason) { setLedgerError(reason instanceof Error ? reason.message : "تعذر فتح دفتر الأستاذ."); }
    finally { setLedgerBusy(false); }
  };
  const openJournal = async (journalId: number) => {
    setJournalBusy(true); setJournalError("");
    try { setSelectedJournal(await api.journalDetail(journalId)); }
    catch (reason) { setJournalError(reason instanceof Error ? reason.message : "تعذر فتح القيد."); }
    finally { setJournalBusy(false); }
  };
  const rows = (items: {account_id:number|null;code:string;name_ar:string;balance:string}[]) => items.map(item=><tr key={item.account_id ?? item.code}><td>{item.code}</td><td>{item.name_ar}</td><td>{money(item.balance)}</td><td><button type="button" className="text-action-button" disabled={item.account_id===null || ledgerBusy} onClick={()=>void openLedger(item.account_id)}>عرض الحركة ←</button></td></tr>);
  const incomeRows = (items: {account_id:number;code:string;name_ar:string;amount:string}[]) => items.map(item=><tr key={item.account_id}><td>{item.code}</td><td>{item.name_ar}</td><td>{money(item.amount)}</td><td><button type="button" className="text-action-button" disabled={ledgerBusy} onClick={()=>void openLedger(item.account_id)}>عرض الحركة ←</button></td></tr>);
  return <div className="asset-reports">
    <div className="work-panel-head"><div><h2>القوائم المالية</h2><p>الميزانية العمومية وقائمة الدخل من القيود المرحلة فقط</p></div><div className="report-actions"><button type="button" className="subtle-button" disabled={!balance} onClick={()=>balance&&downloadCsv(`balance-sheet-${balance.as_of}.csv`,["القسم","كود الحساب","الحساب","الرصيد"],[...balance.assets.map(x=>["الأصول",x.code,x.name_ar,x.balance]),...balance.liabilities.map(x=>["الالتزامات",x.code,x.name_ar,x.balance]),...balance.equity.map(x=>["حقوق الملكية",x.code,x.name_ar,x.balance])])}>CSV للميزانية (Excel)</button><button type="button" className="subtle-button" onClick={(event)=>{const target=event.currentTarget.closest(".asset-reports");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة / PDF</button><button type="button" className="subtle-button" disabled={!income} onClick={()=>income&&downloadCsv(`income-statement-${income.date_from}-${income.date_to}.csv`,["القسم","كود الحساب","الحساب","القيمة"],[...income.revenues.map(x=>["الإيرادات",x.code,x.name_ar,x.amount]),...income.cost_of_sales.map(x=>["تكلفة المبيعات",x.code,x.name_ar,x.amount]),...income.operating_and_other_expenses.map(x=>["المصروفات",x.code,x.name_ar,x.amount])])}>CSV لقائمة الدخل</button><button className="primary-button" disabled={busy || !companyId} onClick={()=>void load()}>{busy?"جارٍ التحميل…":"تحديث القوائم"}</button></div></div>
    <div className="asset-report-filters">
      <label>من تاريخ قائمة الدخل<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label>
      <label>إلى تاريخ / تاريخ الميزانية<input type="date" value={to} onChange={e=>setTo(e.target.value)}/></label>
      <label>الفرع<select value={branchId} onChange={e=>setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(item=><option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {balance && income && <>
      <h3>الميزانية العمومية حتى {balance.as_of}</h3>
      <div className="asset-cashflow-totals">
        <span>إجمالي الأصول: {money(balance.total_assets)}</span>
        <span>إجمالي الالتزامات: {money(balance.total_liabilities)}</span>
        <span>حقوق الملكية: {money(balance.total_equity)}</span>
        <b>فرق الميزانية: {money(balance.difference)}</b>
      </div>
      {[["الأصول",balance.assets],["الالتزامات",balance.liabilities],["حقوق الملكية",balance.equity]].map(([label,items])=><div key={label as string}>
        <h3>{label as string}</h3>
        <div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>الحساب</th><th>الرصيد</th><th>التفاصيل</th></tr></thead><tbody>{rows(items as BalanceSheetReport["assets"])}</tbody></table></div>
      </div>)}
      <h3>قائمة الدخل من {income.date_from} إلى {income.date_to}</h3>
      <div className="asset-cashflow-totals">
        <span>الإيرادات: {money(income.revenue_total)}</span>
        <span>تكلفة المبيعات: {money(income.cost_of_sales_total)}</span>
        <span>مجمل الربح: {money(income.gross_profit)}</span>
        <span>المصروفات الأخرى: {money(income.expenses_total)}</span>
        <b>صافي الربح: {money(income.net_profit)}</b>
      </div>
      {[["الإيرادات",income.revenues],["تكلفة المبيعات",income.cost_of_sales],["المصروفات التشغيلية والأخرى",income.operating_and_other_expenses]].map(([label,items])=><div key={label as string}>
        <h3>{label as string}</h3>
        <div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>الحساب</th><th>القيمة</th><th>التفاصيل</th></tr></thead><tbody>{incomeRows(items as IncomeStatementReport["revenues"])}</tbody></table></div>
      </div>)}
      <p className="asset-report-note">{income.note}</p>
      {ledgerError && <div className="error-banner">{ledgerError}</div>}
      {ledgerBusy && <div className="table-empty">جارٍ تحميل دفتر الأستاذ…</div>}
      {selectedLedger && <section className="report-drilldown">
        <div className="work-panel-head"><div><h3>دفتر الأستاذ: {selectedLedger.account_code} — {selectedLedger.account_name}</h3><p>من {selectedLedger.date_from} إلى {selectedLedger.date_to}</p></div><button type="button" className="subtle-button" onClick={()=>{setSelectedLedger(null);setSelectedJournal(null);}}>إغلاق التفاصيل</button></div>
        <div className="asset-cashflow-totals"><span>رصيد أول المدة: {money(selectedLedger.opening_balance)}</span><b>رصيد آخر المدة: {money(selectedLedger.closing_balance)}</b></div>
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>رقم القيد</th><th>البيان</th><th>الفرع</th><th>مدين</th><th>دائن</th><th>الرصيد</th></tr></thead><tbody>
          {selectedLedger.rows.map(line=><tr key={line.line_id}><td>{line.date}</td><td><button type="button" className="text-action-button" onClick={()=>void openJournal(line.entry_id)}>{line.entry_number}</button></td><td>{line.description}</td><td>{line.branch}</td><td>{money(line.debit)}</td><td>{money(line.credit)}</td><td>{money(line.running_balance)}</td></tr>)}
        </tbody></table>{!selectedLedger.rows.length&&<div className="table-empty">لا توجد حركات خلال الفترة المحددة.</div>}</div>
        {journalBusy && <div className="table-empty">جارٍ تحميل القيد…</div>}
        {journalError && <div className="error-banner">{journalError}</div>}
        {selectedJournal && <div className="journal-detail-inline">
          <div className="work-panel-head"><div><h3>القيد {selectedJournal.entry_number}</h3><p>{selectedJournal.entry_date} · {selectedJournal.description} · {selectedJournal.status}</p></div><button type="button" className="subtle-button" onClick={()=>setSelectedJournal(null)}>إغلاق القيد</button></div>
          <div className="data-table-wrap"><table><thead><tr><th>الحساب</th><th>البيان</th><th>مدين</th><th>دائن</th></tr></thead><tbody>
            {selectedJournal.lines.map(line=><tr key={line.id}><td>{line.account_code} — {line.account_name}</td><td>{line.description}</td><td>{money(line.debit)}</td><td>{money(line.credit)}</td></tr>)}
          </tbody></table></div>
        </div>}
      </section>}
    </>}
    {!balance && !busy && !error && <div className="table-empty">اختر الفترة ثم حدّث القوائم المالية.</div>}
  </div>;
}


// شاشة الأستاذ العام وحقوق الملكية: عرض الحركات والتغيرات المرتبطة بالحسابات.
function LedgerAndEquityPanel({ companyId }: { companyId: number | null }) {
  const today = localISODate();
  const [from, setFrom] = useState(new Date().getFullYear()+"-01-01");
  const [to, setTo] = useState(today);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState("");
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchId, setBranchId] = useState("");
  const [ledger, setLedger] = useState<GeneralLedgerReport|null>(null);
  const [equity, setEquity] = useState<EquityChangesReport|null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!companyId) { setAccounts([]); setAccountId(""); return; }
    api.accounts(companyId).then(items => {
      setAccounts(items);
      setAccountId(String(items.find(item=>!item.is_group)?.id || ""));
      setLedger(null); setEquity(null);
    }).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الحسابات."));
    api.branches(companyId).then(items => { setBranches(items); setBranchId(""); })
      .catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع."));
  }, [companyId]);
  const load = async () => {
    if (!companyId || !accountId) return;
    setBusy(true); setError("");
    try {
      const [ledgerData, equityData] = await Promise.all([
        api.generalLedger(companyId, Number(accountId), from, to, branchId ? Number(branchId) : undefined),
        api.changesInEquity(companyId, from, to, branchId ? Number(branchId) : undefined),
      ]);
      setLedger(ledgerData); setEquity(equityData);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل دفتر الأستاذ والتغيرات في حقوق الملكية."); }
    finally { setBusy(false); }
  };
  const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
  return <div className="asset-reports">
    <div className="work-panel-head"><div><h2>دفتر الأستاذ والتغيرات في حقوق الملكية</h2><p>كشف حركة تفصيلي للحساب مع رصيد متحرك، وتسوية حقوق الملكية خلال الفترة</p></div><div className="report-actions"><button type="button" className="subtle-button" disabled={!ledger} onClick={()=>ledger&&downloadCsv(`general-ledger-${ledger.account_code}-${from}-${to}.csv`,["التاريخ","رقم القيد","البيان","الفرع","مدين","دائن","الرصيد"],ledger.rows.map(x=>[x.date,x.entry_number,x.description,x.branch,x.debit,x.credit,x.running_balance]))}>CSV لدفتر الأستاذ</button><button type="button" className="subtle-button" onClick={(event)=>{const target=event.currentTarget.closest(".asset-reports");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة / PDF</button><button type="button" className="subtle-button" disabled={!equity} onClick={()=>equity&&downloadCsv(`equity-changes-${from}-${to}.csv`,["كود الحساب","الحساب","رصيد أول المدة","حركة الفترة","الرصيد قبل الربح"],equity.accounts.map(x=>[x.code,x.name_ar,x.opening_balance,x.period_movement,x.closing_balance_before_profit]))}>CSV لحقوق الملكية</button><button className="primary-button" disabled={busy || !companyId || !accountId} onClick={()=>void load()}>{busy?"جارٍ التحميل…":"عرض التقريرين"}</button></div></div>
    <div className="asset-report-filters">
      <label>الحساب<select value={accountId} onChange={e=>setAccountId(e.target.value)}><option value="">اختر الحساب</option>{accounts.filter(item=>!item.is_group).map(item=><option key={item.id} value={item.id}>{item.code} — {item.name_ar}</option>)}</select></label>
      <label>من تاريخ<input type="date" value={from} onChange={e=>setFrom(e.target.value)}/></label>
      <label>إلى تاريخ<input type="date" value={to} onChange={e=>setTo(e.target.value)}/></label>
      <label>الفرع<select value={branchId} onChange={e=>setBranchId(e.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map(item=><option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {ledger && <>
      <h3>دفتر الأستاذ: {ledger.account_code} — {ledger.account_name}</h3>
      <div className="asset-cashflow-totals"><span>رصيد أول المدة: {money(ledger.opening_balance)}</span><b>رصيد آخر المدة: {money(ledger.closing_balance)}</b></div>
      <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>رقم القيد</th><th>البيان</th><th>الفرع</th><th>مدين</th><th>دائن</th><th>الرصيد المتحرك</th></tr></thead><tbody>
        {ledger.rows.map(row=><tr key={row.line_id}><td>{row.date}</td><td>{row.entry_number}</td><td>{row.description}</td><td>{row.branch}</td><td>{money(row.debit)}</td><td>{money(row.credit)}</td><td>{money(row.running_balance)}</td></tr>)}
      </tbody></table>{!ledger.rows.length&&<div className="table-empty">لا توجد حركات على الحساب خلال الفترة.</div>}</div>
    </>}
    {equity && <>
      <h3>قائمة التغيرات في حقوق الملكية</h3>
      <div className="asset-cashflow-totals"><span>حقوق الملكية أول المدة: {money(equity.opening_equity)}</span><span>حركة الحسابات: {money(equity.equity_movements)}</span><span>صافي ربح الفترة: {money(equity.period_net_profit)}</span><b>حقوق الملكية آخر المدة: {money(equity.closing_equity)}</b></div>
      <div className="data-table-wrap"><table><thead><tr><th>الحساب</th><th>أول المدة</th><th>الحركة</th><th>آخر المدة قبل الربح</th></tr></thead><tbody>
        {equity.accounts.map(row=><tr key={row.account_id}><td>{row.code} — {row.name_ar}</td><td>{money(row.opening_balance)}</td><td>{money(row.period_movement)}</td><td>{money(row.closing_balance_before_profit)}</td></tr>)}
      </tbody></table></div>
      <p className="asset-report-note">{equity.note}</p>
    </>}
  </div>;
}

// التطبيق الرئيسي: إدارة حالة الجلسة والشركة والفرع والتنقل وعمليات القيود.
export default function App() {
  const [authStatus, setAuthStatus] = useState<"checking" | "signed-out" | "signed-in">("checking");
  const [authRefresh, setAuthRefresh] = useState(0);
  const [isPlatformOwner, setIsPlatformOwner] = useState(false);
  const [themePreference, setThemePreference] = useState<ThemePreference>("system");
  const [systemPrefersDark, setSystemPrefersDark] = useState(() => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false);
  const effectiveTheme = themePreference === "system" ? (systemPrefersDark ? "dark" : "light") : themePreference;
  const [active, setActive] = useState<Section>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [companyId, setCompanyId] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [journals, setJournals] = useState<Journal[]>([]);
  const [trialBalance, setTrialBalance] = useState<TrialBalance | null>(null);
  const [trialBalanceFrom, setTrialBalanceFrom] = useState(new Date().getFullYear()+"-01-01");
  const [trialBalanceTo, setTrialBalanceTo] = useState(localISODate());
  const [excludeClosingEntries, setExcludeClosingEntries] = useState(false);
  const [customerAging, setCustomerAging] = useState<CustomerAging | null>(null);
  const [supplierAging, setSupplierAging] = useState<SupplierAging | null>(null);
  const [agingLoading, setAgingLoading] = useState(false);
  const [agingError, setAgingError] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [journalModal, setJournalModal] = useState(false);
  const [reviewingJournal, setReviewingJournal] = useState(false);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [fiscalYears, setFiscalYears] = useState<FiscalYear[]>([]);
  const [saving, setSaving] = useState(false);
  const [postingId, setPostingId] = useState<number | null>(null);
  const [reversingId, setReversingId] = useState<number | null>(null);
  const [selectedJournal, setSelectedJournal] = useState<Journal | null>(null);
  const [journalError, setJournalError] = useState("");
  const [draft, setDraft] = useState({ branch: "", fiscal_year: "", entry_number: "", entry_date: localISODate(), description: "" });
  const [journalLines, setJournalLines] = useState<DraftLine[]>([{ account: "", debit: "", credit: "", description: "" }, { account: "", debit: "", credit: "", description: "" }]);
  const current = useMemo(() => navigation.find((item) => item.id === active), [active]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemPrefersDark(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setAuthStatus("checking");
    authApi.current().then(async (session) => {
      if (cancelled) return;
      if (!session.authenticated) {
        setAuthStatus("signed-out");
        return;
      }
      setIsPlatformOwner(Boolean(session.user?.is_platform_owner));
      setThemePreference(session.user?.theme_preference || "system");
      setAuthStatus("signed-in");
      try {
        const items = await api.companies();
        if (cancelled) return;
        setCompanies(items);
        if (items.length && !session.user?.is_platform_owner) setCompanyId(items[0].id);
        else if (!session.user?.is_platform_owner) setActive("setup");
      } catch (reason) {
        if (cancelled) return;
        const failure = reason as Error & { status?: number };
        setError(failure.message);
        // رفض الصلاحية لا ينهي الجلسة؛ أما 401 فيعني أن الجلسة غير صالحة.
        setAuthStatus(failure.status === 401 ? "signed-out" : "signed-in");
      }
    }).catch((reason: Error) => {
      if (cancelled) return;
      setError(reason.message);
      setAuthStatus("signed-out");
    });
    return () => { cancelled = true; };
  }, [authRefresh]);

  useEffect(() => {
    if (!companyId) return;
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([api.accounts(companyId), api.journals(companyId)])
      .then(([accountItems, journalItems]) => {
        if (!cancelled) { setAccounts(accountItems); setJournals(journalItems); }
      })
      .catch((reason: Error) => { if (!cancelled) setError(reason.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [companyId]);

  useEffect(() => {
    if (!companyId) { setBranches([]); setFiscalYears([]); return; }
    Promise.all([api.branches(companyId), api.fiscalYears(companyId)]).then(([branchRows, yearRows]) => {
      setBranches(branchRows.filter((row) => row.is_active && row.company === companyId)); setFiscalYears(yearRows.filter((row) => row.company === companyId));
      setDraft((old) => ({ ...old, branch: String(branchRows.find((row) => row.is_active && row.company === companyId)?.id || ""), fiscal_year: String(yearRows.find((row) => row.company === companyId && row.status === "open")?.id || "") }));
    }).catch((reason: Error) => setJournalError(reason.message));
  }, [companyId]);

  useEffect(() => {
    if (active !== "overview" || !companyId) return;
    let cancelled = false;
    setAgingLoading(true); setAgingError("");
    Promise.all([
      salesApi.customerAging(companyId),
      purchasingApi.supplierAging(companyId),
    ]).then(([customers, suppliers]) => {
      if (!cancelled) { setCustomerAging(customers); setSupplierAging(suppliers); }
    }).catch((reason: Error) => {
      if (!cancelled) setAgingError(reason.message || "تعذر تحميل أرصدة العملاء والموردين.");
    }).finally(() => { if (!cancelled) setAgingLoading(false); });
    return () => { cancelled = true; };
  }, [active, companyId]);

  const debitTotal = journalLines.reduce((sum, line) => sum + (Number(line.debit) || 0), 0);
  const creditTotal = journalLines.reduce((sum, line) => sum + (Number(line.credit) || 0), 0);
  const journalDifference = debitTotal - creditTotal;
  // التحقق من اكتمال مسودة القيد وتوازن أطرافه قبل السماح بالحفظ أو الترحيل.
const validateDraft = () => {
    setJournalError("");
    if (!companyId || !draft.branch || !draft.fiscal_year) { setJournalError("حدد الشركة والفرع والسنة المالية."); return; }
    const selectedYear = fiscalYears.find((year) => year.id === Number(draft.fiscal_year));
    if (!selectedYear || selectedYear.status !== "open") { setJournalError("لا يمكن تسجيل القيد إلا في سنة مالية مفتوحة."); return; }
    if (draft.entry_date < selectedYear.start_date || draft.entry_date > selectedYear.end_date) { setJournalError("تاريخ القيد خارج حدود السنة المالية المحددة."); return; }
    if (journalLines.length < 2 || journalLines.some((line) => !line.account || (Number(line.debit) > 0) === (Number(line.credit) > 0) || Number(line.debit) < 0 || Number(line.credit) < 0)) { setJournalError("راجع كل سطر: اختر الحساب وأدخل قيمة في خانة مدين أو دائن فقط."); return; }
    if (Math.abs(journalDifference) > 0.00005) { setJournalError("القيد غير متوازن. يجب أن يتساوى إجمالي المدين والدائن."); return; }
    return true;
  };
  // حفظ القيد كمسودة عبر واجهة الخادم ثم تحديث بيانات الشاشة.
const saveDraft = async () => {
    if (!validateDraft()) return;
    const targetCompanyId = companyId;
    if (targetCompanyId === null) return;
    setSaving(true);
    try {
      await api.createJournal({ company: companyId, branch: Number(draft.branch), fiscal_year: Number(draft.fiscal_year), entry_number: draft.entry_number, entry_date: draft.entry_date, description: draft.description, source_type: "manual", lines: journalLines.map((line) => ({ account: Number(line.account), debit: line.debit || "0", credit: line.credit || "0", description: line.description || draft.description })) });
      const rows = await api.journals(targetCompanyId); setJournals(rows); setJournalModal(false); setReviewingJournal(false);
      setDraft((old) => ({ ...old, entry_number: "", description: "" }));
      setJournalLines([{ account: "", debit: "", credit: "", description: "" }, { account: "", debit: "", credit: "", description: "" }]);
    } catch (reason) { setJournalError(reason instanceof Error ? reason.message : "تعذر حفظ القيد."); }
    finally { setSaving(false); }
  };
  // طلب ترحيل القيد المحدد وتحديث قائمة القيود بعد استجابة الخادم.
const postDraft = async (entry: Journal) => {
    if (!window.confirm("تأكيد ترحيل القيد " + entry.entry_number + "؟ لن يمكن تعديله بعد الترحيل.")) return;
    setJournalError(""); setPostingId(entry.id);
    try { await api.postJournal(entry.id); if (companyId) setJournals(await api.journals(companyId)); setSelectedJournal(null); }
    catch (reason) { setJournalError(reason instanceof Error ? reason.message : "تعذر ترحيل القيد."); }
    finally { setPostingId(null); }
  };
  // عكس قيد مرحّل بعد التأكيد، ثم تحديث قائمة القيود وإظهار نتيجة العملية.
  const reverseJournal = async (entry: Journal) => {
    if (!window.confirm("سيتم إنشاء قيد عكسي وترحيله وربطه بالقيد " + entry.entry_number + ". هل تريد المتابعة؟")) return;
    setJournalError(""); setReversingId(entry.id);
    try {
      await api.reverseJournal(entry.id);
      if (companyId) setJournals(await api.journals(companyId));
      if (selectedJournal?.id === entry.id) setSelectedJournal(null);
    } catch (reason) {
      setJournalError(reason instanceof Error ? reason.message : "تعذر عكس القيد.");
    } finally {
      setReversingId(null);
    }
  };
  const filteredJournals = useMemo(() => journals.filter((entry) =>
    `${entry.entry_number} ${entry.description}`.toLowerCase().includes(search.toLowerCase())), [journals, search]);
  const selectedCompany = companies.find((company) => company.id === companyId);
  const today = new Intl.DateTimeFormat("ar-EG", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(new Date());

  useEffect(() => {
    if (active !== "reports" || !companyId) return;
    let cancelled = false;
    setLoading(true);
    api.trialBalance(companyId, trialBalanceFrom, trialBalanceTo, excludeClosingEntries).then((result) => { if (!cancelled) setTrialBalance(result); })
      .catch((reason: Error) => { if (!cancelled) setError(reason.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [active, companyId, trialBalanceFrom, trialBalanceTo, excludeClosingEntries]);

  const changeThemePreference = async (next: ThemePreference) => {
    const previous = themePreference;
    setThemePreference(next);
    try {
      await authApi.updateThemePreference(next);
    } catch (reason) {
      setThemePreference(previous);
      setError(reason instanceof Error ? reason.message : "تعذر حفظ تفضيل المظهر.");
    }
  };

  const logoutCurrentUser = async () => {
    try {
      await authApi.logout();
      setAuthStatus("signed-out");
      setIsPlatformOwner(false);
      setCompanies([]); setCompanyId(null); setAccounts([]); setJournals([]);
      setSelectedJournal(null); setSidebarOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تسجيل الخروج.");
    }
  };

  if (authStatus === "checking") {
    return <main className="auth-screen" dir="rtl"><div className="auth-card"><div className="table-empty">جارٍ التحقق من جلسة الدخول…</div></div></main>;
  }
  if (authStatus === "signed-out") {
    return <LoginPanel onLogin={() => setAuthRefresh((value) => value + 1)} />;
  }
  if (isPlatformOwner) {
    return <OwnerPortalPanel companies={companies} onCompaniesChanged={setCompanies} onLogout={logoutCurrentUser} />;
  }

  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`} dir="rtl" data-theme={effectiveTheme}>
      {sidebarOpen && <button className="mobile-scrim" aria-label="إغلاق القائمة" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand-lockup"><div className="brand-symbol">A</div><div><strong>ADMRAIL</strong><span>ENTERPRISE PLATFORM</span></div>
          <button className="sidebar-collapse-toggle" type="button" onClick={() => setSidebarCollapsed(value => !value)} aria-label={sidebarCollapsed ? "توسيع القائمة" : "طي القائمة"} title={sidebarCollapsed ? "توسيع القائمة" : "طي القائمة"}>{sidebarCollapsed ? "⟨" : "⟩"}</button><button className="sidebar-close" onClick={() => setSidebarOpen(false)} aria-label="إغلاق القائمة">×</button></div>
        <div className="workspace"><div className="workspace-avatar">AE</div><div><strong>مساحة العمل</strong><small>{selectedCompany?.name_ar || "اختر الشركة"}</small></div>
          <select aria-label="الشركة" value={companyId ?? ""} onChange={(event) => setCompanyId(Number(event.target.value) || null)}>
            <option value="">—</option>{companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar}</option>)}
          </select></div>
        <nav className="navigation">
          {["الرئيسية", "المحاسبة", "المبيعات", "المشتريات", "المخزون", "المالية", "الموارد البشرية", "التقارير", "الإدارة"].map((group) => <div className="nav-group" key={group}><p>{group}</p>
            {navigation.filter((item) => item.group === group).map((item) => <button key={item.id} className={`nav-item ${active === item.id ? "nav-active" : ""}`} title={item.label} aria-label={item.label}
              onClick={() => { setActive(item.id); setSearch(""); setSidebarOpen(false); }}><span className="nav-icon">{item.icon}</span><span>{item.label}</span>{active === item.id && <i />}</button>)}
          </div>)}
        </nav>
        <div className="sidebar-bottom"><a className="admin-link" href="/admin/" target="_blank" rel="noreferrer">فتح لوحة إدارة النظام ↗</a>
          <div className="user-profile"><div className="user-avatar">A</div><div><strong>Admrail ERP v2</strong><small>منصة إدارة الأعمال</small></div><span>•••</span></div></div>
      </aside>
      <main className="main-area">
        <header className="topbar"><div className="topbar-start"><button className="menu-toggle" onClick={() => setSidebarOpen(true)} aria-label="فتح القائمة">☰</button>
          <div className="breadcrumb"><span>Admrail ERP v2</span><b>/</b>{current?.label}</div></div>
          <div className="topbar-actions"><div className="period-select"><span className="period-dot" /> {selectedCompany?.name_ar || "لا توجد شركة"} <span>⌄</span></div>
            <label className="theme-preference" aria-label="مظهر الواجهة"><span>المظهر</span><select className="theme-select" value={themePreference} onChange={event => void changeThemePreference(event.target.value as ThemePreference)}><option value="light">فاتح</option><option value="dark">داكن</option><option value="system">حسب الجهاز</option></select></label>
            <button className="icon-button" aria-label="البحث" onClick={() => document.getElementById("global-search")?.focus()}>⌕</button>
            <a className="admin-shortcut" href="/admin/" target="_blank" rel="noreferrer">الإدارة</a>
            <button type="button" className="subtle-button" onClick={() => void logoutCurrentUser()}>تسجيل الخروج</button></div></header>
        <div className="page-content">
          <section className="welcome-row"><div><p className="eyebrow">{today} · Admrail ERP v2</p><h1>{active === "overview" ? "أهلاً بك في Admrail ERP v2" : current?.label}</h1>
            <p className="welcome-copy">{active === "overview" ? "نظرة موحدة على أعمال شركتك وبياناتها." : "بيانات مباشرة من خدمات النظام المحاسبية."}</p></div>
            {companyId !== null && active !== "customers" && active !== "suppliers" && active !== "purchasing" && active !== "inventory" && <button className="primary-button" onClick={() => { setActive("journals"); setReviewingJournal(false); setJournalLines([{ account: "", debit: "", credit: "", description: "" }, { account: "", debit: "", credit: "", description: "" }]); setJournalModal(true); setJournalError(""); }}><span>＋</span> قيد جديد</button>}</section>

          {error && <div className="error-banner"><strong>تعذر تحميل البيانات</strong><span>{error}</span></div>}
          {active === "overview" && <Suspense fallback={panelFallback}><DashboardPanel key={companyId ?? "none"} companyId={companyId} currency={selectedCompany?.base_currency_code || ""} branches={branches} onNavigate={(section) => setActive(section)} /></Suspense>}
          {active === "overview" && <>
            <div className="section-heading"><div><h2>ملخص البيانات</h2><p>الأرقام التالية مبنية على السجلات المحملة من قاعدة البيانات</p></div><span className="module-count">{loading ? "جارٍ التحديث…" : "تحديث مباشر"}</span></div>
            <div className="metrics-grid">
              {[
                { label: "عدد الحسابات", value: accounts.length.toLocaleString("ar-EG"), note: "دليل الحسابات للشركة", icon: "☷", tone: "blue" },
                { label: "إجمالي قيود اليومية", value: journals.length.toLocaleString("ar-EG"), note: "كل حالات القيود", icon: "⇄", tone: "orange" },
                { label: "القيود المرحلة", value: journals.filter((item) => item.status === "posted").length.toLocaleString("ar-EG"), note: "قيود معتمدة محاسبياً", icon: "✓", tone: "green" },
                { label: "القيود المسودة", value: journals.filter((item) => item.status === "draft").length.toLocaleString("ar-EG"), note: "تحتاج إلى مراجعة وترحيل", icon: "◷", tone: "purple" },
              ].map((metric) => <article className="metric-card" key={metric.label}><div className="metric-top"><span>{metric.label}</span><i className={`metric-icon ${metric.tone}`}>{metric.icon}</i></div>
                <strong className="metric-value">{loading ? "…" : metric.value}</strong><small>{metric.note}</small></article>)}
            </div>
            <div className="section-heading module-heading"><div><h2>موقف الذمم المدينة والدائنة</h2><p>أرصدة الفواتير المرحلة حتى تاريخ اليوم، مع تفاصيل أعمار الاستحقاق</p></div><span className="module-count">{agingLoading ? "جارٍ التحديث…" : customerAging && supplierAging ? "محدث" : "—"}</span></div>
            {agingError && <div className="error-banner">{agingError}</div>}
            <div className="metrics-grid receivables-payables-grid">
              <button type="button" className="metric-card aging-metric-card" onClick={() => setActive("sales-workflow")}>
                <div className="metric-top"><span>مديونية العملاء</span><i className="metric-icon violet">↗</i></div>
                <strong className="metric-value">{agingLoading ? "…" : Number(customerAging?.totals.total_due || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong>
                <small>اضغط لفتح تقرير أعمار مديونية العملاء</small>
                <div className="aging-buckets"><span>غير مستحق {Number(customerAging?.totals.not_due || 0).toLocaleString("ar-EG", { maximumFractionDigits: 0 })}</span><span>متأخر {Number((Number(customerAging?.totals.total_due || 0) - Number(customerAging?.totals.not_due || 0))).toLocaleString("ar-EG", { maximumFractionDigits: 0 })}</span></div>
              </button>
              <button type="button" className="metric-card aging-metric-card" onClick={() => setActive("purchasing")}>
                <div className="metric-top"><span>مستحقات الموردين</span><i className="metric-icon orange">↙</i></div>
                <strong className="metric-value">{agingLoading ? "…" : Number(supplierAging?.totals.total_due || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong>
                <small>اضغط لفتح تقرير أعمار مديونية الموردين</small>
                <div className="aging-buckets"><span>غير مستحق {Number(supplierAging?.totals.not_due || 0).toLocaleString("ar-EG", { maximumFractionDigits: 0 })}</span><span>متأخر {Number((Number(supplierAging?.totals.total_due || 0) - Number(supplierAging?.totals.not_due || 0))).toLocaleString("ar-EG", { maximumFractionDigits: 0 })}</span></div>
              </button>
            </div>
            <div className="section-heading module-heading"><div><h2>وحدات النظام</h2><p>البنية الأساسية للوحدات الرئيسية</p></div><span className="module-count">7 وحدات أساسية</span></div>
            <div className="modules-grid">{modules.map((module) => <button className="module-card" key={module.title} onClick={() => setActive(module.section)}>
              <i className={`module-icon ${module.tone}`}>{module.icon}</i><div><strong>{module.title}</strong><small>{module.detail}</small></div><span className="module-arrow">←</span></button>)}</div>
            <section className="activity-panel"><div className="activity-title"><div><h2>أحدث قيود اليومية</h2><p>آخر خمس حركات مسجلة</p></div><button className="subtle-button" onClick={() => setActive("journals")}>عرض الكل ←</button></div>
              {journals.slice(0, 5).length ? <div className="data-table-wrap"><table><thead><tr><th>رقم القيد</th><th>التاريخ</th><th>البيان</th><th>الحالة</th></tr></thead><tbody>{journals.slice(0, 5).map((entry) => <tr key={entry.id}><td>{entry.entry_number}</td><td>{entry.entry_date}</td><td>{entry.description}</td><td><span className={`status-pill ${entry.status}`}>{statusNames[entry.status] || entry.status}</span></td></tr>)}</tbody></table></div>
                : <div className="empty-state"><div className="empty-graphic">◷</div><strong>{loading ? "جارٍ تحميل القيود" : "لا توجد قيود حتى الآن"}</strong><p>ستظهر القيود هنا بمجرد تسجيلها في الشركة المختارة.</p></div>}</section>
          </>}

          {active === "accounts" && <Suspense fallback={panelFallback}><AccountsPanel companyId={companyId} companyName={selectedCompany?.name_ar || "الشركة"} activity={selectedCompany?.activity} accounts={accounts} onRefresh={async () => { if (!companyId) return; const rows = await api.accounts(companyId); setAccounts(rows); }} /></Suspense>}

          {active === "journals" && <section className="work-panel"><div className="work-panel-head"><div><h2>قيود اليومية</h2><p>سجل القيود وحالات الترحيل للشركة المحددة</p></div><button className="primary-button" onClick={() => { setReviewingJournal(false); setJournalLines([{ account: "", debit: "", credit: "", description: "" }, { account: "", debit: "", credit: "", description: "" }]); setJournalModal(true); setJournalError(""); }}>＋ قيد جديد</button></div>
            {journalError && <div className="error-banner">{journalError}</div>}<div className="table-toolbar"><input id="global-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث برقم القيد أو البيان…" /><span className="module-count">{filteredJournals.length} نتيجة</span></div>
            <div className="data-table-wrap"><table><thead><tr><th>رقم القيد</th><th>التاريخ</th><th>البيان</th><th>الفرع</th><th>المصدر</th><th>الحالة</th><th>إجراء</th></tr></thead>
              <tbody>{filteredJournals.map((entry) => <tr key={entry.id}><td className="code-cell">{entry.entry_number}</td><td>{entry.entry_date}</td><td>{entry.description}</td><td>{entry.branch_code}</td><td>{sourceNames[entry.source_type] || entry.source_type}</td><td><span className={`status-pill ${entry.status}`}>{statusNames[entry.status] || entry.status}</span></td><td>{entry.status === "draft" ? <button className="subtle-button" onClick={() => { setJournalError(""); setSelectedJournal(entry); }}>مراجعة</button> : entry.status === "posted" ? <button className="subtle-button" disabled={reversingId === entry.id} onClick={() => void reverseJournal(entry)}>{reversingId === entry.id ? "جارٍ العكس…" : "عكس القيد"}</button> : "—"}</td></tr>)}</tbody></table>
              {!loading && !filteredJournals.length && <div className="table-empty">لا توجد قيود مطابقة للبحث.</div>}</div></section>}

          {active === "reports" && <section className="work-panel"><div className="work-panel-head"><div><h2>ميزان المراجعة</h2><p>أرصدة الحسابات من القيود المرحلة فقط — للشركة المحددة</p></div><span className="coming-soon">{trialBalance?.accounts.length ?? 0} حساب</span></div>
            <div className="asset-report-filters">
              <label>من تاريخ<input type="date" value={trialBalanceFrom} onChange={e=>setTrialBalanceFrom(e.target.value)}/></label>
              <label>إلى تاريخ<input type="date" value={trialBalanceTo} onChange={e=>setTrialBalanceTo(e.target.value)}/></label>
              <label className="checkbox-filter"><input type="checkbox" checked={excludeClosingEntries} onChange={e=>setExcludeClosingEntries(e.target.checked)}/> استبعاد قيود الإقفال</label>
            </div>
            {trialBalance && <><div className="metrics-grid report-metrics">
              <article className="metric-card"><div className="metric-top">إجمالي المدين</div><strong className="metric-value">{Number(trialBalance.debit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
              <article className="metric-card"><div className="metric-top">إجمالي الدائن</div><strong className="metric-value">{Number(trialBalance.credit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
              <article className="metric-card"><div className="metric-top">الفرق</div><strong className="metric-value">{Number(trialBalance.difference).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
            </div><div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>اسم الحساب</th><th>مدين</th><th>دائن</th><th>الرصيد (مدين - دائن)</th></tr></thead>
              <tbody>{trialBalance.accounts.map((row) => <tr key={row.account_id}><td className="code-cell">{row.code}</td><td>{row.name_ar}</td><td>{Number(row.debit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td><td>{Number(row.credit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td><td>{Number(row.net_balance).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td></tr>)}</tbody></table></div></>}
            {!trialBalance && <div className="table-empty">{loading ? "جارٍ تحميل ميزان المراجعة…" : "لا توجد بيانات ميزان مراجعة."}</div>}
            <Suspense fallback={panelFallback}><ExpenseAnalysisPanel companyId={companyId} branches={branches} /></Suspense>
            <FinancialStatementsPanel companyId={companyId} /><LedgerAndEquityPanel companyId={companyId} /><CashFlowReportPanel companyId={companyId} /><IndirectCashFlowReportPanel companyId={companyId} /><Suspense fallback={panelFallback}><PartyStatementsPanel companyId={companyId} currency={selectedCompany?.base_currency_code || ""} /></Suspense><Suspense fallback={panelFallback}><OperationalReconciliationPanel companyId={companyId} currency={selectedCompany?.base_currency_code || ""} branches={branches} /></Suspense></section>}
          {active === "tax" && <><TaxCodeSettingsPanel companyId={companyId} country={selectedCompany?.country} /><section className="work-panel"><VATReturnPanel companyId={companyId} /></section></>}
          {active === "backup" && <Suspense fallback={panelFallback}><BackupPanel /></Suspense>}
          {active === "customers" && <Suspense fallback={panelFallback}><CustomersPanel companyId={companyId} /></Suspense>}
          {active === "sales-workflow" && <Suspense fallback={panelFallback}><SalesPanel companyId={companyId} /></Suspense>}
          {active === "suppliers" && <Suspense fallback={panelFallback}><SuppliersPanel companyId={companyId} /></Suspense>}
          {active === "purchasing" && <Suspense fallback={panelFallback}><PurchasingPanel companyId={companyId} /></Suspense>}
          {active === "inventory" && <Suspense fallback={panelFallback}><InventoryPanel companyId={companyId} /></Suspense>}
          {active === "treasury" && companyId !== null && <Suspense fallback={panelFallback}><TreasuryPanel companyId={companyId} /></Suspense>}
          {active === "fixed-assets" && <Suspense fallback={panelFallback}><FixedAssetsPanel companyId={companyId} /></Suspense>}
          {active === "hr" && <Suspense fallback={panelFallback}><HrPayrollPanel companyId={companyId} baseCurrencyId={selectedCompany?.base_currency} country={selectedCompany?.country} /></Suspense>}
          {active === "audit" && <Suspense fallback={panelFallback}><SystemMonitorPanel companyId={companyId} /></Suspense>}
          {active === "approvals" && <Suspense fallback={panelFallback}><DiscountApprovalsPanel companyId={companyId} /></Suspense>}
          {active === "access" && <Suspense fallback={panelFallback}><CompanyAccessPanel companyId={companyId} /></Suspense>}
          {active === "setup" && <Suspense fallback={panelFallback}><CompanySetupPanel companies={companies} companyId={companyId} onCompanyCreated={(company) => { setCompanies((rows) => [...rows, company]); setCompanyId(company.id); setActive("setup"); }} onCompanyUpdated={(company) => setCompanies((rows) => rows.map((row) => row.id === company.id ? company : row))} onStructureChanged={async () => { if (!companyId) return; try { const [branchRows, yearRows] = await Promise.all([api.branches(companyId), api.fiscalYears(companyId)]); setBranches(branchRows.filter((row) => row.company === companyId)); setFiscalYears(yearRows.filter((row) => row.company === companyId)); setDraft((old) => ({ ...old, branch: String(branchRows.find((row) => row.is_active)?.id || ""), fiscal_year: String(yearRows.find((row) => row.status === "open")?.id || "") })); } catch (reason) { setJournalError(reason instanceof Error ? reason.message : "تعذر تحديث إعدادات الفرع والسنة المالية."); } }} /></Suspense>}
          {journalModal && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setJournalModal(false); }}>
            <section className="journal-modal" role="dialog" aria-modal="true" aria-labelledby="journal-title">
              <div className="modal-heading"><div><h2 id="journal-title">{reviewingJournal ? "مراجعة القيد قبل الحفظ" : "إنشاء قيد يومية"}</h2><p>{reviewingJournal ? "راجع بيانات القيد وإجمالياته، ثم احفظه كمسودة دون ترحيل." : "أدخل البيانات والتفاصيل ثم انتقل إلى المراجعة قبل الحفظ."}</p></div><button type="button" className="icon-button" onClick={() => setJournalModal(false)} aria-label="إغلاق">×</button></div>
              {journalError && <div className="error-banner">{journalError}</div>}
              {!reviewingJournal ? <form className="journal-form" onSubmit={(event) => { event.preventDefault(); if (validateDraft()) setReviewingJournal(true); }}>
                <label>رقم القيد<input required value={draft.entry_number} onChange={(e) => setDraft({ ...draft, entry_number: e.target.value })} /></label>
                <label>التاريخ<input required type="date" value={draft.entry_date} onChange={(e) => setDraft({ ...draft, entry_date: e.target.value })} /></label>
                <label>الفرع<select required value={draft.branch} onChange={(e) => setDraft({ ...draft, branch: e.target.value })}><option value="">اختر الفرع</option>{branches.map((b) => <option key={b.id} value={b.id}>{b.name_ar} ({b.code})</option>)}</select></label>
                <label>السنة المالية<select required value={draft.fiscal_year} onChange={(e) => setDraft({ ...draft, fiscal_year: e.target.value })}><option value="">اختر السنة</option>{fiscalYears.filter((y) => y.status === "open").map((y) => <option key={y.id} value={y.id}>{y.name} · مفتوحة</option>)}</select></label>
                <label className="form-wide">البيان<input required value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
                <div className="journal-lines-head form-wide"><div><strong>تفاصيل القيد</strong><span> أضف أي عدد من السطور حسب طبيعة العملية</span></div><button type="button" className="add-line-button" onClick={() => setJournalLines((rows) => [...rows, { account: "", debit: "", credit: "", description: "" }])}>＋ إضافة سطر</button></div>
                <div className="journal-lines-wrap form-wide"><table className="journal-lines-table"><thead><tr><th>#</th><th>الحساب</th><th>البيان</th><th>مدين</th><th>دائن</th><th></th></tr></thead><tbody>
                  {journalLines.map((line, index) => <tr key={index}><td>{index + 1}</td>
                    <td><select aria-label={"حساب السطر " + (index + 1)} value={line.account} onChange={(e) => setJournalLines((rows) => rows.map((row, i) => i === index ? { ...row, account: e.target.value } : row))}><option value="">اختر الحساب</option>{accounts.filter((a) => a.is_active && a.allow_posting && !a.is_group).map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></td>
                    <td><input aria-label={"بيان السطر " + (index + 1)} value={line.description} onChange={(e) => setJournalLines((rows) => rows.map((row, i) => i === index ? { ...row, description: e.target.value } : row))} placeholder="اختياري" /></td>
                    <td><input aria-label={"مدين السطر " + (index + 1)} type="number" min="0" step="0.0001" value={line.debit} onChange={(e) => setJournalLines((rows) => rows.map((row, i) => i === index ? { ...row, debit: e.target.value, credit: e.target.value ? "0" : row.credit } : row))} placeholder="0.00" /></td>
                    <td><input aria-label={"دائن السطر " + (index + 1)} type="number" min="0" step="0.0001" value={line.credit} onChange={(e) => setJournalLines((rows) => rows.map((row, i) => i === index ? { ...row, credit: e.target.value, debit: e.target.value ? "0" : row.debit } : row))} placeholder="0.00" /></td>
                    <td><button type="button" className="remove-line-button" aria-label="حذف السطر" disabled={journalLines.length <= 2} onClick={() => setJournalLines((rows) => rows.filter((_, i) => i !== index))}>×</button></td></tr>)}
                </tbody></table></div>
                <div className="balance-preview form-wide"><div><span>إجمالي المدين</span><strong>{debitTotal.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>إجمالي الدائن</span><strong>{creditTotal.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div className={Math.abs(journalDifference) <= 0.00005 ? "balance-ok" : "balance-warn"}><span>الفرق</span><strong>{journalDifference.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong><small>{Math.abs(journalDifference) <= 0.00005 ? "القيد متوازن" : "القيد غير متوازن"}</small></div><small>{selectedCompany?.base_currency_code || ""}</small></div>
                <div className="form-actions form-wide"><button type="button" className="subtle-button" onClick={() => setJournalModal(false)}>إلغاء</button><button type="submit" className="primary-button">مراجعة القيد ←</button></div>
              </form> : <div className="journal-review"><div className="review-meta"><div><span>الشركة</span><strong>{selectedCompany?.name_ar || "—"}</strong></div><div><span>الفرع</span><strong>{branches.find((item) => item.id === Number(draft.branch))?.name_ar || "—"}</strong></div><div><span>السنة المالية</span><strong>{fiscalYears.find((item) => item.id === Number(draft.fiscal_year))?.name || "—"}</strong></div><div><span>رقم القيد</span><strong>{draft.entry_number || "—"}</strong></div><div><span>التاريخ</span><strong>{draft.entry_date}</strong></div><div className="review-description"><span>البيان</span><strong>{draft.description || "—"}</strong></div></div><div className="journal-lines-wrap"><table className="journal-lines-table"><thead><tr><th>#</th><th>الحساب</th><th>البيان</th><th>مدين</th><th>دائن</th></tr></thead><tbody>{journalLines.map((line, index) => { const account = accounts.find((item) => item.id === Number(line.account)); return <tr key={index}><td>{index + 1}</td><td>{account ? `${account.code} · ${account.name_ar}` : "—"}</td><td>{line.description || draft.description || "—"}</td><td>{(Number(line.debit) || 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{(Number(line.credit) || 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>; })}</tbody></table></div><div className="review-totals"><div><span>إجمالي المدين</span><strong>{debitTotal.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>إجمالي الدائن</span><strong>{creditTotal.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>الفرق</span><strong>{journalDifference.toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong><small> {selectedCompany?.base_currency_code || ""} · متوازن</small></div></div><div className="form-actions"><button type="button" className="subtle-button" disabled={saving} onClick={() => setReviewingJournal(false)}>← تعديل البيانات</button><button type="button" className="primary-button" disabled={saving} onClick={() => void saveDraft()}>{saving ? "جارٍ الحفظ…" : "تأكيد وحفظ كمسودة"}</button></div></div>}
            </section>
          </div>}
          {selectedJournal && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedJournal(null); }}>
<section className="journal-modal journal-detail-modal" role="dialog" aria-modal="true" aria-labelledby="saved-journal-title">
{journalError && <div className="error-banner">{journalError}</div>}<div className="modal-heading"><div><h2 id="saved-journal-title">مراجعة القيد {selectedJournal.entry_number}</h2><p>راجع تفاصيل المسودة قبل اتخاذ قرار الترحيل.</p></div><button type="button" className="icon-button" onClick={() => setSelectedJournal(null)} aria-label="إغلاق">×</button></div>
<div className="review-meta"><div><span>الشركة</span><strong>{selectedCompany?.name_ar || "—"}</strong></div><div><span>الفرع</span><strong>{selectedJournal.branch_code || "—"}</strong></div><div><span>السنة المالية</span><strong>{selectedJournal.fiscal_year_name || "—"}</strong></div><div><span>التاريخ</span><strong>{selectedJournal.entry_date}</strong></div><div className="review-description"><span>البيان</span><strong>{selectedJournal.description || "—"}</strong></div></div>
<div className="journal-lines-wrap"><table className="journal-lines-table"><thead><tr><th>#</th><th>الحساب</th><th>البيان</th><th>مدين</th><th>دائن</th></tr></thead><tbody>{selectedJournal.lines.map((line, index) => <tr key={line.id}><td>{index + 1}</td><td>{line.account_code} · {line.account_name}</td><td>{line.description || "—"}</td><td>{Number(line.debit).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td><td>{Number(line.credit).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</td></tr>)}</tbody></table></div>
<div className="review-totals"><div><span>إجمالي المدين</span><strong>{selectedJournal.lines.reduce((sum, line) => sum + Number(line.debit), 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>إجمالي الدائن</span><strong>{selectedJournal.lines.reduce((sum, line) => sum + Number(line.credit), 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong></div><div><span>الفرق</span><strong>{(selectedJournal.lines.reduce((sum, line) => sum + Number(line.debit), 0) - selectedJournal.lines.reduce((sum, line) => sum + Number(line.credit), 0)).toLocaleString("ar-EG", { maximumFractionDigits: 4 })}</strong><small>{selectedCompany?.base_currency_code || ""}</small></div></div>
<div className="form-actions"><button type="button" className="subtle-button" onClick={() => setSelectedJournal(null)}>إغلاق</button><button type="button" className="primary-button" disabled={postingId === selectedJournal.id} onClick={() => postDraft(selectedJournal)}>{postingId === selectedJournal.id ? "جارٍ الترحيل…" : "اعتماد وترحيل القيد"}</button></div>
</section></div>}
          <footer className="page-footer"><span>© 2026 Admrail ERP v2</span><span><i /> واجهة التطوير المحلية</span><span>الإصدار التأسيسي 0.1.0</span></footer>
        </div>
      </main>
    </div>
  );
}
