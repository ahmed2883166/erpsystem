import { useEffect, useMemo, useState } from "react";
import { api, type Branch, type CostCenter, type ExpenseAnalysisReport, type GeneralLedgerReport } from "./api";

function localDate(date: Date) {
  const y=date.getFullYear(), m=String(date.getMonth()+1).padStart(2,"0"), d=String(date.getDate()).padStart(2,"0");
  return `${y}-${m}-${d}`;
}
function downloadCsv(filename:string, rows:(string|number)[][]) {
  const quote=(value:string|number)=>'"'+String(value??"").replace(/"/g,'""')+'"';
  const csv="\uFEFF"+rows.map(row=>row.map(quote).join(";")).join("\r\n");
  const url=URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"}));
  const a=document.createElement("a"); a.href=url; a.download=filename; a.click(); URL.revokeObjectURL(url);
}
const money=(value:string|number)=>Number(value||0).toLocaleString("ar-EG",{minimumFractionDigits:2,maximumFractionDigits:2});

export default function ExpenseAnalysisPanel({companyId,branches}:{companyId:number|null;branches:Branch[]}) {
  const today=localDate(new Date());
  const [from,setFrom]=useState(`${today.slice(0,4)}-01-01`);
  const [to,setTo]=useState(today);
  const [branch,setBranch]=useState("");
  const [costCenter,setCostCenter]=useState("");
  const [centers,setCenters]=useState<CostCenter[]>([]);
  const [report,setReport]=useState<ExpenseAnalysisReport|null>(null);
  const [ledger,setLedger]=useState<GeneralLedgerReport|null>(null);
  const [ledgerBusy,setLedgerBusy]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [tableSearch,setTableSearch]=useState("");
  const [ledgerSearch,setLedgerSearch]=useState("");
  useEffect(()=>{ if(!companyId){setCenters([]);return;} api.costCenters(companyId).then(setCenters).catch(()=>setCenters([])); },[companyId]);
  const load=async()=>{
    if(!companyId)return;
    setBusy(true);setError("");
    try{setReport(await api.expenseAnalysis(companyId,from,to,branch?Number(branch):undefined,costCenter?Number(costCenter):undefined));}
    catch(e){setReport(null);setError(e instanceof Error?e.message:"تعذر تحميل تحليل المصروفات.");}
    finally{setBusy(false);}
  };
  useEffect(()=>{if(companyId)void load();},[companyId]);
  const openLedger=async(accountId:number)=>{if(!companyId)return;setLedgerBusy(true);try{setLedger(await api.generalLedger(companyId,accountId,from,to,branch?Number(branch):undefined));}catch(e){setError(e instanceof Error?e.message:"تعذر فتح دفتر الأستاذ للحساب.");}finally{setLedgerBusy(false);}};
  const maxMonth=Math.max(1,...(report?.monthly_rows||[]).map(x=>Number(x.amount)));
  const visibleExpenseRows=(report?.rows||[]).filter(x=>`${x.code} ${x.name_ar} ${x.branch_name} ${x.cost_center_name} ${x.category}`.toLocaleLowerCase("ar").includes(tableSearch.trim().toLocaleLowerCase("ar")));
  const visibleLedgerRows=(ledger?.rows||[]).filter(row=>`${row.date} ${row.entry_number} ${row.description} ${row.branch}`.toLocaleLowerCase("ar").includes(ledgerSearch.trim().toLocaleLowerCase("ar")));
  const exportRows=useMemo(()=>[
    ["كود الحساب","الحساب","الفرع","مركز التكلفة","التصنيف","مدين","دائن","صافي المصروف"],
    ...(report?.rows||[]).map(x=>[x.code,x.name_ar,x.branch_name,x.cost_center_name,x.category==="direct_cost"?"تكلفة مبيعات مباشرة":"مصروف تشغيلي / إداري",x.debit,x.credit,x.amount] as (string|number)[])
  ],[report]);
  return <section className="work-panel expense-analysis-panel">
    <div className="work-panel-head"><div><h2>تحليل المصروفات</h2><p>تحليل القيود المرحلة حسب الحساب والفرع ومركز التكلفة مع مقارنة شهرية</p></div>
      <div className="report-actions">
        <button type="button" className="subtle-button" disabled={!report} onClick={()=>report&&downloadCsv(`expense-analysis-${from}-${to}.csv`,exportRows)}>تصدير Excel (CSV)</button>
        <button type="button" className="subtle-button" disabled={!report} onClick={(event)=>{const target=event.currentTarget.closest(".expense-analysis-panel");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة / PDF</button>
        <button type="button" className="primary-button" disabled={busy||!companyId} onClick={()=>void load()}>{busy?"جارٍ التحميل…":"تحديث التقرير"}</button>
      </div>
    </div>
    <div className="asset-report-filters">
      <label>من تاريخ<input type="date" value={from} max={to} onChange={e=>setFrom(e.target.value)}/></label>
      <label>إلى تاريخ<input type="date" value={to} min={from} onChange={e=>setTo(e.target.value)}/></label>
      <label>الفرع<select value={branch} onChange={e=>setBranch(e.target.value)}><option value="">كل الفروع</option>{branches.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
      <label>مركز التكلفة<select value={costCenter} onChange={e=>setCostCenter(e.target.value)}><option value="">كل مراكز التكلفة</option>{centers.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
    </div>
    {error&&<div className="error-banner">{error}</div>}
    {report&&<>
      <div className="metrics-grid report-metrics">
        <article className="metric-card"><div className="metric-top">إجمالي المصروفات</div><strong className="metric-value">{money(report.total)} {report.currency}</strong></article>
        <article className="metric-card"><div className="metric-top">تكلفة المبيعات المباشرة</div><strong className="metric-value">{money(report.direct_total)} {report.currency}</strong></article>
        <article className="metric-card"><div className="metric-top">المصروفات التشغيلية والإدارية</div><strong className="metric-value">{money(report.operating_total)} {report.currency}</strong></article>
      </div>
      <h3>الاتجاه الشهري</h3>
      <div className="expense-month-chart">{report.monthly_rows.map(x=><div className="expense-month" key={x.month}><div className="expense-month-track"><span style={{height:`${Math.max(2,Math.abs(Number(x.amount))/maxMonth*100)}%`}} /></div><b>{money(x.amount)}</b><small>{x.month}</small></div>)}</div>
      <h3>تفصيل المصروفات حسب الحساب والفرع ومركز التكلفة</h3>
      <div className="table-toolbar"><input aria-label="البحث في تحليل المصروفات" placeholder="ابحث بكود الحساب أو الاسم أو الفرع…" value={tableSearch} onChange={e=>setTableSearch(e.target.value)}/><span className="module-count">{visibleExpenseRows.length} من {report.rows.length} بند</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>كود الحساب</th><th>الحساب</th><th>الفرع</th><th>مركز التكلفة</th><th>التصنيف</th><th>مدين</th><th>دائن</th><th>الصافي</th><th>التفاصيل</th></tr></thead>
        <tbody>{visibleExpenseRows.map((x,i)=><tr key={`${x.account_id}-${x.branch_id}-${x.cost_center_id}-${i}`}><td className="code-cell">{x.code}</td><td>{x.name_ar}</td><td>{x.branch_name}</td><td>{x.cost_center_name}</td><td>{x.category==="direct_cost"?"تكلفة مباشرة":"تشغيلي / إداري"}</td><td>{money(x.debit)}</td><td>{money(x.credit)}</td><td>{money(x.amount)}</td><td><button type="button" className="text-action-button" disabled={ledgerBusy} onClick={()=>void openLedger(x.account_id)}>{ledgerBusy?"جارٍ التحميل…":"دفتر الأستاذ ←"}</button></td></tr>)}</tbody>
        <tfoot><tr><th colSpan={8}>الإجمالي</th><th>{money(report.total)}</th></tr></tfoot>
      </table>{!report.rows.length&&<div className="table-empty">لا توجد مصروفات مرحلة خلال الفترة المحددة.</div>}{report.rows.length>0&&!visibleExpenseRows.length&&<div className="table-empty">لا توجد بنود مطابقة للبحث.</div>}</div>
      {ledger&&<section className="report-drilldown">
        <div className="work-panel-head"><div><h3>دفتر الأستاذ — {ledger.account_code} · {ledger.account_name}</h3><p>حركة الحساب خلال نفس الفترة المحددة</p></div><button type="button" className="subtle-button" onClick={()=>setLedger(null)}>إغلاق التفاصيل</button></div>
        <div className="asset-cashflow-totals"><span>رصيد أول المدة: {money(ledger.opening_balance)}</span><b>رصيد آخر المدة: {money(ledger.closing_balance)}</b></div>
        <div className="table-toolbar"><input aria-label="البحث في دفتر الأستاذ" placeholder="ابحث برقم القيد أو البيان أو التاريخ…" value={ledgerSearch} onChange={e=>setLedgerSearch(e.target.value)}/><span className="module-count">{visibleLedgerRows.length} من {ledger.rows.length} حركة</span></div>
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>رقم القيد</th><th>البيان</th><th>الفرع</th><th>مدين</th><th>دائن</th><th>الرصيد</th></tr></thead><tbody>
          {visibleLedgerRows.map(row=><tr key={row.line_id}><td>{row.date}</td><td>{row.entry_number}</td><td>{row.description}</td><td>{row.branch}</td><td>{money(row.debit)}</td><td>{money(row.credit)}</td><td>{money(row.running_balance)}</td></tr>)}
        </tbody></table>{ledger.rows.length>0&&!visibleLedgerRows.length&&<div className="table-empty">لا توجد حركات مطابقة للبحث.</div>}</div>
      </section>}
      <p className="asset-report-note">{report.note}</p>
    </>}
    {!report&&!error&&<div className="table-empty">{busy?"جارٍ تحميل التقرير…":"حدد الفترة ثم حدّث التقرير."}</div>}
  </section>;
}
