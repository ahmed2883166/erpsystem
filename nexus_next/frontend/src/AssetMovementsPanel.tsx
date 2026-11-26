import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, fixedAssetsApi, hrApi, type AssetMovement, type FixedAsset, type Branch, type HrEmployee } from "./api";

type Props = { companyId:number|null; onChanged?:()=>void };
const today = new Date().toISOString().slice(0,10);
const empty = {asset:"",movement_type:"branch_transfer" as AssetMovement["movement_type"],movement_date:today,to_branch:"",to_employee:"",to_custodian:"",to_location:"",document_number:"",notes:""};

export default function AssetMovementsPanel({companyId,onChanged}:Props) {
  const [assets,setAssets]=useState<FixedAsset[]>([]);
  const [branches,setBranches]=useState<Branch[]>([]);
  const [employees,setEmployees]=useState<HrEmployee[]>([]);
  const [rows,setRows]=useState<AssetMovement[]>([]);
  const [form,setForm]=useState(empty);
  const [reviewNotes,setReviewNotes]=useState<Record<number,string>>({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");

  const selectedAsset=useMemo(()=>assets.find(a=>a.id===Number(form.asset))||null,[assets,form.asset]);
  const destinationBranch=useMemo(()=>{
    if(form.movement_type!=="branch_transfer") return selectedAsset?.branch||0;
    return Number(form.to_branch)||0;
  },[form.movement_type,form.to_branch,selectedAsset]);
  const availableEmployees=employees.filter(e=>e.status==="active"&&e.branch===destinationBranch);

  const refresh=async()=>{
    if(!companyId)return;
    const [a,b,e,m]=await Promise.all([
      fixedAssetsApi.list(companyId),api.branches(companyId),hrApi.employees(companyId),fixedAssetsApi.movements(companyId),
    ]);
    setAssets(a);setBranches(b.filter(x=>x.is_active&&x.company===companyId));setEmployees(e);setRows(m);
  };
  useEffect(()=>{if(companyId)void refresh().catch(reason=>setError(reason instanceof Error?reason.message:"تعذر تحميل حركة الأصول."));},[companyId]);

  const submit=async(event:FormEvent)=>{
    event.preventDefault();if(!companyId||!selectedAsset)return;
    setBusy(true);setError("");setNotice("");
    try{
      await fixedAssetsApi.createMovement({
        asset:selectedAsset.id,movement_type:form.movement_type,movement_date:form.movement_date,
        ...(form.movement_type==="branch_transfer"?{to_branch:Number(form.to_branch)}:{}),
        to_employee:form.to_employee?Number(form.to_employee):null,
        to_custodian:form.to_custodian,to_location:form.to_location,
        document_number:form.document_number.trim(),notes:form.notes,
      });
      setNotice("تم إنشاء مستند الحركة وإرساله للاعتماد. لن تتغير بيانات الأصل قبل موافقة المدير.");
      setForm({...empty,asset:form.asset,to_branch:form.to_branch});
      await refresh();onChanged?.();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر إنشاء مستند الحركة.");}
    finally{setBusy(false);}
  };

  const review=async(row:AssetMovement,approve:boolean)=>{
    const note=reviewNotes[row.id]?.trim()||"";
    if(!approve&&!note){setError("اكتب سبب الرفض أولاً.");return;}
    setBusy(true);setError("");setNotice("");
    try{
      if(approve) await fixedAssetsApi.approveMovement(row.id,note);
      else await fixedAssetsApi.rejectMovement(row.id,note);
      setNotice(approve?"تم اعتماد الحركة وتحديث بطاقة الأصل.":"تم رفض الحركة دون تغيير بطاقة الأصل.");
      await refresh();onChanged?.();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر مراجعة مستند الحركة.");}
    finally{setBusy(false);}
  };

  if(!companyId)return null;
  return <section className="asset-reports">
    <div className="table-toolbar"><strong>نقل الأصول والعهدة</strong><span className="module-count">مستندات بمراجعة واعتماد مدير الشركة</span></div>
    <p className="asset-report-note">تغيير الفرع أو الموظف المسؤول لا يتم مباشرة؛ يُحفظ الطلب أولاً، ثم يحدّث النظام بطاقة الأصل بعد الاعتماد مع الاحتفاظ بسجل الحركة السابق.</p>
    {error&&<div className="error-banner">{error}</div>}{notice&&<div className="asset-report-note">{notice}</div>}
    <form onSubmit={submit} className="asset-report-filters">
      <label>الأصل<select required value={form.asset} onChange={e=>setForm(v=>({...v,asset:e.target.value}))}><option value="">اختر الأصل</option>{assets.filter(a=>a.status==="active").map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar} — {a.branch_name||branches.find(b=>b.id===a.branch)?.name_ar||""}</option>)}</select></label>
      <label>نوع الحركة<select value={form.movement_type} onChange={e=>setForm(v=>({...v,movement_type:e.target.value as AssetMovement["movement_type"],to_employee:"",to_custodian:""}))}><option value="branch_transfer">نقل بين الفروع</option><option value="custody_change">تغيير العهدة</option><option value="location_change">تغيير الموقع</option></select></label>
      <label>تاريخ الحركة<input type="date" required value={form.movement_date} onChange={e=>setForm(v=>({...v,movement_date:e.target.value}))}/></label>
      {form.movement_type==="branch_transfer"&&<label>الفرع المستلم<select required value={form.to_branch} onChange={e=>setForm(v=>({...v,to_branch:e.target.value,to_employee:""}))}><option value="">اختر الفرع</option>{branches.filter(b=>b.id!==selectedAsset?.branch).map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>}
      {form.movement_type!=="location_change"&&<label>الموظف المستلم<select value={form.to_employee} onChange={e=>setForm(v=>({...v,to_employee:e.target.value,to_custodian:""}))}><option value="">بدون ربط بموظف</option>{availableEmployees.map(e=><option key={e.id} value={e.id}>{e.code} · {e.full_name_ar}</option>)}</select></label>}
      {form.movement_type!=="location_change"&&!form.to_employee&&<label>اسم المسؤول المستلم<input value={form.to_custodian} onChange={e=>setForm(v=>({...v,to_custodian:e.target.value}))}/></label>}
      {form.movement_type==="location_change"&&<label>الموقع الجديد<input value={form.to_location} onChange={e=>setForm(v=>({...v,to_location:e.target.value}))}/></label>}
      <label>رقم مستند التسليم / النقل<input required value={form.document_number} onChange={e=>setForm(v=>({...v,document_number:e.target.value}))}/></label>
      <label>ملاحظات<textarea value={form.notes} onChange={e=>setForm(v=>({...v,notes:e.target.value}))}/></label>
      <button className="primary-button" disabled={busy||!selectedAsset}>{busy?"جارٍ الحفظ…":"إرسال للاعتماد"}</button>
    </form>
    <div className="data-table-wrap"><table><thead><tr><th>التاريخ / المستند</th><th>الأصل</th><th>نوع الحركة</th><th>من</th><th>إلى</th><th>الحالة</th><th>مراجعة المدير</th></tr></thead>
      <tbody>{rows.map(row=><tr key={row.id}>
        <td>{row.movement_date}<div className="table-subtitle">{row.document_number}</div></td>
        <td>{row.asset_code} · {row.asset_name}</td>
        <td>{row.movement_type==="branch_transfer"?"نقل فرع":row.movement_type==="custody_change"?"تغيير عهدة":"تغيير موقع"}</td>
        <td>{row.from_branch_name}<div className="table-subtitle">{row.from_employee_name||row.from_custodian||row.from_location||"—"}</div></td>
        <td>{row.to_branch_name}<div className="table-subtitle">{row.to_employee_name||row.to_custodian||row.to_location||"—"}</div></td>
        <td>{row.status==="pending"?"بانتظار الاعتماد":row.status==="approved"?"معتمد":"مرفوض"}</td>
        <td>{row.status==="pending"?<div className="asset-review-actions"><input aria-label="ملاحظة المراجعة" placeholder="ملاحظة أو سبب الرفض" value={reviewNotes[row.id]||""} onChange={e=>setReviewNotes(v=>({...v,[row.id]:e.target.value}))}/><button type="button" className="subtle-button" disabled={busy} onClick={()=>void review(row,true)}>اعتماد</button><button type="button" className="subtle-button" disabled={busy} onClick={()=>void review(row,false)}>رفض</button></div>:<span>{row.review_note||row.reviewed_by_name||"—"}</span>}</td>
      </tr>)}</tbody></table>{!rows.length&&<div className="table-empty">لا توجد مستندات نقل أو عهدة مسجلة.</div>}</div>
  </section>;
}
