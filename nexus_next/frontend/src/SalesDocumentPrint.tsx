import type { Company, SalesInvoice, SalesQuotation } from "./api";

type Props = {
  company:Company|null;
  invoice?:SalesInvoice|null;
  quotation?:SalesQuotation|null;
  onClose:()=>void;
};
type PrintLine = {
  product_code:string;product_name:string;quantity:string;unit_price:string;
  discount_percent:string;discount_amount:string;tax_rate:string;tax_amount:string;total_amount:string;
};
const money=(value:string)=>Number(value||0).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});

export default function SalesDocumentPrint({company,invoice,quotation,onClose}:Props){
  const doc=invoice||quotation;
  if(!doc)return null;
  const isInvoice=Boolean(invoice);
  const snapshot=doc.print_template_snapshot||{template_id:null,name:"Classic",style:"classic" as const,config:{}};
  const config=snapshot.config||{};
  const language=config.language||"ar";
  const label=(ar:string,en:string)=>language==="en"?en:language==="bilingual"?ar+" / "+en:ar;
  const lines:PrintLine[]=isInvoice
    ? invoice!.invoice_lines.map(line=>({product_code:line.product_code,product_name:line.product_name,quantity:line.quantity,unit_price:line.transaction_unit_price||line.unit_price,discount_percent:line.discount_percent,discount_amount:line.discount_amount,tax_rate:line.tax_rate,tax_amount:line.tax_amount,total_amount:line.total_amount}))
    : quotation!.quotation_lines.map(line=>({product_code:line.product_code,product_name:line.product_name,quantity:line.quantity,unit_price:line.unit_price,discount_percent:line.discount_percent,discount_amount:line.discount_amount,tax_rate:line.tax_rate,tax_amount:line.tax_amount,total_amount:line.total_amount}));
  const number=isInvoice?invoice!.invoice_number:quotation!.quotation_number;
  const date=isInvoice?invoice!.invoice_date:quotation!.quotation_date;
  const total=isInvoice?invoice!.grand_total:quotation!.grand_total;
  const net=isInvoice?invoice!.net_total:quotation!.net_total;
  const discount=isInvoice?invoice!.discount_total:quotation!.discount_total;
  const tax=isInvoice?invoice!.tax_total:quotation!.tax_total;
  const customer=isInvoice?invoice!.customer_name:quotation!.customer_name;
  return <div className="sales-print-overlay">
    <div className="print-controls"><span>معاينة {isInvoice?"فاتورة المبيعات":"عرض السعر"} · {snapshot.name}</span><div><button type="button" className="subtle-button" onClick={onClose}>إغلاق</button><button type="button" className="primary-button" onClick={()=>window.print()}>طباعة / حفظ PDF</button></div></div>
    <article className={`sales-print-sheet ${snapshot.style} font-${config.font_scale||"normal"}`} style={{borderTopColor:config.accent_color||"#1F4E79"}}>
      <header className="sales-print-header">
        <div><h1>{company?.name_ar||"اسم الشركة"} </h1>{company?.name_en&&<p>{company.name_en}</p>}
          {company?.address&&<p>{company.address}</p>}{company?.phone&&<p>{label("هاتف","Phone")}: {company.phone}</p>}
          {config.show_tax_number!==false&&company?.tax_registration_number&&<p>{label("رقم التسجيل الضريبي","Tax ID")}: {company.tax_registration_number}</p>}
        </div>
        <div className="sales-print-document-title"><strong>{label(isInvoice?"فاتورة مبيعات":"عرض سعر",isInvoice?"Sales Invoice":"Quotation")}</strong><span>{number}</span></div>
      </header>
      <section className="sales-print-meta"><div><span>{label("العميل","Customer")}</span><strong>{customer}</strong></div><div><span>{label("التاريخ","Date")}</span><strong>{date}</strong></div>
        {!isInvoice&&<div><span>{label("صالح حتى","Valid until")}</span><strong>{quotation!.valid_until}</strong></div>}
        {isInvoice&&<div><span>{label("الحالة","Status")}</span><strong>{invoice!.status==="posted"?label("مرحّلة","Posted"):label("مسودة","Draft")}</strong></div>}
      </section>
      <table className="sales-print-lines"><thead><tr><th>{label("الصنف","Item")}</th><th>{label("الكمية","Qty")}</th><th>{label("سعر الوحدة","Unit price")}</th>{config.show_discount!==false&&<th>{label("الخصم","Discount")}</th>}{config.show_tax!==false&&<th>{label("الضريبة","Tax")}</th>}<th>{label("الإجمالي","Total")}</th></tr></thead>
        <tbody>{lines.map((line,index)=><tr key={index}><td><strong>{line.product_name}</strong><small>{line.product_code}</small></td><td>{line.quantity}</td><td>{money(line.unit_price)}</td>{config.show_discount!==false&&<td>{line.discount_percent}%<small>{money(line.discount_amount)}</small></td>}{config.show_tax!==false&&<td>{line.tax_rate}%<small>{money(line.tax_amount)}</small></td>}<td>{money(line.total_amount)}</td></tr>)}</tbody>
      </table>
      <section className="sales-print-totals">
        <div><span>{label("الصافي","Net")}</span><strong>{money(net)}</strong></div>
        {config.show_discount === false ? null : <div><span>{label("إجمالي الخصم","Discount total")}</span><strong>{money(discount)}</strong></div>}
        {config.show_tax === false ? null : <div><span>{label("الضريبة","Tax")}</span><strong>{money(tax)}</strong></div>}
        <div className="grand"><span>{label("الإجمالي النهائي","Grand total")}</span><strong>{money(total)}</strong></div>
      </section>
      {doc.notes&&<section className="sales-print-notes"><strong>{label("ملاحظات","Notes")}</strong><p>{doc.notes}</p></section>}
      {config.show_bank_details&&<p className="sales-print-bank">{label("بيانات التحويل البنكي: وفق بيانات الشركة المعتمدة.","Bank details: refer to the company's approved banking information.")}</p>}
      <footer>{config.footer_text||" "}</footer>
    </article>
  </div>;
}
