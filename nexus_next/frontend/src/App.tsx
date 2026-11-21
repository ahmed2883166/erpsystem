import { useEffect, useMemo, useState } from "react";
import { api, type Account, type Branch, type Company, type FiscalYear, type Journal, type TrialBalance } from "./api";

type Section = "overview" | "journals" | "accounts" | "reports" | "setup";
const navigation: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "نظرة عامة", icon: "◫", group: "الرئيسية" },
  { id: "journals", label: "قيود اليومية", icon: "⇄", group: "المحاسبة" },
  { id: "accounts", label: "دليل الحسابات", icon: "☷", group: "المحاسبة" },
  { id: "reports", label: "التقارير المالية", icon: "▤", group: "التقارير" },
  { id: "setup", label: "إعدادات الشركة", icon: "⚙", group: "الإدارة" },
];
const modules = [
  { title: "المحاسبة العامة", detail: "القيود، دليل الحسابات، مراكز التكلفة", icon: "◈", tone: "blue", section: "accounts" as Section },
  { title: "المبيعات والعملاء", detail: "دورة البيع والتحصيل والأرصدة", icon: "↗", tone: "violet", section: "setup" as Section },
  { title: "المشتريات والموردون", detail: "الفواتير والاستحقاقات والمدفوعات", icon: "↙", tone: "cyan", section: "setup" as Section },
  { title: "المخزون والمستودعات", detail: "الأصناف والحركات والتقييم", icon: "▦", tone: "amber", section: "setup" as Section },
  { title: "المرتبات والموارد البشرية", detail: "الموظفون والاستحقاقات والحضور", icon: "♙", tone: "green", section: "setup" as Section },
  { title: "الأصول الثابتة", detail: "الإهلاك والتصرفات والتسويات", icon: "▣", tone: "rose", section: "setup" as Section },
];
const natureNames: Record<string, string> = { asset: "أصول", liability: "التزامات", equity: "حقوق ملكية", revenue: "إيرادات", expense: "مصروفات" };
const statusNames: Record<string, string> = { draft: "مسودة", posted: "مرحّل", reversed: "معكوس" };
const sourceNames: Record<string, string> = { manual: "يدوي", sales: "مبيعات", purchases: "مشتريات", payroll: "مرتبات", inventory: "مخزون", assets: "أصول", opening: "افتتاحي", closing: "إقفال" };

export default function App() {
  const [active, setActive] = useState<Section>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [companyId, setCompanyId] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [journals, setJournals] = useState<Journal[]>([]);
  const [trialBalance, setTrialBalance] = useState<TrialBalance | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [journalModal, setJournalModal] = useState(false);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [fiscalYears, setFiscalYears] = useState<FiscalYear[]>([]);
  const [saving, setSaving] = useState(false);
  const [journalError, setJournalError] = useState("");
  const [draft, setDraft] = useState({ branch: "", fiscal_year: "", entry_number: "", entry_date: new Date().toISOString().slice(0, 10), description: "", debit: "", credit: "", amount: "" });
  const current = useMemo(() => navigation.find((item) => item.id === active), [active]);

  useEffect(() => {
    api.companies().then((items) => {
      setCompanies(items);
      if (items.length) setCompanyId(items[0].id);
    }).catch((reason: Error) => setError(reason.message));
  }, []);

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

  const saveDraft = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setJournalError("");
    if (!companyId || !draft.branch || !draft.fiscal_year) { setJournalError("حدد الشركة والفرع والسنة المالية."); return; }
    if (draft.debit === draft.credit || Number(draft.amount) <= 0) { setJournalError("اختر حسابين مختلفين وأدخل مبلغاً أكبر من صفر."); return; }
    setSaving(true);
    try {
      await api.createJournal({ company: companyId, branch: Number(draft.branch), fiscal_year: Number(draft.fiscal_year), entry_number: draft.entry_number, entry_date: draft.entry_date, description: draft.description, source_type: "manual", lines: [{ account: Number(draft.debit), debit: draft.amount, credit: "0", description: draft.description }, { account: Number(draft.credit), debit: "0", credit: draft.amount, description: draft.description }] });
      const rows = await api.journals(companyId); setJournals(rows); setJournalModal(false);
      setDraft((old) => ({ ...old, entry_number: "", description: "", debit: "", credit: "", amount: "" }));
    } catch (reason) { setJournalError(reason instanceof Error ? reason.message : "تعذر حفظ القيد."); }
    finally { setSaving(false); }
  };
  const filteredAccounts = useMemo(() => accounts.filter((account) =>
    `${account.code} ${account.name_ar}`.toLowerCase().includes(search.toLowerCase())), [accounts, search]);
  const filteredJournals = useMemo(() => journals.filter((entry) =>
    `${entry.entry_number} ${entry.description}`.toLowerCase().includes(search.toLowerCase())), [journals, search]);
  const selectedCompany = companies.find((company) => company.id === companyId);
  const today = new Intl.DateTimeFormat("ar-EG", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(new Date());

  useEffect(() => {
    if (active !== "reports" || !companyId) return;
    let cancelled = false;
    setLoading(true);
    api.trialBalance(companyId).then((result) => { if (!cancelled) setTrialBalance(result); })
      .catch((reason: Error) => { if (!cancelled) setError(reason.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [active, companyId]);

  return (
    <div className="app-shell" dir="rtl">
      {sidebarOpen && <button className="mobile-scrim" aria-label="إغلاق القائمة" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand-lockup"><div className="brand-symbol">N</div><div><strong>NEXUS</strong><span>ENTERPRISE PLATFORM</span></div>
          <button className="sidebar-close" onClick={() => setSidebarOpen(false)} aria-label="إغلاق القائمة">×</button></div>
        <div className="workspace"><div className="workspace-avatar">NX</div><div><strong>مساحة العمل</strong><small>{selectedCompany?.name_ar || "اختر الشركة"}</small></div>
          <select aria-label="الشركة" value={companyId ?? ""} onChange={(event) => setCompanyId(Number(event.target.value) || null)}>
            <option value="">—</option>{companies.map((company) => <option key={company.id} value={company.id}>{company.name_ar}</option>)}
          </select></div>
        <nav className="navigation">
          {["الرئيسية", "المحاسبة", "التقارير", "الإدارة"].map((group) => <div className="nav-group" key={group}><p>{group}</p>
            {navigation.filter((item) => item.group === group).map((item) => <button key={item.id} className={`nav-item ${active === item.id ? "nav-active" : ""}`}
              onClick={() => { setActive(item.id); setSearch(""); setSidebarOpen(false); }}><span className="nav-icon">{item.icon}</span><span>{item.label}</span>{active === item.id && <i />}</button>)}
          </div>)}
        </nav>
        <div className="sidebar-bottom"><a className="admin-link" href="/admin/" target="_blank" rel="noreferrer">فتح لوحة إدارة النظام ↗</a>
          <div className="user-profile"><div className="user-avatar">N</div><div><strong>NEXUS ERP</strong><small>منصة إدارة الأعمال</small></div><span>•••</span></div></div>
      </aside>
      <main className="main-area">
        <header className="topbar"><div className="topbar-start"><button className="menu-toggle" onClick={() => setSidebarOpen(true)} aria-label="فتح القائمة">☰</button>
          <div className="breadcrumb"><span>NEXUS</span><b>/</b>{current?.label}</div></div>
          <div className="topbar-actions"><div className="period-select"><span className="period-dot" /> {selectedCompany?.name_ar || "لا توجد شركة"} <span>⌄</span></div>
            <button className="icon-button" aria-label="البحث" onClick={() => document.getElementById("global-search")?.focus()}>⌕</button>
            <a className="admin-shortcut" href="/admin/" target="_blank" rel="noreferrer">الإدارة</a></div></header>
        <div className="page-content">
          <section className="welcome-row"><div><p className="eyebrow">{today} · NEXUS ERP</p><h1>{active === "overview" ? "أهلاً بك في NEXUS" : current?.label}</h1>
            <p className="welcome-copy">{active === "overview" ? "نظرة موحدة على أعمال شركتك وبياناتها." : "بيانات مباشرة من خدمات النظام المحاسبية."}</p></div>
            <button className="primary-button" onClick={() => { setActive("journals"); setJournalModal(true); setJournalError(""); }}><span>＋</span> قيد جديد</button></section>

          {error && <div className="error-banner"><strong>تعذر تحميل البيانات</strong><span>{error}</span><a href="/admin/" target="_blank" rel="noreferrer">تسجيل الدخول من لوحة الإدارة</a></div>}
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
            <div className="section-heading module-heading"><div><h2>وحدات النظام</h2><p>البنية الأساسية للوحدات الرئيسية</p></div><span className="module-count">6 وحدات أساسية</span></div>
            <div className="modules-grid">{modules.map((module) => <button className="module-card" key={module.title} onClick={() => setActive(module.section)}>
              <i className={`module-icon ${module.tone}`}>{module.icon}</i><div><strong>{module.title}</strong><small>{module.detail}</small></div><span className="module-arrow">←</span></button>)}</div>
            <section className="activity-panel"><div className="activity-title"><div><h2>أحدث قيود اليومية</h2><p>آخر خمس حركات مسجلة</p></div><button className="subtle-button" onClick={() => setActive("journals")}>عرض الكل ←</button></div>
              {journals.slice(0, 5).length ? <div className="data-table-wrap"><table><thead><tr><th>رقم القيد</th><th>التاريخ</th><th>البيان</th><th>الحالة</th></tr></thead><tbody>{journals.slice(0, 5).map((entry) => <tr key={entry.id}><td>{entry.entry_number}</td><td>{entry.entry_date}</td><td>{entry.description}</td><td><span className={`status-pill ${entry.status}`}>{statusNames[entry.status] || entry.status}</span></td></tr>)}</tbody></table></div>
                : <div className="empty-state"><div className="empty-graphic">◷</div><strong>{loading ? "جارٍ تحميل القيود" : "لا توجد قيود حتى الآن"}</strong><p>ستظهر القيود هنا بمجرد تسجيلها في الشركة المختارة.</p></div>}</section>
          </>}

          {active === "accounts" && <section className="work-panel"><div className="work-panel-head"><div><h2>دليل الحسابات</h2><p>شجرة الحسابات الخاصة بـ {selectedCompany?.name_ar || "الشركة"}</p></div><span className="coming-soon">{accounts.length} حساب</span></div>
            <div className="table-toolbar"><input id="global-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث بكود أو اسم الحساب…" /><span className="module-count">{filteredAccounts.length} نتيجة</span></div>
            <div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>اسم الحساب</th><th>الحساب الأب</th><th>الطبيعة</th><th>نوع الحساب</th><th>الحالة</th></tr></thead>
              <tbody>{filteredAccounts.map((account) => <tr key={account.id}><td className="code-cell">{account.code}</td><td>{account.name_ar}</td><td>{account.parent_code || "—"}</td><td>{natureNames[account.nature] || account.nature}</td>
                <td>{account.is_group ? "تجميعي" : "تفصيلي"}</td><td><span className={`status-pill ${account.is_active ? "posted" : "reversed"}`}>{account.is_active ? "نشط" : "موقوف"}</span></td></tr>)}</tbody></table>
              {!loading && !filteredAccounts.length && <div className="table-empty">لا توجد حسابات مطابقة للبحث.</div>}</div></section>}

          {active === "journals" && <section className="work-panel"><div className="work-panel-head"><div><h2>قيود اليومية</h2><p>سجل القيود وحالات الترحيل للشركة المحددة</p></div><button className="primary-button" onClick={() => { setJournalModal(true); setJournalError(""); }}>＋ قيد جديد</button></div>
            <div className="table-toolbar"><input id="global-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ابحث برقم القيد أو البيان…" /><span className="module-count">{filteredJournals.length} نتيجة</span></div>
            <div className="data-table-wrap"><table><thead><tr><th>رقم القيد</th><th>التاريخ</th><th>البيان</th><th>الفرع</th><th>المصدر</th><th>الحالة</th></tr></thead>
              <tbody>{filteredJournals.map((entry) => <tr key={entry.id}><td className="code-cell">{entry.entry_number}</td><td>{entry.entry_date}</td><td>{entry.description}</td><td>{entry.branch_code}</td><td>{sourceNames[entry.source_type] || entry.source_type}</td><td><span className={`status-pill ${entry.status}`}>{statusNames[entry.status] || entry.status}</span></td></tr>)}</tbody></table>
              {!loading && !filteredJournals.length && <div className="table-empty">لا توجد قيود مطابقة للبحث.</div>}</div></section>}

          {active === "reports" && <section className="work-panel"><div className="work-panel-head"><div><h2>ميزان المراجعة</h2><p>أرصدة الحسابات من القيود المرحلة فقط — للشركة المحددة</p></div><span className="coming-soon">{trialBalance?.accounts.length ?? 0} حساب</span></div>
            {trialBalance && <><div className="metrics-grid report-metrics">
              <article className="metric-card"><div className="metric-top">إجمالي المدين</div><strong className="metric-value">{Number(trialBalance.debit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
              <article className="metric-card"><div className="metric-top">إجمالي الدائن</div><strong className="metric-value">{Number(trialBalance.credit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
              <article className="metric-card"><div className="metric-top">الفرق</div><strong className="metric-value">{Number(trialBalance.difference).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</strong></article>
            </div><div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>اسم الحساب</th><th>مدين</th><th>دائن</th><th>الرصيد (مدين - دائن)</th></tr></thead>
              <tbody>{trialBalance.accounts.map((row) => <tr key={row.account_id}><td className="code-cell">{row.code}</td><td>{row.name_ar}</td><td>{Number(row.debit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td><td>{Number(row.credit_total).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td><td>{Number(row.net_balance).toLocaleString("ar-EG", { maximumFractionDigits: 2 })}</td></tr>)}</tbody></table></div></>}
            {!trialBalance && <div className="table-empty">{loading ? "جارٍ تحميل ميزان المراجعة…" : "لا توجد بيانات ميزان مراجعة."}</div>}</section>}
          {active === "setup" && <section className="work-panel"><div className="work-panel-head"><div><h2>إعدادات الشركة</h2><p>بيانات الشركة الأساسية والفروع والسنوات المالية.</p></div><span className="coming-soon">إعدادات أساسية</span></div>
            <div className="data-table-wrap"><table><thead><tr><th>كود الشركة</th><th>اسم الشركة</th><th>العملة الأساسية</th></tr></thead><tbody>{companies.map((company) => <tr key={company.id}><td className="code-cell">{company.code}</td><td>{company.name_ar}</td><td>{company.base_currency_code}</td></tr>)}</tbody></table></div></section>}
          {journalModal && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setJournalModal(false); }}>
            <section className="journal-modal" role="dialog" aria-modal="true" aria-labelledby="journal-title">
              <div className="modal-heading"><div><h2 id="journal-title">إنشاء قيد يومية</h2><p>أدخل بيانات القيد واحفظه كمسودة للمراجعة.</p></div><button type="button" className="icon-button" onClick={() => setJournalModal(false)} aria-label="إغلاق">×</button></div>
              {journalError && <div className="error-banner">{journalError}</div>}
              <form className="journal-form" onSubmit={saveDraft}>
                <label>رقم القيد<input required value={draft.entry_number} onChange={(e) => setDraft({ ...draft, entry_number: e.target.value })} /></label>
                <label>التاريخ<input required type="date" value={draft.entry_date} onChange={(e) => setDraft({ ...draft, entry_date: e.target.value })} /></label>
                <label>الفرع<select required value={draft.branch} onChange={(e) => setDraft({ ...draft, branch: e.target.value })}><option value="">اختر الفرع</option>{branches.map((b) => <option key={b.id} value={b.id}>{b.name_ar} ({b.code})</option>)}</select></label>
                <label>السنة المالية<select required value={draft.fiscal_year} onChange={(e) => setDraft({ ...draft, fiscal_year: e.target.value })}><option value="">اختر السنة</option>{fiscalYears.map((y) => <option key={y.id} value={y.id}>{y.name} · {y.status === "open" ? "مفتوحة" : "مغلقة"}</option>)}</select></label>
                <label className="form-wide">البيان<input required value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
                <div className="journal-lines-head form-wide"><strong>أطراف القيد</strong><span>مدين ودائن متساويان</span></div>
                <label>الحساب المدين<select required value={draft.debit} onChange={(e) => setDraft({ ...draft, debit: e.target.value })}><option value="">اختر الحساب</option>{accounts.filter((a) => a.is_active && a.allow_posting && !a.is_group).map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
                <label>الحساب الدائن<select required value={draft.credit} onChange={(e) => setDraft({ ...draft, credit: e.target.value })}><option value="">اختر الحساب</option>{accounts.filter((a) => a.is_active && a.allow_posting && !a.is_group).map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
                <label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={draft.amount} onChange={(e) => setDraft({ ...draft, amount: e.target.value })} /></label>
                <div className="balance-preview"><span>إجمالي المدين = إجمالي الدائن</span><strong>{Number(draft.amount || 0).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} {selectedCompany?.base_currency_code || ""}</strong></div>
                <div className="form-actions form-wide"><button type="button" className="subtle-button" onClick={() => setJournalModal(false)}>إلغاء</button><button type="submit" className="primary-button" disabled={saving}>{saving ? "جارٍ الحفظ…" : "حفظ كمسودة"}</button></div>
              </form>
            </section>
          </div>}
          <footer className="page-footer"><span>© 2026 NEXUS ERP</span><span><i /> واجهة التطوير المحلية</span><span>الإصدار التأسيسي 0.1.0</span></footer>
        </div>
      </main>
    </div>
  );
}
