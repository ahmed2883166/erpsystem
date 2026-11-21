import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, fixedAssetsApi, treasuryApi, type Account, type Branch, type FiscalYear, type FixedAsset, type AssetSchedule, type TreasuryAccount } from "./api";

type Props = { companyId: number | null };
const today = new Date().toISOString().slice(0, 10);
const emptyForm = {
  branch: "", code: "", name_ar: "", description: "", acquisition_date: today, depreciation_start_date: today,
  original_cost: "", salvage_value: "0", useful_life_months: "60", asset_account: "",
  accumulated_depreciation_account: "", depreciation_expense_account: "", acquisition_counter_account: "",
  disposal_gain_loss_account: "", fiscal_year: "",
};

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
  const accountSelect = (label: string, key: keyof typeof emptyForm, rows: Account[]) => (
    <label>{label}<select required value={form[key]} onChange={e => setForm(old => ({...old,[key]:e.target.value}))}>
      <option value="">اختر الحساب</option>{rows.map(a => <option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}
    </select></label>
  );
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
        disposal_gain_loss_account: Number(form.disposal_gain_loss_account), fiscal_year: Number(form.fiscal_year),
      });
      setForm({...emptyForm, branch: form.branch, fiscal_year: form.fiscal_year});
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تسجيل الأصل."); }
    finally { setSaving(false); }
  };
  const depreciate = async (asset: FixedAsset) => {
    if (!form.fiscal_year) { setError("حدد سنة مالية مفتوحة."); return; }
    setSaving(true); setError("");
    try { await fixedAssetsApi.depreciate(asset.id, periodDate, Number(form.fiscal_year)); await refresh(); if (selected === asset.id) setSchedule(await fixedAssetsApi.schedule(asset.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تسجيل الإهلاك."); }
    finally { setSaving(false); }
  };
  const showSchedule = async (asset: FixedAsset) => {
    setSelected(asset.id); setError("");
    try { setSchedule(await fixedAssetsApi.schedule(asset.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل سجل الإهلاك."); }
  };
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

  if (!companyId) return <section className="work-panel"><p>اختر شركة لعرض الأصول الثابتة.</p></section>;
  return <section className="work-panel assets-panel">
    <div className="work-panel-head"><div><h2>الأصول الثابتة</h2><p>تسجيل تكلفة الأصل، إثبات الاقتناء، وجدول الإهلاك وربطه بالقيود.</p></div><span className="coming-soon">{assets.length} أصل</span></div>
    {error && <div className="error-banner">{error}</div>}
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
      {accountSelect("حساب أرباح/خسائر الاستبعاد","disposal_gain_loss_account",accounts)}
      <label className="asset-description">الوصف<input value={form.description} onChange={e=>setForm({...form,description:e.target.value})}/></label>
      <div className="asset-form-actions"><button className="primary-button" disabled={saving || !years.length}>{saving ? "جارٍ الحفظ…" : "حفظ الأصل وإثبات الاقتناء"}</button></div>
    </form>
    <div className="table-toolbar"><strong>سجل الأصول</strong><span className="module-count">{loading ? "جارٍ التحميل…" : assets.length + " أصل"}</span></div>
    <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الأصل</th><th>التكلفة</th><th>مجمع الإهلاك</th><th>صافي القيمة الدفترية</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
      {assets.map(a=><tr key={a.id}><td className="code-cell">{a.code}</td><td>{a.name_ar}</td><td>{Number(a.original_cost).toLocaleString("ar-EG",{maximumFractionDigits:2})}</td><td>{Number(a.accumulated_depreciation).toLocaleString("ar-EG",{maximumFractionDigits:2})}</td><td>{Number(a.net_book_value).toLocaleString("ar-EG",{maximumFractionDigits:2})}</td><td><span className="status-pill posted">{a.status === "active" ? "نشط" : "مستبعد"}</span></td><td><button type="button" className="subtle-button" onClick={()=>void showSchedule(a)}>سجل الإهلاك / التصرف</button></td></tr>)}
    </tbody></table>{!assets.length && !loading && <div className="table-empty">لا توجد أصول مسجلة حتى الآن.</div>}</div>
    {selected && schedule && <div className="asset-schedule"><div className="work-panel-head"><div><h2>سجل إهلاك الأصل</h2><p>صافي القيمة الدفترية: {Number(schedule.net_book_value).toLocaleString("ar-EG",{maximumFractionDigits:2})}</p></div><button className="subtle-button" onClick={()=>{setSelected(null);setSchedule(null);}}>إغلاق</button></div>
      {assets.find(x=>x.id===selected)?.status === "active" && <div className="asset-depreciation-action"><label>تاريخ الإهلاك<input type="date" value={periodDate} onChange={e=>setPeriodDate(e.target.value)}/></label><button className="primary-button" disabled={saving} onClick={()=>{const a=assets.find(x=>x.id===selected);if(a)void depreciate(a);}}>تسجيل إهلاك الفترة</button></div>}
      <div className="data-table-wrap"><table><thead><tr><th>تاريخ الفترة</th><th>قيمة الإهلاك</th><th>النوع</th><th>رقم القيد</th></tr></thead><tbody>{schedule.rows.map(row=><tr key={row.id}><td>{row.period_date}</td><td>{Number(row.amount).toLocaleString("ar-EG",{maximumFractionDigits:4})}</td><td>{row.is_partial_period ? "حتى تاريخ الاستبعاد" : "شهري"}</td><td>{row.journal_entry}</td></tr>)}</tbody></table></div>
      {assets.find(x=>x.id===selected)?.status === "active" && <div className="asset-disposal-form">
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
