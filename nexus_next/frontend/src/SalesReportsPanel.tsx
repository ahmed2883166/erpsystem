import { useState, type FormEvent } from "react";
import { salesApi, type SalesPerformanceReport } from "./api";
import { formatMoney } from "./formatters";

type Props = { companyId:number|null };
const today = () => {
  const value = new Date();
  return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;
};
const monthStart = () => {
  const value = new Date();
  return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-01`;
};
const money = (value:string) => formatMoney(value);

export default function SalesReportsPanel({companyId}:Props) {
  const [dateFrom,setDateFrom] = useState(monthStart);
  const [dateTo,setDateTo] = useState(today);
  const [report,setReport] = useState<SalesPerformanceReport|null>(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");

  const load = async (event:FormEvent) => {
    event.preventDefault();
    if(!companyId)return;
    setBusy(true);setError("");
    try { setReport(await salesApi.salesPerformance(companyId,dateFrom,dateTo)); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحميل تقرير المبيعات."); }
    finally { setBusy(false); }
  };

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>تقارير أداء المبيعات</h2><p>مقارنة المبيعات والمرتجعات وصافي المبيعات وتكلفة المبيعات والربح حسب العميل والصنف.</p></div></div>
    {!companyId&&<div className="table-empty">اختر شركة لعرض التقرير.</div>}
    {companyId&&<form className="inventory-form" onSubmit={load}>
      <label>من تاريخ<input type="date" required value={dateFrom} onChange={event=>setDateFrom(event.target.value)}/></label>
      <label>إلى تاريخ<input type="date" required min={dateFrom} value={dateTo} onChange={event=>setDateTo(event.target.value)}/></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy?"جارٍ إعداد التقرير…":"عرض التقرير"}</button></div>
    </form>}
    {error&&<div className="error-banner">{error}</div>}
    {report&&<>
      <div className="stats-grid">
        <div className="stat-card"><small>صافي المبيعات قبل المرتجعات</small><strong>{money(report.totals.sales_net)}</strong></div>
        <div className="stat-card"><small>مردودات المبيعات</small><strong>{money(report.totals.return_net)}</strong></div>
        <div className="stat-card"><small>صافي المبيعات بعد المرتجعات</small><strong>{money(report.totals.net_sales)}</strong></div>
        <div className="stat-card"><small>تكلفة المبيعات الصافية</small><strong>{money(report.totals.net_cost)}</strong></div>
        <div className="stat-card"><small>مجمل الربح</small><strong>{money(report.totals.gross_profit)}</strong></div>
      </div>
      <p className="muted">الفترة: {report.date_from} إلى {report.date_to} · الفواتير: {report.totals.invoice_count} · المرتجعات: {report.totals.return_count} · ضريبة المبيعات: {money(report.totals.sales_tax)} · ضريبة المرتجعات: {money(report.totals.return_tax)}</p>
      <h3>تحليل المبيعات حسب العميل</h3>
      <div className="data-table-wrap"><table><thead><tr><th>العميل</th><th>عدد الفواتير</th><th>صافي المبيعات</th><th>المرتجعات</th><th>الصافي بعد المرتجعات</th><th>مجمل الربح</th></tr></thead><tbody>
        {report.by_customer.map(row=><tr key={row.customer}><td>{row.customer_name}</td><td>{row.invoice_count}</td><td>{money(row.sales_net)}</td><td>{money(row.return_net)}</td><td>{money(row.net_sales)}</td><td>{money(row.gross_profit)}</td></tr>)}
      </tbody></table>{report.by_customer.length===0&&<div className="table-empty">لا توجد مبيعات أو مرتجعات في الفترة المحددة.</div>}</div>
      <h3>تحليل المبيعات حسب الصنف</h3>
      <div className="data-table-wrap"><table><thead><tr><th>كود الصنف</th><th>الصنف</th><th>الكمية المباعة</th><th>الكمية المرتجعة</th><th>صافي المبيعات</th><th>المرتجعات</th><th>مجمل الربح</th></tr></thead><tbody>
        {report.by_product.map(row=><tr key={row.product}><td>{row.product_code}</td><td>{row.product_name}</td><td>{row.quantity_sold}</td><td>{row.quantity_returned}</td><td>{money(row.sales_net)}</td><td>{money(row.return_net)}</td><td>{money(row.gross_profit)}</td></tr>)}
      </tbody></table>{report.by_product.length===0&&<div className="table-empty">لا توجد بنود مبيعات في الفترة المحددة.</div>}</div>
    </>}
  </section>;
}
