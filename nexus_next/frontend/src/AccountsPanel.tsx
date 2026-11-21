import { useEffect, useMemo, useState, type ReactNode } from "react";
import { accountRoleApi, api, type Account, type AccountRole, type AccountTemplateInfo, type AccountImportPreview, type AccountImportResult } from "./api";

type Props = {
  companyId: number | null;
  companyName: string;
  activity?: string;
  accounts: Account[];
  onRefresh: () => Promise<void>;
};

const natureNames: Record<string, string> = {
  asset: "الأصول",
  liability: "الالتزامات",
  equity: "حقوق الملكية",
  revenue: "الإيرادات",
  expense: "التكاليف والمصروفات",
};

const roleNames: Record<string, string> = {
  cash: "النقدية",
  bank: "البنوك",
  accounts_receivable: "العملاء",
  accounts_payable: "الموردون",
  inventory: "المخزون",
  sales: "إيرادات النشاط",
  sales_returns: "مردودات المبيعات",
  cogs: "تكلفة النشاط",
  operating_expense: "المصروفات التشغيلية",
  vat_input: "ضريبة المدخلات",
  vat_output: "ضريبة المخرجات",
  vat_payable: "ضريبة مستحقة",
  employee_insurance_payable: "تأمينات الموظفين",
  employer_insurance_payable: "تأمينات الشركة",
  employer_insurance_expense: "مصروف تأمينات الشركة",
  retained_earnings: "الأرباح المرحلة",
  payroll_expense: "مصروف المرتبات",
  payroll_payable: "مرتبات مستحقة",
  goods_received_not_invoiced: "استلامات غير مفوترة",
  inventory_adjustment_gain: "أرباح تسوية المخزون",
  inventory_adjustment_loss: "خسائر تسوية المخزون",
  inter_branch_clearing: "تسويات الفروع",
};

const natureFilters = [
  { value: "all", label: "كل الحسابات" },
  { value: "asset", label: "الأصول" },
  { value: "liability", label: "الالتزامات" },
  { value: "equity", label: "حقوق الملكية" },
  { value: "revenue", label: "الإيرادات" },
  { value: "expense", label: "التكاليف والمصروفات" },
];

export default function AccountsPanel({ companyId, companyName, activity, accounts, onRefresh }: Props) {
  const [roles, setRoles] = useState<AccountRole[]>([]);
  const [template, setTemplate] = useState<AccountTemplateInfo | null>(null);
  const [search, setSearch] = useState("");
  const [nature, setNature] = useState("all");
  const [showInactive, setShowInactive] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPreview, setImportPreview] = useState<AccountImportPreview | null>(null);
  const [importBusy, setImportBusy] = useState(false);

  const loadMeta = async () => {
    if (!companyId) { setRoles([]); setTemplate(null); return; }
    const [roleRows, templateInfo] = await Promise.all([
      accountRoleApi.list(companyId),
      api.accountTemplateInfo(companyId),
    ]);
    setRoles(roleRows);
    setTemplate(templateInfo);
  };

  useEffect(() => {
    let alive = true;
    if (!companyId) { setRoles([]); setTemplate(null); setExpanded(new Set()); return; }
    setError("");
    Promise.all([accountRoleApi.list(companyId), api.accountTemplateInfo(companyId)])
      .then(([roleRows, templateInfo]) => {
        if (alive) { setRoles(roleRows); setTemplate(templateInfo); }
      })
      .catch(reason => { if (alive) setError(reason instanceof Error ? reason.message : "تعذر تحميل إعدادات دليل الحسابات."); });
    return () => { alive = false; };
  }, [companyId]);

  useEffect(() => {
    setExpanded(new Set(accounts.filter(item => item.is_group).map(item => item.id)));
  }, [accounts]);

  const rolesByAccount = useMemo(() => {
    const result = new Map<number, AccountRole[]>();
    roles.forEach(role => result.set(role.account, [...(result.get(role.account) || []), role]));
    return result;
  }, [roles]);

  const visibleIds = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    const visible = new Set<number>();
    const byId = new Map(accounts.map(item => [item.id, item]));
    accounts.forEach(item => {
      const isVisible = (showInactive || item.is_active) &&
        (nature === "all" || item.nature === nature) &&
        (!query || item.code.toLocaleLowerCase().includes(query) || item.name_ar.toLocaleLowerCase().includes(query));
      if (!isVisible) return;
      visible.add(item.id);
      let parent = item.parent ? byId.get(item.parent) : undefined;
      while (parent) {
        visible.add(parent.id);
        parent = parent.parent ? byId.get(parent.parent) : undefined;
      }
    });
    return visible;
  }, [accounts, nature, search, showInactive]);

  const childrenByParent = useMemo(() => {
    const result = new Map<number | null, Account[]>();
    accounts.forEach(item => result.set(item.parent, [...(result.get(item.parent) || []), item]));
    result.forEach(items => items.sort((a, b) => a.code.localeCompare(b.code, "en", { numeric: true })));
    return result;
  }, [accounts]);

  const roots = (childrenByParent.get(null) || []).filter(item => visibleIds.has(item.id));
  const groupCount = accounts.filter(item => item.is_group).length;
  const detailCount = accounts.filter(item => !item.is_group).length;
  const activeCount = accounts.filter(item => item.is_active).length;

  const toggle = (id: number) => setExpanded(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const applyTemplate = async () => {
    if (!companyId || busy) return;
    const confirmed = window.confirm(
      "سيتم إضافة الحسابات والأدوار المحاسبية الناقصة وفق نشاط الشركة. لن يتم تعديل أو حذف أي حساب أو رصيد أو ربط قائم. هل تريد المتابعة؟"
    );
    if (!confirmed) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.applyAccountTemplate(companyId);
      await onRefresh();
      await loadMeta();
      setNotice(
        `تم إنشاء ${result.created_accounts.length} حسابًا جديدًا، وإعادة استخدام ${result.reused_accounts.length} حساب. تمت إضافة ${result.role_assignments_created.length} ربط محاسبي.` +
        (result.role_conflicts.length ? ` مع الحفاظ على ${result.role_conflicts.length} ربط موجود دون تغييره.` : "")
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تطبيق قالب دليل الحسابات.");
    } finally { setBusy(false); }
  };


  const previewWorkbook = async () => {
    if (!companyId || !importFile || importBusy) return;
    setImportBusy(true); setError(""); setNotice(""); setImportPreview(null);
    try {
      const result = await api.importAccountWorkbook(companyId, importFile, "preview") as AccountImportPreview;
      setImportPreview(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذرت معاينة ملف Excel.");
    } finally { setImportBusy(false); }
  };

  const commitWorkbook = async () => {
    if (!companyId || !importFile || !importPreview || importPreview.errors.length || importBusy) return;
    if (!window.confirm("سيتم إضافة الحسابات الجديدة فقط. الحسابات المطابقة سيتم تخطيها، ولن يتم تعديل أو حذف أي حساب موجود. هل تعتمد الاستيراد؟")) return;
    setImportBusy(true); setError(""); setNotice("");
    try {
      const result = await api.importAccountWorkbook(companyId, importFile, "import") as AccountImportResult;
      await onRefresh();
      setImportPreview(null); setImportFile(null);
      setNotice(`تم استيراد ${result.created_accounts.length} حسابًا جديدًا وتخطي ${result.skipped_existing} حساب مطابق. لم يتم تعديل الحسابات الموجودة.`);
    } catch (reason) {
      const details = (reason as Error & {data?: {errors?: string[]}}).data?.errors || [];
      setError([reason instanceof Error ? reason.message : "تعذر اعتماد الاستيراد.", ...details].join(" · "));
    } finally { setImportBusy(false); }
  };

  const renderNode = (item: Account, depth = 0): ReactNode => {
    if (!visibleIds.has(item.id)) return null;
    const children = (childrenByParent.get(item.id) || []).filter(child => visibleIds.has(child.id));
    const hasChildren = children.length > 0;
    const isExpanded = expanded.has(item.id);
    const mappedRoles = rolesByAccount.get(item.id) || [];
    return <li className={`account-tree-node ${item.is_group ? "is-group" : "is-detail"} ${item.is_active ? "" : "is-inactive"}`} key={item.id}>
      <div className="account-tree-row" style={{ paddingInlineStart: `${depth * 18 + 10}px` }}>
        <button type="button" className={`account-tree-toggle ${item.is_group ? "group-toggle" : "leaf-toggle"}`}
          onClick={() => hasChildren && toggle(item.id)} disabled={!hasChildren} aria-label={hasChildren ? (isExpanded ? "طي المجموعة" : "توسيع المجموعة") : "حساب تفصيلي"}>
          {hasChildren ? (isExpanded ? "⌄" : "‹") : "·"}
        </button>
        <span className={`account-tree-kind ${item.is_group ? "group-kind" : "detail-kind"}`}>{item.is_group ? "▦" : "◦"}</span>
        <span className="account-tree-code" dir="ltr">{item.code}</span>
        <span className="account-tree-name">{item.name_ar}</span>
        <span className="account-role-chips">{mappedRoles.map(role => <span className="account-role-chip" key={role.id}>{roleNames[role.role] || role.role}</span>)}</span>
        <span className={`account-tree-type ${item.is_group ? "group-type" : "detail-type"}`}>{item.is_group ? "مجموعة" : "تفصيلي"}</span>
        <span className={`account-tree-status ${item.is_active ? "active" : "inactive"}`}>{item.is_active ? "نشط" : "موقوف"}</span>
      </div>
      {hasChildren && isExpanded && <ul className="account-tree-children">{children.map(child => renderNode(child, depth + 1))}</ul>}
    </li>;
  };

  return <section className="work-panel accounts-premium-panel">
    <div className="accounts-hero">
      <div className="accounts-hero-copy">
        <span className="accounts-eyebrow">FINANCIAL STRUCTURE · NEXUS ERP</span>
        <h2>دليل الحسابات</h2>
        <p>شجرة مالية موحدة، مرنة حسب النشاط، وأرقام حسابات مستقلة عن منطق الترحيل المحاسبي.</p>
        <div className="accounts-company-line"><strong>{companyName || "الشركة"}</strong>{template?.activity_label && <span>{template.activity_label}</span>}</div>
      </div>
      <div className="accounts-hero-mark" aria-hidden="true"><span>∑</span><i>◇</i></div>
    </div>

    <div className="accounts-stat-grid">
      <article><span>إجمالي الحسابات</span><strong>{accounts.length.toLocaleString("ar-EG")}</strong><small>الحسابات المسجلة بالشركة</small></article>
      <article><span>الحسابات التجميعية</span><strong>{groupCount.toLocaleString("ar-EG")}</strong><small>مستويات تنظيم الشجرة</small></article>
      <article><span>الحسابات التفصيلية</span><strong>{detailCount.toLocaleString("ar-EG")}</strong><small>الحسابات القابلة للترحيل</small></article>
      <article><span>الحسابات النشطة</span><strong>{activeCount.toLocaleString("ar-EG")}</strong><small>دون الحسابات الموقوفة</small></article>
    </div>

    <div className="account-template-card">
      <div className="template-icon">✧</div>
      <div className="template-copy">
        <strong>قالب دليل الحسابات حسب النشاط</strong>
        <p>{template ? `القالب المتاح: ${template.activity_label} · ${template.template_account_count} حسابًا منظمًا.` : "يتم تحميل بيانات قالب النشاط…"}</p>
        <small>الأصول والالتزامات والضرائب والتأمينات لها نطاقات مستقلة. ويمكن تخصيص الأرقام دون تغيير أدوار الحسابات أو القيود.</small>
      </div>
      <button type="button" className="primary-button template-apply-button" onClick={() => void applyTemplate()} disabled={!companyId || busy || !template}>
        {busy ? "جارٍ التجهيز…" : accounts.length ? "استكمال الحسابات الناقصة" : "تجهيز دليل الحسابات"}
      </button>
    </div>

    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}

    <div className="account-import-card">
      <div className="account-import-heading"><div><strong>استيراد شجرة من Excel</strong><p>ارفع ملف .xlsx لمعاينته وفحص الأكواد والحسابات الأب قبل أي حفظ.</p></div><span>معاينة آمنة</span></div>
      <div className="account-import-controls">
        <a className="subtle-button" href="/api/accounting/account-import/" download>تحميل نموذج Excel</a>
        <input type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={event => { setImportFile(event.target.files?.[0] || null); setImportPreview(null); }} aria-label="اختيار ملف شجرة الحسابات Excel" />
        <button type="button" className="subtle-button" disabled={!importFile || importBusy} onClick={() => void previewWorkbook()}>{importBusy ? "جارٍ الفحص…" : "معاينة وفحص الملف"}</button>
        {importPreview && <button type="button" className="primary-button" disabled={importBusy || importPreview.errors.length > 0 || importPreview.summary.blocked > 0} onClick={() => void commitWorkbook()}>{importBusy ? "جارٍ الاعتماد…" : "اعتماد الاستيراد"}</button>}
      </div>
      <small>الأعمدة المطلوبة: كود الحساب، اسم الحساب، طبيعة الحساب، التصنيف (مجموعة/تفصيلي). كود الأب اختياري للحسابات الرئيسية.</small>
      {importPreview && <div className="account-import-preview">
        <div className="account-import-summary"><span>إجمالي الصفوف: <b>{importPreview.summary.total}</b></span><span>جديد: <b>{importPreview.summary.ready_to_create}</b></span><span>مطابق موجود: <b>{importPreview.summary.already_exists}</b></span><span>يحتاج مراجعة: <b>{importPreview.summary.blocked}</b></span><span>تحذيرات أسماء: <b>{importPreview.summary.warnings}</b></span></div>
        {importPreview.errors.length > 0 && <div className="error-banner">{importPreview.errors.slice(0, 12).map((item, index) => <div key={index}>{item}</div>)}{importPreview.errors.length > 12 && <div>… و{importPreview.errors.length - 12} ملاحظة أخرى</div>}</div>}
        <div className="data-table-wrap"><table><thead><tr><th>صف Excel</th><th>الكود</th><th>اسم الحساب</th><th>الأب</th><th>الحالة</th></tr></thead><tbody>{importPreview.rows.slice(0, 30).map(row => <tr key={row.excel_row}><td>{row.excel_row}</td><td dir="ltr">{row.code}</td><td>{row.name_ar}</td><td dir="ltr">{row.parent_code || "—"}</td><td>{row.errors.length ? row.errors.join(" ") : row.status}{row.warnings?.length ? <small className="import-row-warning"> · {row.warnings.join(" ")}</small> : null}</td></tr>)}</tbody></table></div>
        {importPreview.rows.length > 30 && <small>تم عرض أول 30 صفًا من أصل {importPreview.rows.length}.</small>}
        {importPreview.errors.length === 0 && <div className="success-banner">الفحص سليم. لم يتم حفظ أي بيانات بعد؛ اضغط اعتماد الاستيراد لإضافة الحسابات الجديدة.</div>}
      </div>}
    </div>

    <div className="account-tree-toolbar">
      <div className="account-tree-toolbar-title"><div><h3>الشجرة المحاسبية</h3><p>افتح المجموعات للوصول إلى الحسابات التفصيلية والأدوار المرتبطة بها.</p></div><span>{visibleIds.size.toLocaleString("ar-EG")} ظاهر</span></div>
      <div className="account-tree-controls">
        <label className="account-tree-search"><span>⌕</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="ابحث برقم الحساب أو اسمه…" /></label>
        <select value={nature} onChange={event => setNature(event.target.value)} aria-label="تصفية حسب طبيعة الحساب">
          {natureFilters.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <label className="account-tree-inactive"><input type="checkbox" checked={showInactive} onChange={event => setShowInactive(event.target.checked)} /> إظهار الموقوف</label>
        <button type="button" className="subtle-button" onClick={() => setExpanded(new Set(accounts.filter(item => item.is_group).map(item => item.id)))}>فتح الكل</button>
        <button type="button" className="subtle-button" onClick={() => setExpanded(new Set())}>طي الكل</button>
      </div>
    </div>

    <div className="account-tree-legend">
      {Object.entries(natureNames).map(([key, label]) => <span key={key}><i className={`nature-dot ${key}`}></i>{label}</span>)}
    </div>

    <div className="account-tree-shell">
      <div className="account-tree-columns"><span>الحساب</span><span>الاسم / الدور المحاسبي</span><span>التصنيف</span><span>الحالة</span></div>
      {roots.length ? <ul className="account-tree-root">{roots.map(item => renderNode(item))}</ul> :
        <div className="account-tree-empty"><span>⌕</span><strong>{accounts.length ? "لا توجد نتائج مطابقة" : "دليل الحسابات لم يُجهز بعد"}</strong><p>{accounts.length ? "غيّر كلمات البحث أو الفلتر المختار." : "استخدم قالب النشاط بالأعلى لإنشاء الهيكل المحاسبي الأولي."}</p></div>}
    </div>

    <div className="account-roles-footer">
      <div><strong>أدوار الحسابات المحاسبية</strong><p>الأنظمة والتقارير تعتمد على الدور، وليس على رقم الحساب.</p></div>
      <span>{roles.length.toLocaleString("ar-EG")} ربطًا</span>
    </div>
    {roles.length > 0 && <div className="account-role-grid">{roles.map(role => <div className="account-role-item" key={role.id}>
      <span>{roleNames[role.role] || role.role}</span><strong dir="ltr">{role.account_code}</strong>
    </div>)}</div>}
  </section>;
}
