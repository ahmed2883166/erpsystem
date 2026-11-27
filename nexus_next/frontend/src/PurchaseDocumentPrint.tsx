import { useEffect, useState } from "react";
import { salesApi, type Company, type SalesDocumentTemplate } from "./api";
import { formatMoneyEnglish } from "./formatters";
export type PurchasePrintDocument={documentType:SalesDocumentTemplate["document_type"];title:string;number:string;date:string;party:string;lines:{label:string;code?:string;quantity?:string;unitPrice?:string;amount?:string;detail?:string}[];summary:{label:string;value:string;emphasis?:boolean}[];notes?:string;related?:string};
type Props={company:Company|null;document:PurchasePrintDocument;onClose:()=>void};
const money=(v:string)=>formatMoneyEnglish(v);
const tr=(lang:string,ar:string,en:string)=>lang==="en"?en:lang==="bilingual"?ar+" / "+en:ar;
export default function PurchaseDocumentPrint({company,document,onClose}:Props){
 const [template,setTemplate]=useState<SalesDocumentTemplate|null>(null);
 useEffect(()=>{let active=true;if(!company?.id)return()=>{active=false;};void salesApi.documentTemplates(company.id).then(rows=>{if(active)setTemplate(rows.find(row=>row.document_type===document.documentType&&row.is_active&&row.is_default)||null);}).catch(()=>{if(active)setTemplate(null);});return()=>{active=false;};},[company?.id,document.documentType]);
 const config=template?.config||{},lang=config.language||"ar",label=(ar:string,en:string)=>tr(lang,ar,en);
 const visibleSummary=document.summary.filter(item=>{
  const key=item.label.toLocaleLowerCase();
  if(config.show_tax===false && (key.includes("ضريبة")||key.includes("tax")))return false;
  if(config.show_discount===false && (key.includes("خصم")||key.includes("discount")))return false;
  return true;
 });
 return <div className="sales-print-overlay"><div className="print-controls"><span>معاينة {document.title} · {template?.name||"Classic"}</span><div><button type="button" className="subtle-button" onClick={onClose}>إغلاق</button><button type="button" className="primary-button" onClick={()=>window.print()}>طباعة / حفظ PDF</button></div></div>
 <article className={`sales-print-sheet ${template?.style||"classic"} font-${config.font_scale||"normal"}`} style={{borderTopColor:config.accent_color||"#1F4E79"}}>
 <header className="sales-print-header"><div><h1>{company?.name_ar||"اسم الشركة"}</h1>{company?.name_en&&<p>{company.name_en}</p>}{company?.address&&<p>{company.address}</p>}{company?.phone&&<p>{label("هاتف","Phone")}: {company.phone}</p>}{config.show_tax_number!==false&&company?.tax_registration_number&&<p>{label("الرقم الضريبي","Tax ID")}: {company.tax_registration_number}</p>}</div><div className="sales-print-document-title"><strong>{document.title}</strong><span>{document.number}</span></div></header>
 <section className="sales-print-meta"><div><span>{label("المورد / الجهة","Supplier / Party")}</span><strong>{document.party}</strong></div><div><span>{label("التاريخ","Date")}</span><strong>{document.date}</strong></div>{document.related&&<div><span>{label("مرجع","Reference")}</span><strong>{document.related}</strong></div>}</section>
 {document.lines.length>0&&<table className="sales-print-lines"><thead><tr><th>{label("البيان","Description")}</th><th>{label("الكمية","Quantity")}</th><th>{label("سعر الوحدة","Unit price")}</th><th>{label("القيمة","Amount")}</th></tr></thead><tbody>{document.lines.map((line,i)=><tr key={i}><td><strong>{line.label}</strong>{line.code&&<small>{line.code}</small>}{line.detail&&<small>{line.detail}</small>}</td><td>{line.quantity||"—"}</td><td>{line.unitPrice?money(line.unitPrice):"—"}</td><td>{line.amount?money(line.amount):"—"}</td></tr>)}</tbody></table>}
 <section className="sales-print-totals">{visibleSummary.map((item,i)=><div className={item.emphasis?"grand":""} key={i}><span>{item.label}</span><strong>{item.value}</strong></div>)}</section>
 {document.notes&&<section className="sales-print-notes"><strong>{label("ملاحظات","Notes")}</strong><p>{document.notes}</p></section>}{config.show_bank_details&&config.bank_details&&<p className="sales-print-bank">{config.bank_details}</p>}<footer>{config.footer_text||" "}</footer></article></div>;
}
