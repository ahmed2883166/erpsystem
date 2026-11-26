import { useEffect, useState, type FormEvent } from "react";
import { purchasingApi, type PurchaseDocumentTemplate } from "./api";

type Props = { companyId:number|null; onClose?:()=>void };
type DocumentType = PurchaseDocumentTemplate["document_type"];
type Style = PurchaseDocumentTemplate["style"];

const documentLabels:Record<DocumentType,string> = {
  purchase_order:"أوامر الشراء",
  purchase_receipt:"أذون الاستلام",
  purchase_invoice:"فواتير الموردين",
  supplier_return:"مرتجعات الموردين",
  supplier_payment:"سندات الصرف",
  supplier_refund:"استردادات الموردين",
};

const initialForm = () => ({
  name:"",style:"classic" as Style,accent_color:"#1F4E79",
  show_logo:true,show_tax_number:true,show_discount:true,show_tax:true,
  show_bank_details:false,bank_details:"",
  language:"ar" as "ar"|"en"|"bilingual",
  font_scale:"normal" as "small"|"normal"|"large",
  footer_text:"نشكركم على التعامل معنا",is_default:false,
});

export default function PurchaseDocumentTemplatesPanel({companyId,onClose}:Props) {
  const [rows,setRows]=useState<PurchaseDocumentTemplate[]>([]);
  const [documentType,setDocumentType]=useState<DocumentType>("purchase_invoice");
  const [editingId,setEditingId]=useState<number|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [form,setForm]=useState(initialForm);

  const refresh=async()=>{
    if(!companyId){setRows([]);return;}
    setBusy(true);setError("");
    try{setRows(await purchasingApi.documentTemplates(companyId));}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تحميل قوالب المشتريات.");}
    finally{setBusy(false);}
  };
  useEffect(()=>{void refresh();},[companyId]);

  const reset=()=>{setEditingId(null);setForm(initialForm());};
  const edit=(row:PurchaseDocumentTemplate)=>{
    setEditingId(row.id);setDocumentType(row.document_type);
    setForm({
      name:row.name,style:row.style,accent_color:row.config.accent_color||"#1F4E79",
      show_logo:row.config.show_logo??true,show_tax_number:row.config.show_tax_number??true,
      show_discount:row.config.show_discount??true,show_tax:row.config.show_tax??true,
      show_bank_details:row.config.show_bank_details??false,bank_details:row.config.bank_details||"",
      language:row.config.language||"ar",font_scale:row.config.font_scale||"normal",
      footer_text:row.config.footer_text||"",is_default:row.is_default,
    });
  };
  const save=async(event:FormEvent)=>{
    event.preventDefault();if(!companyId)return;
    setBusy(true);setError("");
    const payload={
      company:companyId,document_type:documentType,name:form.name,style:form.style,
      is_default:form.is_default,is_active:true,
      config:{
        accent_color:form.accent_color,show_logo:form.show_logo,
        show_tax_number:form.show_tax_number,show_discount:form.show_discount,
        show_tax:form.show_tax,show_bank_details:form.show_bank_details,
        bank_details:form.bank_details,language:form.language,
        font_scale:form.font_scale,footer_text:form.footer_text,
      },
    };
    try{
      if(editingId)await purchasingApi.updateDocumentTemplate(editingId,payload);
      else await purchasingApi.createDocumentTemplate(payload);
      reset();await refresh();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر حفظ القالب.");}
    finally{setBusy(false);}
  };
  const makeDefault=async(row:PurchaseDocumentTemplate)=>{
    setBusy(true);setError("");
    try{await purchasingApi.updateDocumentTemplate(row.id,{is_default:true});await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تعيين القالب الافتراضي.");}
    finally{setBusy(false);}
  };
  const toggle=async(row:PurchaseDocumentTemplate)=>{
    setBusy(true);setError("");
    try{await purchasingApi.updateDocumentTemplate(row.id,{is_active:!row.is_active,is_default:row.is_active?false:row.is_default});await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تغيير حالة القالب.");}
    finally{setBusy(false);}
  };
  const visible=rows.filter(row=>row.document_type===documentType);

  return <section className="work-panel">
    <div className="work-panel-head">
      <div><h2>قوالب طباعة المشتريات</h2><p>احفظ أكثر من تصميم لكل مستند، وحدد قالبًا افتراضيًا مستقلًا لكل نوع. إيقاف القالب لا يحذف إعداداته.</p></div>
      {onClose&&<button type="button" className="subtle-button" onClick={onClose}>العودة إلى المشتريات</button>}
    </div>
    {error&&<div className="error-banner">{error}</div>}
    {!companyId&&<div className="table-empty">اختر شركة لإدارة قوالبها.</div>}
    {companyId&&<>
      <div className="customer-toolbar">
        <label>نوع المستند<select value={documentType} onChange={e=>{setDocumentType(e.target.value as DocumentType);reset();}}>
          {Object.entries(documentLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}
        </select></label>
        <button className="primary-button" type="button" onClick={reset}>＋ قالب جديد</button>
      </div>
      <form className="customer-form" onSubmit={save}>
        <div className="customer-form-wide"><strong>{editingId?"تعديل القالب":"إضافة قالب مستقل"}</strong></div>
        <label>اسم القالب<input required maxLength={80} value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="مثال: فاتورة المورد الحديثة"/></label>
        <label>التصميم<select value={form.style} onChange={e=>setForm({...form,style:e.target.value as Style})}><option value="classic">Classic — كلاسيكي</option><option value="modern">Modern — حديث</option><option value="compact">Compact — مختصر</option></select></label>
        <label>لون الهوية<input type="color" value={form.accent_color} onChange={e=>setForm({...form,accent_color:e.target.value})}/></label>
        <label>لغة الطباعة<select value={form.language} onChange={e=>setForm({...form,language:e.target.value as typeof form.language})}><option value="ar">العربية</option><option value="en">English</option><option value="bilingual">عربي / English</option></select></label>
        <label>حجم الخط<select value={form.font_scale} onChange={e=>setForm({...form,font_scale:e.target.value as typeof form.font_scale})}><option value="small">صغير</option><option value="normal">عادي</option><option value="large">كبير</option></select></label>
        <label className="customer-form-wide">بيانات البنك<input maxLength={500} value={form.bank_details} onChange={e=>setForm({...form,bank_details:e.target.value})} placeholder="اسم البنك، رقم الحساب، IBAN"/></label>
        <label className="customer-form-wide">نص التذييل<input maxLength={500} value={form.footer_text} onChange={e=>setForm({...form,footer_text:e.target.value})}/></label>
        <div className="customer-form-wide table-actions">
          <label><input type="checkbox" checked={form.show_logo} onChange={e=>setForm({...form,show_logo:e.target.checked})}/> إظهار الشعار</label>
          <label><input type="checkbox" checked={form.show_tax_number} onChange={e=>setForm({...form,show_tax_number:e.target.checked})}/> إظهار الرقم الضريبي</label>
          <label><input type="checkbox" checked={form.show_discount} onChange={e=>setForm({...form,show_discount:e.target.checked})}/> إظهار الخصم</label>
          <label><input type="checkbox" checked={form.show_tax} onChange={e=>setForm({...form,show_tax:e.target.checked})}/> إظهار الضريبة</label>
          <label><input type="checkbox" checked={form.show_bank_details} onChange={e=>setForm({...form,show_bank_details:e.target.checked})}/> إظهار بيانات البنك</label>
          <label><input type="checkbox" checked={form.is_default} onChange={e=>setForm({...form,is_default:e.target.checked})}/> القالب الافتراضي لهذا النوع</label>
        </div>
        <div className="customer-form-actions customer-form-wide"><button type="button" className="subtle-button" onClick={reset}>إلغاء</button><button type="submit" className="primary-button" disabled={busy}>{busy?"جارٍ الحفظ…":editingId?"حفظ التعديلات":"حفظ القالب"}</button></div>
      </form>
      <div className="document-template-preview-grid">
        {(["classic","modern","compact"] as Style[]).map(style=><div className={`document-template-preview ${style}`} key={style} style={{borderTopColor:form.accent_color}}>
          <strong>{style==="classic"?"Classic":style==="modern"?"Modern":"Compact"}</strong>
          <div className="document-template-preview-company">اسم الشركة</div><div className="document-template-preview-line">{documentLabels[documentType]}</div>
          <div className="document-template-preview-line">المورد: مورد تجريبي</div><div className="document-template-preview-table">الصنف　　 الكمية　　 السعر　　 الإجمالي</div>
          <div className="document-template-preview-total">الإجمالي: 1,250.00</div><small>{form.footer_text||" "}</small>
        </div>)}
      </div>
      <div className="data-table-wrap"><table><thead><tr><th>اسم القالب</th><th>التصميم</th><th>اللغة</th><th>افتراضي</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>
        {visible.map(row=><tr key={row.id}><td>{row.name}</td><td>{row.style}</td><td>{row.config.language||"ar"}</td><td>{row.is_default?"نعم":"—"}</td><td>{row.is_active?"نشط":"موقوف"}</td><td><div className="table-actions"><button className="subtle-button" type="button" disabled={busy} onClick={()=>edit(row)}>تعديل</button>{row.is_active&&!row.is_default&&<button className="subtle-button" type="button" disabled={busy} onClick={()=>void makeDefault(row)}>جعله افتراضيًا</button>}<button className="subtle-button" type="button" disabled={busy} onClick={()=>void toggle(row)}>{row.is_active?"إيقاف":"تنشيط"}</button></div></td></tr>)}
      </tbody></table>{!busy&&visible.length===0&&<div className="table-empty">لا توجد قوالب لهذا النوع. أضف أكثر من تصميم حسب احتياج الشركة.</div>}</div>
    </>}
  </section>;
}
