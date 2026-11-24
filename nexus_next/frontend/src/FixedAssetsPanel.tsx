/*
 * الأصول الثابتة: تسجيل الأصول وحساب جداول الإهلاك وعرض سجل الحركة والتقارير.
 * تشمل إجراءات الاستبعاد أو البيع وربط الحسابات المحاسبية والخزينة.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { formatMoney } from "./formatters";
import { api, fixedAssetsApi, treasuryApi, type Account, type Branch, type FiscalYear, type FixedAsset, type AssetSchedule, type TreasuryAccount, type AssetRegisterReport, type AssetDepreciationReport, type AssetMovementReport, type CashFlowReport, type AssetAccountReconciliation, type AssetDepreciationRunResult } from "./api";

type Props = { companyId: number | null };
const today = new Date().toISOString().slice(0, 10);
const emptyForm = {
  branch: "", code: "", name_ar: "", description: "", acquisition_date: today, depreciation_start_date: today,
  original_cost: "", salvage_value: "0", useful_life_months: "60", asset_account: "",
  accumulated_depreciation_account: "", depreciation_expense_account: "", acquisition_counter_account: "",
  disposal_gain_loss_account: "", disposal_loss_account: "", fiscal_year: "",
};

// شاشة الأصول الثابتة لإضافة الأصول وإدارة الإهلاك والاستبعاد والتقارير.
export default function FixedAssetsPanel({ companyId }: Props) {
  const [assets, setAssets] = useState<FixedAsset[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [years, setYears] = useState<FiscalYear[]>([]);
  const [form, setForm] = useState(emptyForm);
  const [periodDate, setPeriodDate] = useState(today.slice(0, 7) + "-28");
  const [disposalDate, setDisposalDate] = useState(today);
  const [disposalProceeds, setDisposalProceeds] = useState("0");
  const [disposalCashAccount, setDisposalCashAccount] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [schedule, setSchedule] = useState<AssetSchedule | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [reportTab, setReportTab] = useState<"register"|"depreciation"|"movements"|"cashflow"|"reconciliation">("register");
  const [reportBranch, setReportBranch] = useState("");
  const [reportFrom, setReportFrom] = useState(new Date().getFullYear()+"-01-01");
  const [reportTo, setReportTo] = useState(today);
  const [reportAsOf, setReportAsOf] = useState(today);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState("");
  const [registerReport, setRegisterReport] = useState<AssetRegisterReport|null>(null);
  const [depreciationReport, setDepreciationReport] = useState<AssetDepreciationReport|null>(null);
  const [movementReport, setMovementReport] = useState<AssetMovementReport|null>(null);
  const [cashFlowReport, setCashFlowReport] = useState<CashFlowReport|null>(null);
  const [assetReconciliation, setAssetReconciliation] = useState<AssetAccountReconciliation|null>(null);
  const [runResult, setRunResult] = useState<AssetDepreciationRunResult|null>(null);
  const [notice, setNotice] = useState("");

  // تحميل الأصول والحسابات والفروع والسنوات المالية وحسابات الخزينة.
const refresh = async () => {
    if (!companyId) return;
    setLoading(true);
    try { setAssets(await fixedAssetsApi.list(companyId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل الأصول."); }
    finally { setLoading(false); }
  };
  useEffect(() => {
    if (!companyId) return;
    Promise.all([api.accounts(companyId), api.branches(companyId), api.fiscalYears(companyId), treasuryApi.accounts(companyId)]).then(([a,b,y,t]) => {
      setAccounts(a.filter(x => x.is_active && !x.is_group && x.allow_posting));
      setTreasuryAccounts(t.filter(x => x.is_active));
      setBranches(b.filter(x => x.is_active && x.company === companyId));
      setYears(y.filter(x => x.company === companyId && x.status === "open"));
      setForm(old => ({...old, branch: String(b.find(x => x.is_active && x.company === companyId)?.id || ""), fiscal_year: String(y.find(x => x.company === companyId && x.status === "open")?.id || "")}));
    }).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل إعدادات الأصول."));
    void refresh();
  }, [companyId]);

  const assetAccounts = useMemo(() => accounts.filter(x => x.nature === "asset"), [accounts]);
  const expenseAccounts = useMemo(() => accounts.filter(x => x.nature === "expense"), [accounts]);
  // إنشاء قائمة اختيار موحدة للحسابات المرتبطة ببطاقة الأصل.
const accountSelect = (label: string, key: keyof typeof emptyForm, rows: Account[]) => (
    <label>{label}<select required value={form[key]} onChange={e => setForm(old => ({...old,[key]:e.target.value}))}>
      <option value="">اختر الحساب</option>{rows.map(a => <option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}
    </select></label>
  );
  // حفظ بطاقة الأصل وتفاصيل التكلفة والعمر والحسابات المحاسبية.
const create = async (event: FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setSaving(true); setError("");
    try {
      await fixedAssetsApi.create({
        company: companyId, branch: Number(form.branch), code: form.code, name_ar: form.name_ar, description: form.description,
        acquisition_date: form.acquisition_date, depreciation_start_date: form.depreciation_start_date,
        original_cost: form.original_cost, salvage_value: form.salvage_value, useful_life_months: Number(form.useful_life_months),
        depreciation_method: "straight_line", asset_account: Number(form.asset_account),
        accumulated_depreciation_account: Number(form.accumulated_depreciation_account),
        depreciation_expense_account: Number(form.depreciation_expense_account),
        acquisition_counter_account: Number(form.acquisition_counter_account),
        disposal_gain_loss_account: Number(form.disposal_gain_loss_account), disposal_loss_account: Number(form.disposal_loss_account), fiscal_year: Number(form.fiscal_year),
      });
      setForm({...emptyForm, branch: form.branch, fiscal_year: form.fiscal_year});
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تسجيل الأصل."); }
    finally { setSaving(false); }
  };
  // طلب احتساب وترحيل إهلاك الأصل المحدد وفق الجدول المعتمد.
const depreciate = async (asset: FixedAsset) => {
    if (!form.fiscal_year) { setError("حدد سنة مالية مفتوحة."); return; }
    setSaving(true); setError("");
    try { await fixedAssetsApi.depreciate(asset.id, periodDate, Number(form.fiscal_year)); await refresh(); if (selected === asset.id) setSchedule(await fixedAssetsApi.schedule(asset.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تسجيل الإهلاك."); }
    finally { setSaving(false); }
  };
  // تحميل جدول الإهلاك التفصيلي للأصل لعرض الفترات والقيم.
const showSchedule = async (asset: FixedAsset) => {
    setSelected(asset.id); setError("");
    try { setSchedule(await fixedAssetsApi.schedule(asset.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل سجل الإهلاك."); }
  };
  // تنفيذ إجراء بيع أو استبعاد الأصل وإرسال بيانات التسوية للخادم.
const dispose = async (asset: FixedAsset) => {
    if (!form.fiscal_year || (Number(disposalProceeds) > 0 && !disposalCashAccount)) { setError("حدد السنة المالية وحساب البنك أو الخزينة عند وجود متحصلات."); return; }
    if (!window.confirm("تأكيد بيع / استبعاد الأصل؟ سيتم ترحيل الإهلاك حتى التاريخ وقيد الاستبعاد.")) return;
    setSaving(true); setError("");
    try {
      const result = await fixedAssetsApi.dispose(asset.id, disposalDate, disposalProceeds, Number(disposalCashAccount || 0), Number(form.fiscal_year));
      await refresh();
      setSchedule(await fixedAssetsApi.schedule(asset.id));
      setError("تم الاستبعاد. رقم القيد: " + result.disposal_journal + " · " + (Number(result.gain) > 0 ? "ربح " + result.gain : Number(result.loss) > 0 ? "خسارة " + result.loss : "دون ربح أو خسارة"));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر استبعاد الأصل."); }
    finally { setSaving(false); }
  };

  // تحميل تقارير سجل الأصول والإهلاك والحركة للفلاتر المحددة.
const loadReport = async () => {
    if (!companyId) return;
    setReportLoading(true); setReportError("");
    const branch = reportBranch ? Number(reportBranch) : undefined;
    try {
      if (reportTab === "register") setRegisterReport(await fixedAssetsApi.register(companyId, reportAsOf, branch));
      else if (reportTab === "depreciation") setDepreciationReport(await fixedAssetsApi.depreciationReport(companyId, reportFrom, reportTo, branch));
      else if (reportTab === "movements") setMovementReport(await fixedAssetsApi.movementReport(companyId, reportFrom, reportTo, branch));
      else if (reportTab === "cashflow") setCashFlowReport(await treasuryApi.cashFlow(companyId, reportFrom, reportTo, branch));
      else setAssetReconciliation(await fixedAssetsApi.accountReconciliation(companyId, reportAsOf, reportFrom, reportTo, branch));
    } catch (reason) { setReportError(reason instanceof Error ? reason.message : "تعذر تحميل التقرير."); }
    finally { setReportLoading(false); }
  };

  const runAllDepreciation = async () => {
    if (!companyId || !form.fiscal_year) { setError("اختر سنة مالية مفتوحة قبل تشغيل الإهلاك."); return; }
    if (!window.confirm(`سيتم احتساب وترحيل إهلاك جميع الأصول النشطة حتى ${periodDate} في الشركة/الفرع المحدد. هل تريد المتابعة؟`)) return;
    setSaving(true); setError(""); setNotice(""); setRunResult(null);
    try {
      const result = await fixedAssetsApi.runDepreciation(
        companyId, periodDate, Number(form.fiscal_year), reportBranch ? Number(reportBranch) : undefined
      );
      setRunResult(result);
      setNotice(`تم ترحيل إهلاك ${result.processed_count} أصل بإجمالي ${Number(result.processed_total).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}. الأصول المتعذرة أو المسجلة سابقًا: ${result.skipped_count}.`);
      await refresh();
      if (selected) setSchedule(await fixedAssetsApi.schedule(selected));
      if (reportTab === "depreciation") setDepreciationReport(await fixedAssetsApi.depreciationReport(companyId, reportFrom, reportTo, reportBranch ? Number(reportBranch) : undefined));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تشغيل الإهلاك الدوري."); }
    finally { setSaving(false); }
  };

  if (!companyId) return <section className="work-panel"><p>اختر شركة لعرض الأصول الثابتة.</p></section>;
  return <section className="work-panel assets-panel">
    <div className="work-panel-head"><div><h2>الأصول الثابتة</h2><p>تسجيل تكلفة الأصل، إثبات الاقتناء، وجدول الإهلاك وربطه بالقيود.</p></div><span className="coming-soon">{assets.length} أصل</span></div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    <form className="asset-form" onSubmit={create}>
      <div className="asset-form-heading"><strong>تسجيل أصل جديد</strong><small>يتم إنشاء قيد الاقتناء وترحيله تلقائيًا داخل معاملة واحدة.</small></div>
      <label>كود الأصل<input required value={form.code} onChange={e=>setForm({...form,code:e.target.value})}/></label>
      <label>اسم الأصل<input required value={form.name_ar} onChange={e=>setForm({...form,name_ar:e.target.value})}/></label>
      <label>الفرع<select required value={form.branch} onChange={e=>setForm({...form,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
      <label>تاريخ الاقتناء<input required type="date" value={form.acquisition_date} onChange={e=>setForm({...form,acquisition_date:e.target.value})}/></label>
      <label>بداية الإهلاك<input required type="date" value={form.depreciation_start_date} onChange={e=>setForm({...form,depreciation_start_date:e.target.value})}/></label>
      <label>التكلفة<input required type="number" min="0.0001" step="0.0001" value={form.original_cost} onChange={e=>setForm({...form,original_cost:e.target.value})}/></label>
      <label>القيمة التخريدية<input type="number" min="0" step="0.0001" value={form.salvage_value} onChange={e=>setForm({...form,salvage_value:e.target.value})}/></label>
      <label>العمر (شهر)<input required type="number" min="1" value={form.useful_life_months} onChange={e=>setForm({...form,useful_life_months:e.target.value})}/></label>
      <label>السنة المالية<select required value={form.fiscal_year} onChange={e=>setForm({...form,fiscal_year:e.target.value})}><option value="">اختر السنة</option>{years.map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
      {accountSelect("حساب تكلفة الأصل","asset_account",assetAccounts)}
      {accountSelect("حساب مجمع الإهلاك","accumulated_depreciation_account",assetAccounts)}
      {accountSelect("حساب مصروف الإهلاك","depreciation_expense_account",expenseAccounts)}
      {accountSelect("حساب مقابل الاقتناء","acquisition_counter_account",accounts)}
      {accountSelect("حساب أرباح بيع الأصول","disposal_gain_loss_account",accounts.filter(x=>x.nature==="revenue"))}
      {accountSelect("حساب خسائر بيع الأصول","disposal_loss_account",expenseAccounts)}
      <label className="asset-description">الوصف<input value={form.description} onChange={e=>setForm({...form,description:e.target.value})}/></label>
      <div className="asset-form-actions"><button className="primary-button" disabled={saving || !years.length}>{saving ? "جارٍ الحفظ…" : "حفظ الأصل وإثبات الاقتناء"}</button></div>
    </form>
    <div className="table-toolbar"><strong>سجل الأصول</strong><span className="module-count">{loading ? "جارٍ التحميل…" : assets.length + " أصل"}</span></div>
    <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الأصل</th><th>التكلفة</th><th>مجمع الإهلاك</th><th>صافي القيمة الدفترية</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
      {assets.map(a=><tr key={a.id}><td className="code-cell">{a.code}</td><td>{a.name_ar}</td><td>{a.acquisition_journal ? Number(a.original_cost).toLocaleString("ar-EG",{maximumFractionDigits:2}) : "—"}</td><td>{a.acquisition_journal ? Number(a.accumulated_depreciation).toLocaleString("ar-EG",{maximumFractionDigits:2}) : "—"}</td><td>{a.acquisition_journal ? Number(a.net_book_value).toLocaleString("ar-EG",{maximumFractionDigits:2}) : "—"}</td><td><span className="status-pill posted">{!a.acquisition_journal ? "بانتظار اعتماد الاقتناء" : a.status === "active" ? "نشط" : "مستبعد"}</span></td><td><button type="button" className="subtle-button" onClick={()=>void showSchedule(a)}>سجل الإهلاك / التصرف</button></td></tr>)}
    </tbody></table>{!assets.length && !loading && <div className="table-empty">لا توجد أصول مسجلة حتى الآن.</div>}</div>
    <div className="asset-reports">
      <div className="table-toolbar"><strong>تقارير الأصول والتدفقات الاستثمارية</strong><span className="module-count">حسب الشركة والفرع والفترة</span></div>
      <div className="asset-report-filters">
        <label>تاريخ تشغيل الإهلاك الدوري<input type="date" value={periodDate} onChange={e=>setPeriodDate(e.target.value)}/></label>
        <label>سنة الإهلاك<select value={form.fiscal_year} onChange={e=>setForm(old=>({...old,fiscal_year:e.target.value}))}><option value="">اختر سنة مفتوحة</option>{years.map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
        <button type="button" className="primary-button" disabled={saving || !form.fiscal_year} onClick={()=>void runAllDepreciation()}>{saving?"جارٍ ترحيل الإهلاك…":"تشغيل إهلاك جميع الأصول"}</button>
      </div>
      {runResult && runResult.skipped.length>0 && <div className="asset-report-note">الأصول التي لم تُرحّل: {runResult.skipped.slice(0,8).map(row=>row.code+" ("+String(row.reason)+")").join(" · ")}{runResult.skipped.length>8?" · وباقي السجلات في نتيجة التشغيل":""}</div>}
      <div className="asset-report-tabs">
        <button type="button" className={reportTab==="register"?"active":""} onClick={()=>setReportTab("register")}>سجل الأصول</button>
        <button type="button" className={reportTab==="depreciation"?"active":""} onClick={()=>setReportTab("depreciation")}>الإهلاك حسب الفرع</button>
        <button type="button" className={reportTab==="movements"?"active":""} onClick={()=>setReportTab("movements")}>حركة الأصل</button>
        <button type="button" className={reportTab==="cashflow"?"active":""} onClick={()=>setReportTab("cashflow")}>التدفقات النقدية</button>
        <button type="button" className={reportTab==="reconciliation"?"active":""} onClick={()=>setReportTab("reconciliation")}>مطابقة الأستاذ</button>
      </div>
      <div className="asset-report-filters">
        <label>الفرع<select value={reportBranch} onChange={e=>setReportBranch(e.target.value)}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        {reportTab==="register" || reportTab==="reconciliation" ? <label>كما في تاريخ<input type="date" value={reportAsOf} onChange={e=>setReportAsOf(e.target.value)}/></label> : null}
        {reportTab!=="register" && <><label>من تاريخ<input type="date" value={reportFrom} onChange={e=>setReportFrom(e.target.value)}/></label><label>إلى تاريخ<input type="date" value={reportTo} onChange={e=>setReportTo(e.target.value)}/></label></>}
        <button type="button" className="primary-button" disabled={reportLoading} onClick={()=>void loadReport()}>{reportLoading?"جارٍ إعداد التقرير…":"عرض التقرير"}</button>
      </div>
      {reportError && <div className="error-banner">{reportError}</div>}
      {reportTab==="reconciliation" && assetReconciliation && <>
        <div className="asset-cashflow-totals"><b>{assetReconciliation.is_reconciled?"سجل الأصول مطابق للأستاذ":"توجد فروق تحتاج مراجعة: "+assetReconciliation.difference_count}</b><span>التكلفة ومجمع الإهلاك كما في {assetReconciliation.as_of}</span><span>مصروف الإهلاك خلال {assetReconciliation.date_from} إلى {assetReconciliation.date_to}</span></div>
        {([
          ["تكلفة الأصول",assetReconciliation.cost_accounts],
          ["مجمع الإهلاك",assetReconciliation.accumulated_depreciation_accounts],
          ["مصروف الإهلاك",assetReconciliation.depreciation_expense_accounts],
        ] as [string, AssetAccountReconciliation["cost_accounts"]][]).map(([title,rows])=><div className="data-table-wrap" key={title}><h3>{title}</h3><table><thead><tr><th>الحساب</th><th>رصيد سجل الأصول</th><th>رصيد الأستاذ</th><th>الفرق</th></tr></thead><tbody>{rows.map(row=><tr key={row.account_id}><td>{row.code} · {row.name}</td><td>{Number(row.expected).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td><td>{Number(row.actual).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td><td>{Number(row.difference).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td></tr>)}{!rows.length&&<tr><td colSpan={4}>لا توجد حسابات مرتبطة بأصول ضمن النطاق.</td></tr>}</tbody></table></div>)}
        <p className="asset-report-note">{assetReconciliation.note}</p>
      </>}
      {reportTab==="register" && registerReport && <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الأصل</th><th>الفرع</th><th>التكلفة</th><th>مجمع الإهلاك</th><th>القيمة الدفترية</th><th>الحالة</th></tr></thead><tbody>{registerReport.rows.map(row=><tr key={row.asset_id}><td>{row.code}</td><td>{row.name_ar}</td><td>{row.branch_name}</td><td>{Number(row.original_cost).toLocaleString("ar-EG")}</td><td>{Number(row.accumulated_depreciation).toLocaleString("ar-EG")}</td><td>{Number(row.net_book_value).toLocaleString("ar-EG")}</td><td>{row.status==="active"?"نشط":"مستبعد"}</td></tr>)}</tbody></table>{!registerReport.rows.length&&<div className="table-empty">لا توجد أصول في التاريخ المحدد.</div>}</div>}
      {reportTab==="depreciation" && depreciationReport && <div className="data-table-wrap"><div className="asset-cashflow-totals"><b>إجمالي الإهلاك: {Number(depreciationReport.total_depreciation).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</b>{depreciationReport.branch_totals.map(item=><span key={item.branch_name}>{item.branch_name}: {Number(item.amount).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</span>)}</div><table><thead><tr><th>التاريخ</th><th>الأصل</th><th>الفرع</th><th>نوع الإهلاك</th><th>القيمة</th><th>القيد</th></tr></thead><tbody>{depreciationReport.rows.map((row,i)=><tr key={row.asset_id+"-"+row.period_date+"-"+i}><td>{row.period_date}</td><td>{row.asset_code} · {row.asset_name}</td><td>{row.branch_name}</td><td>{row.is_partial_period?"جزئي":"دوري"}</td><td>{Number(row.amount).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td><td>{row.journal_entry}</td></tr>)}</tbody></table>{!depreciationReport.rows.length&&<div className="table-empty">لا توجد حركات إهلاك.</div>}</div>}
      {reportTab==="movements" && movementReport && <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>الأصل</th><th>الفرع</th><th>الحركة</th><th>القيمة</th><th>القيد</th><th>الخزينة</th></tr></thead><tbody>{movementReport.rows.map((row,i)=><tr key={row.asset_id+"-"+row.event_type+"-"+row.date+"-"+i}><td>{row.date}</td><td>{row.asset_code} · {row.asset_name}</td><td>{row.branch_name}</td><td>{row.event_label}</td><td>{Number(row.amount).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td><td>{row.journal_entry||"—"}</td><td>{row.treasury_transaction||"—"}</td></tr>)}</tbody></table>{!movementReport.rows.length&&<div className="table-empty">لا توجد حركات للأصول.</div>}</div>}
      {reportTab==="cashflow" && cashFlowReport && <div className="data-table-wrap"><div className="asset-cashflow-totals">{cashFlowReport.categories.map(item=><span key={item.category}>{item.label}: صافي {Number(item.net).toLocaleString("ar-EG",{maximumFractionDigits:2})} (داخل {Number(item.inflows).toLocaleString("ar-EG",{maximumFractionDigits:2})} / خارج {Number(item.outflows).toLocaleString("ar-EG",{maximumFractionDigits:2})})</span>)}<b>صافي التدفق: {Number(cashFlowReport.net_cash_flow).toLocaleString("ar-EG",{maximumFractionDigits:2})}</b></div><p className="asset-report-note">{cashFlowReport.note}</p><table><thead><tr><th>التاريخ</th><th>المستند</th><th>البيان</th><th>الفرع</th><th>التصنيف</th><th>القيمة</th><th>القيد</th></tr></thead><tbody>{cashFlowReport.rows.map(row=><tr key={row.id}><td>{row.date}</td><td>{row.document_number}</td><td>{row.description}</td><td>{row.branch}</td><td>{row.category==="investing"?"استثماري":row.category==="financing"?"تمويلي":"تشغيلي"}</td><td>{Number(row.signed_amount).toLocaleString("ar-EG",{maximumFractionDigits:2})}</td><td>{row.journal_entry||"—"}</td></tr>)}</tbody></table>{!cashFlowReport.rows.length&&<div className="table-empty">لا توجد حركات نقدية مرحلة.</div>}</div>}
    </div>
    {selected && schedule && <div className="asset-schedule"><div className="work-panel-head"><div><h2>سجل إهلاك الأصل</h2><p>صافي القيمة الدفترية: {Number(schedule.net_book_value).toLocaleString("ar-EG",{maximumFractionDigits:2})}</p></div><button className="subtle-button" onClick={()=>{setSelected(null);setSchedule(null);}}>إغلاق</button></div>
      {!assets.find(x=>x.id===selected)?.acquisition_journal && <p className="asset-report-note">الاقتناء لم يُرحّل بعد؛ بعد اعتماد حركة السداد وترحيل قيد الاقتناء ستتاح إجراءات الإهلاك والاستبعاد.</p>}
      {assets.find(x=>x.id===selected)?.status === "active" && assets.find(x=>x.id===selected)?.acquisition_journal && <div className="asset-depreciation-action"><label>تاريخ الإهلاك<input type="date" value={periodDate} onChange={e=>setPeriodDate(e.target.value)}/></label><button className="primary-button" disabled={saving} onClick={()=>{const a=assets.find(x=>x.id===selected);if(a)void depreciate(a);}}>تسجيل إهلاك الفترة</button></div>}
      <div className="data-table-wrap"><table><thead><tr><th>تاريخ الفترة</th><th>قيمة الإهلاك</th><th>النوع</th><th>رقم القيد</th></tr></thead><tbody>{schedule.rows.map(row=><tr key={row.id}><td>{row.period_date}</td><td>{Number(row.amount).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2})}</td><td>{row.is_partial_period ? "حتى تاريخ الاستبعاد" : "شهري"}</td><td>{row.journal_entry}</td></tr>)}</tbody></table></div>
      {assets.find(x=>x.id===selected)?.status === "active" && assets.find(x=>x.id===selected)?.acquisition_journal && <div className="asset-disposal-form">
        <strong>بيع الأصل أو استبعاده</strong>
        <div className="asset-disposal-fields">
          <label>تاريخ البيع / الاستبعاد<input type="date" value={disposalDate} onChange={e=>setDisposalDate(e.target.value)}/></label>
          <label>قيمة المتحصلات<input type="number" min="0" step="0.0001" value={disposalProceeds} onChange={e=>setDisposalProceeds(e.target.value)}/></label>
          <label>حساب الخزينة / البنك<select value={disposalCashAccount} onChange={e=>setDisposalCashAccount(e.target.value)}><option value="">اختر حساب الخزينة</option>{treasuryAccounts.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        </div>
        <button className="danger-button" disabled={saving} onClick={()=>{const a=assets.find(x=>x.id===selected);if(a)void dispose(a);}}>{saving ? "جارٍ الترحيل…" : "تأكيد البيع / الاستبعاد"}</button>
      </div>}
    </div>}
  </section>;
}
