import { useEffect, useState, type FormEvent } from "react";
import { salesApi, type SalesDocumentTemplate } from "./api";

type Props = { companyId:number|null };
type DocumentType = SalesDocumentTemplate["document_type"];
type Style = SalesDocumentTemplate["style"];

const documentLabels:Record<DocumentType,string> = {
  quotation:"عروض الأسعار", invoice:"فواتير المبيعات", delivery:"أذون التسليم", receipt:"سندات القبض",
};

export default function DocumentTemplatesPanel({companyId}:Props) {
  const [rows,setRows]=useState<SalesDocumentTemplate[]>([]);
  const [documentType,setDocumentType]=useState<DocumentType>("invoice");
  const [editingId,setEditingId]=useState<number|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [form,setForm]=useState({
    name:"",style:"classic" as Style,accent_color:"#1F4E79",
    show_tax_number:true,show_discount:true,show_tax:true,show_bank_details:false,
    language:"ar" as "ar"|"en"|"bilingual",font_scale:"normal" as "small"|"normal"|"large",
    footer_text:"شكراً لتعاملكم معنا",is_default:false,
  });

  const refresh=async()=>{
    if(!companyId){setRows([]);return;}
    setBusy(true);setError("");
    try{setRows(await salesApi.documentTemplates(companyId));}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تحميل قوالب الطباعة.");}
    finally{setBusy(false);}
  };
  useEffect(()=>{void refresh();},[companyId]);

  const reset=()=>{
    setEditingId(null);
    setForm({name:"",style:"classic",accent_color:"#1F4E79",show_tax_number:true,show_discount:true,show_tax:true,show_bank_details:false,language:"ar",font_scale:"normal",footer_text:"شكراً لتعاملكم معنا",is_default:false});
  };
  const edit=(row:SalesDocumentTemplate)=>{
    setEditingId(row.id);setDocumentType(row.document_type);
    setForm({
      name:row.name,style:row.style,accent_color:row.config.accent_color||"#1F4E79",
      show_tax_number:row.config.show_tax_number??true,show_discount:row.config.show_discount??true,
      show_tax:row.config.show_tax??true,show_bank_details:row.config.show_bank_details??false,
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
      config:{accent_color:form.accent_color,show_tax_number:form.show_tax_number,
        show_discount:form.show_discount,show_tax:form.show_tax,show_bank_details:form.show_bank_details,
        language:form.language,font_scale:form.font_scale,footer_text:form.footer_text},
    };
    try{if(editingId)await salesApi.updateDocumentTemplate(editingId,payload);else await salesApi.createDocumentTemplate(payload);reset();await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر حفظ القالب.");}
    finally{setBusy(false);}
  };
  const makeDefault=async(row:SalesDocumentTemplate)=>{
    setBusy(true);setError("");
    try{await salesApi.updateDocumentTemplate(row.id,{is_default:true});await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تعيين القالب الافتراضي.");}
    finally{setBusy(false);}
  };
  const toggle=async(row:SalesDocumentTemplate)=>{
    setBusy(true);setError("");
    try{await salesApi.updateDocumentTemplate(row.id,{is_active:!row.is_active,is_default:row.is_active?false:row.is_default});await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تغيير حالة القالب.");}
    finally{setBusy(false);}
  };
  const visible=rows.filter(row=>row.document_type===documentType);

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>قوالب الطباعة</h2><p>كل شركة تختار شكلًا مستقلًا لعروض الأسعار والفواتير وأذون التسليم وسندات القبض.</p></div></div>
    {error&&<div className="error-banner">{error}</div>}
    {!companyId&&<div className="table-empty">اختر شركة لإدارة قوالبها.</div>}
    {companyId&&<>
      <div className="customer-toolbar">
        <label>نوع المستند<select value={documentType} onChange={e=>{setDocumentType(e.target.value as DocumentType);reset();}}>{Object.entries(documentLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
        <button className="primary-button" type="button" onClick={reset}>قالب جديد</button>
      </div>
      <form className="customer-form" onSubmit={save}>
        <div className="customer-form-wide"><strong>{editingId?"تعديل القالب":"إضافة قالب جديد"}</strong></div>
        <label>اسم القالب<input required maxLength={80} value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="مثال: الفاتورة الحديثة"/></label>
        <label>التصميم<select value={form.style} onChange={e=>setForm({...form,style:e.target.value as Style})}><option value="classic">Classic — كلاسيكي</option><option value="modern">Modern — حديث</option><option value="compact">Compact — مختصر</option></select></label>
        <label>لون الهوية<input type="color" value={form.accent_color} onChange={e=>setForm({...form,accent_color:e.target.value})}/></label>
        <label>لغة الطباعة<select value={form.language} onChange={e=>setForm({...form,language:e.target.value as typeof form.language})}><option value="ar">العربية</option><option value="en">English</option><option value="bilingual">عربي / English</option></select></label>
        <label>حجم الخط<select value={form.font_scale} onChange={e=>setForm({...form,font_scale:e.target.value as typeof form.font_scale})}><option value="small">صغير</option><option value="normal">عادي</option><option value="large">كبير</option></select></label>
        <label className="customer-form-wide">نص التذييل<input maxLength={500} value={form.footer_text} onChange={e=>setForm({...form,footer_text:e.target.value})}/></label>
        <div className="customer-form-wide table-actions">
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
          <div className="document-template-preview-company">اسم الشركة</div><div className="document-template-preview-line">فاتورة مبيعات / عرض سعر</div>
          <div className="document-template-preview-line">العميل: عميل تجريبي</div><div className="document-template-preview-table">الصنف　　 الكمية　　 السعر　　 الإجمالي</div>
          <div className="document-template-preview-total">الإجمالي: 1,250.00</div>
          <small>{form.footer_text||" "}</small>
        </div>)}
      </div>
      <div className="data-table-wrap"><table><thead><tr><th>اسم القالب</th><th>الشكل</th><th>اللغة</th><th>افتراضي</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>
        {visible.map(row=><tr key={row.id}><td>{row.name}</td><td>{row.style}</td><td>{row.config.language||"ar"}</td><td>{row.is_default?"نعم":"—"}</td><td>{row.is_active?"نشط":"موقوف"}</td><td><div className="table-actions"><button className="subtle-button" type="button" disabled={busy} onClick={()=>edit(row)}>تعديل</button>{row.is_active&&!row.is_default&&<button className="subtle-button" type="button" disabled={busy} onClick={()=>void makeDefault(row)}>جعله افتراضيًا</button>}<button className="subtle-button" type="button" disabled={busy} onClick={()=>void toggle(row)}>{row.is_active?"إيقاف":"تنشيط"}</button></div></td></tr>)}
      </tbody></table>{!busy&&visible.length===0&&<div className="table-empty">لا توجد قوالب لهذا النوع. أضف Classic أو Modern أو Compact.</div>}</div>
    </>}
  </section>;
}
