import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  fixedAssetsApi,
  type FixedAsset,
  type AssetComponent,
  type AssetInsurancePolicy,
  type AssetPhysicalInventory,
} from "./api";

type Props = { asset: FixedAsset; fiscalYearId: number | null; periodDate: string; onChanged?: () => void; onComponentUnitsChange?: (values:Record<string,string>) => void };

export default function AssetControlsPanel({ asset, fiscalYearId, periodDate, onChanged, onComponentUnitsChange }: Props) {
  const [components, setComponents] = useState<AssetComponent[]>([]);
  const [policies, setPolicies] = useState<AssetInsurancePolicy[]>([]);
  const [counts, setCounts] = useState<AssetPhysicalInventory[]>([]);
  const [componentUnits, setComponentUnits] = useState<Record<string,string>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [componentForm, setComponentForm] = useState({
    code: "", name_ar: "", original_cost: "", salvage_value: "0",
    useful_life_months: "60", depreciation_method: "straight_line" as AssetComponent["depreciation_method"],
    declining_rate_percentage: "", production_units_total: "",
  });
  const [policyForm, setPolicyForm] = useState({
    policy_number: "", insurer_name: "", coverage_description: "",
    start_date: new Date().toISOString().slice(0, 10), end_date: "",
    insured_value: "", premium_amount: "0", document_reference: "", notes: "",
  });
  const [countForm, setCountForm] = useState({
    count_date: new Date().toISOString().slice(0, 10), counted_location: "",
    counted_custodian: "", result: "matched" as AssetPhysicalInventory["result"],
    condition_notes: "", notes: "",
  });

  const reload = useCallback(async () => {
    const [componentRows, policyRows, countRows] = await Promise.all([
      fixedAssetsApi.components(asset.id),
      fixedAssetsApi.insurancePolicies(asset.id),
      fixedAssetsApi.physicalInventory(asset.id),
    ]);
    setComponents(componentRows);
    setPolicies(policyRows);
    setCounts(countRows);
  }, [asset.id]);

  useEffect(() => {
    void reload().catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل سجلات الأصل الرقابية."));
  }, [reload]);

  const submit = async (event: FormEvent, action: () => Promise<unknown>, success: string) => {
    event.preventDefault();
    setSaving(true); setError(""); setNotice("");
    try {
      await action();
      await reload();
      setNotice(success);
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ البيانات.");
    } finally {
      setSaving(false);
    }
  };

  const saveComponent = (event: FormEvent) => submit(event, () => fixedAssetsApi.createComponent({
    asset: asset.id,
    code: componentForm.code.trim(),
    name_ar: componentForm.name_ar.trim(),
    original_cost: componentForm.original_cost,
    salvage_value: componentForm.salvage_value,
    useful_life_months: Number(componentForm.useful_life_months),
    depreciation_method: componentForm.depreciation_method,
    declining_rate_percentage: componentForm.depreciation_method === "declining_balance" ? componentForm.declining_rate_percentage : null,
    production_units_total: componentForm.depreciation_method === "units_of_production" ? Number(componentForm.production_units_total) : null,
    asset_account: asset.asset_account,
    accumulated_depreciation_account: asset.accumulated_depreciation_account,
    depreciation_expense_account: asset.depreciation_expense_account,
  }), "تم حفظ مكوّن الأصل. يجب توزيع كامل التكلفة والقيمة التخريدية قبل تشغيل إهلاك المكونات.");

  const runComponentDepreciation = async () => {
    if (!fiscalYearId) { setError("حدد سنة مالية مفتوحة من شاشة الأصول."); return; }
    const units: Record<string,number> = {};
    for (const component of components.filter(row=>row.depreciation_method === "units_of_production")) {
      const quantity = Number(componentUnits[String(component.id)]);
      if (!(quantity > 0)) { setError("أدخل وحدات الإنتاج الفعلية لكل مكوّن يعمل بطريقة وحدات الإنتاج."); return; }
      units[String(component.id)] = quantity;
    }
    setSaving(true); setError(""); setNotice("");
    try {
      await fixedAssetsApi.depreciate(asset.id, periodDate, fiscalYearId, undefined, units);
      await reload();
      setNotice("تم احتساب وترحيل إهلاك مكونات الأصل.");
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر ترحيل إهلاك المكونات.");
    } finally {
      setSaving(false);
    }
  };

  const savePolicy = (event: FormEvent) => submit(event, () => fixedAssetsApi.createInsurancePolicy({
    asset: asset.id, ...policyForm, policy_number: policyForm.policy_number.trim(),
    insurer_name: policyForm.insurer_name.trim(),
  }), "تم تسجيل وثيقة التأمين.");

  const saveCount = (event: FormEvent) => submit(event, () => fixedAssetsApi.createPhysicalInventory({
    asset: asset.id, ...countForm,
  }), "تم حفظ محضر الجرد الفعلي.");

  return <section className="asset-reports">
    <div className="table-toolbar"><strong>مكونات الأصل والتأمين والجرد الفعلي</strong><span className="module-count">{asset.code} · {asset.name_ar}</span></div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}

    <div className="asset-form-heading"><strong>مكونات الأصل</strong><small>توزيع تكلفة الأصل على مكوناته الجوهرية؛ يجب أن يساوي مجموع تكاليف المكونات تكلفة الأصل، ومجموع القيم التخريدية قيمته التخريدية.</small></div>
    <form className="asset-form" onSubmit={saveComponent}>
      <label>كود المكوّن<input required value={componentForm.code} onChange={e=>setComponentForm({...componentForm,code:e.target.value})}/></label>
      <label>اسم المكوّن<input required value={componentForm.name_ar} onChange={e=>setComponentForm({...componentForm,name_ar:e.target.value})}/></label>
      <label>التكلفة<input required type="number" min="0.0001" step="0.0001" value={componentForm.original_cost} onChange={e=>setComponentForm({...componentForm,original_cost:e.target.value})}/></label>
      <label>القيمة التخريدية<input type="number" min="0" step="0.0001" value={componentForm.salvage_value} onChange={e=>setComponentForm({...componentForm,salvage_value:e.target.value})}/></label>
      <label>العمر الإنتاجي (شهر)<input required type="number" min="1" value={componentForm.useful_life_months} onChange={e=>setComponentForm({...componentForm,useful_life_months:e.target.value})}/></label>
      <label>طريقة الإهلاك<select value={componentForm.depreciation_method} onChange={e=>setComponentForm({...componentForm,depreciation_method:e.target.value as AssetComponent["depreciation_method"],declining_rate_percentage:"",production_units_total:""})}>
        <option value="straight_line">القسط الثابت</option><option value="declining_balance">القسط المتناقص</option><option value="units_of_production">وحدات الإنتاج</option>
      </select></label>
      {componentForm.depreciation_method === "declining_balance" && <label>معدل الإهلاك السنوي (%)<input required type="number" min="0.0001" max="100" step="0.0001" value={componentForm.declining_rate_percentage} onChange={e=>setComponentForm({...componentForm,declining_rate_percentage:e.target.value})}/></label>}
      {componentForm.depreciation_method === "units_of_production" && <label>إجمالي وحدات الإنتاج<input required type="number" min="1" step="1" value={componentForm.production_units_total} onChange={e=>setComponentForm({...componentForm,production_units_total:e.target.value})}/></label>}
      <button className="primary-button" type="submit" disabled={saving}>إضافة مكوّن</button>
    </form>
    <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>المكوّن</th><th>التكلفة</th><th>العمر</th><th>طريقة الإهلاك</th><th>الإهلاك المتراكم</th></tr></thead><tbody>
      {components.map(row=><tr key={row.id}><td>{row.code}</td><td>{row.name_ar}</td><td>{Number(row.original_cost).toLocaleString("ar-EG")}</td><td>{row.useful_life_months} شهر</td><td>{row.depreciation_method==="straight_line"?"ثابت":row.depreciation_method==="declining_balance"?"متناقص":"وحدات الإنتاج"}</td><td>{Number(row.accumulated_depreciation).toLocaleString("ar-EG")}</td></tr>)}
      {!components.length && <tr><td colSpan={6}>لم يتم تسجيل مكونات بعد.</td></tr>}
    </tbody></table></div>
    {components.some(row=>row.depreciation_method === "units_of_production") && <div className="asset-report-filters">
      {components.filter(row=>row.depreciation_method === "units_of_production").map(row=><label key={row.id}>وحدات الإنتاج الفعلية · {row.name_ar}
        <input type="number" min="1" step="1" value={componentUnits[String(row.id)] || ""} onChange={e=>{const next={...componentUnits,[String(row.id)]:e.target.value};setComponentUnits(next);onComponentUnitsChange?.(next);}}/>
      </label>)}
    </div>}
    {components.length > 0 && <button className="primary-button" type="button" disabled={saving} onClick={()=>void runComponentDepreciation()}>تشغيل إهلاك المكونات للفترة</button>}

    <div className="asset-form-heading"><strong>وثائق التأمين</strong><small>سجل الوثيقة والتغطية وقيمة التأمين والقسط وفترة السريان؛ لا يتم إنشاء قيد تلقائي من هذا السجل.</small></div>
    <form className="asset-form" onSubmit={savePolicy}>
      <label>رقم الوثيقة<input required value={policyForm.policy_number} onChange={e=>setPolicyForm({...policyForm,policy_number:e.target.value})}/></label>
      <label>شركة التأمين<input required value={policyForm.insurer_name} onChange={e=>setPolicyForm({...policyForm,insurer_name:e.target.value})}/></label>
      <label>بداية الوثيقة<input required type="date" value={policyForm.start_date} onChange={e=>setPolicyForm({...policyForm,start_date:e.target.value})}/></label>
      <label>نهاية الوثيقة<input required type="date" value={policyForm.end_date} onChange={e=>setPolicyForm({...policyForm,end_date:e.target.value})}/></label>
      <label>قيمة التغطية<input required type="number" min="0" step="0.0001" value={policyForm.insured_value} onChange={e=>setPolicyForm({...policyForm,insured_value:e.target.value})}/></label>
      <label>قسط التأمين<input type="number" min="0" step="0.0001" value={policyForm.premium_amount} onChange={e=>setPolicyForm({...policyForm,premium_amount:e.target.value})}/></label>
      <label>مرجع الوثيقة / الملف<input value={policyForm.document_reference} onChange={e=>setPolicyForm({...policyForm,document_reference:e.target.value})}/></label>
      <label>نطاق التغطية<input value={policyForm.coverage_description} onChange={e=>setPolicyForm({...policyForm,coverage_description:e.target.value})}/></label>
      <button className="primary-button" type="submit" disabled={saving}>تسجيل وثيقة التأمين</button>
    </form>
    <div className="data-table-wrap"><table><thead><tr><th>الوثيقة</th><th>شركة التأمين</th><th>من</th><th>إلى</th><th>قيمة التغطية</th><th>الحالة</th></tr></thead><tbody>
      {policies.map(row=><tr key={row.id}><td>{row.policy_number}</td><td>{row.insurer_name}</td><td>{row.start_date}</td><td>{row.end_date}</td><td>{Number(row.insured_value).toLocaleString("ar-EG")}</td><td>{row.status==="active"?"سارية":row.status==="expired"?"منتهية":"ملغاة"}</td></tr>)}
      {!policies.length && <tr><td colSpan={6}>لا توجد وثائق تأمين مسجلة.</td></tr>}
    </tbody></table></div>

    <div className="asset-form-heading"><strong>الجرد الفعلي</strong><small>الموقع والعهدة المتوقعان يؤخذان من بطاقة الأصل؛ سجل الجرد يحفظ نتيجة المعاينة الفعلية فقط.</small></div>
    <form className="asset-form" onSubmit={saveCount}>
      <label>تاريخ الجرد<input required type="date" value={countForm.count_date} onChange={e=>setCountForm({...countForm,count_date:e.target.value})}/></label>
      <label>الموقع الفعلي<input value={countForm.counted_location} onChange={e=>setCountForm({...countForm,counted_location:e.target.value})}/></label>
      <label>المستلم / المسؤول الفعلي<input value={countForm.counted_custodian} onChange={e=>setCountForm({...countForm,counted_custodian:e.target.value})}/></label>
      <label>نتيجة الجرد<select value={countForm.result} onChange={e=>setCountForm({...countForm,result:e.target.value as AssetPhysicalInventory["result"]})}>
        <option value="matched">مطابق</option><option value="location_mismatch">اختلاف موقع</option><option value="custodian_mismatch">اختلاف عهدة</option><option value="condition_issue">ملاحظة حالة</option><option value="not_found">غير موجود</option>
      </select></label>
      <label>حالة الأصل / الملاحظات<input value={countForm.condition_notes} onChange={e=>setCountForm({...countForm,condition_notes:e.target.value})}/></label>
      <button className="primary-button" type="submit" disabled={saving}>حفظ محضر الجرد</button>
    </form>
    <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>الموقع الفعلي</th><th>العهدة الفعلية</th><th>النتيجة</th><th>ملاحظات الحالة</th></tr></thead><tbody>
      {counts.map(row=><tr key={row.id}><td>{row.count_date}</td><td>{row.counted_location}</td><td>{row.counted_custodian}</td><td>{row.result==="matched"?"مطابق":row.result==="not_found"?"غير موجود":row.result==="location_mismatch"?"اختلاف موقع":row.result==="custodian_mismatch"?"اختلاف عهدة":"ملاحظة حالة"}</td><td>{row.condition_notes}</td></tr>)}
      {!counts.length && <tr><td colSpan={5}>لا توجد محاضر جرد مسجلة.</td></tr>}
    </tbody></table></div>
  </section>;
}
