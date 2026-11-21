import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, hrApi, type Branch, type Currency, type HrEmployee, type HrPayrollCycle, type HrPayrollResult } from "./api";

const statusLabels: Record<string, string> = {
  active: "نشط", leave: "إجازة", unpaid_leave: "إجازة بدون راتب",
  suspended: "موقوف", resigned: "مستقيل", retired: "متقاعد",
};
const cycleLabels: Record<string, string> = {
  draft: "مسودة", calculated: "تم الاحتساب", review: "تحت المراجعة",
  approved: "معتمدة", posted: "مرحّلة", paid: "تم الصرف",
};

export default function HrPayrollPanel({ companyId, baseCurrencyId }: { companyId: number | null; baseCurrencyId?: number }) {
  const [tab, setTab] = useState<"employees" | "payroll">("employees");
  const [employees, setEmployees] = useState<HrEmployee[]>([]);
  const [cycles, setCycles] = useState<HrPayrollCycle[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [results, setResults] = useState<HrPayrollResult[]>([]);
  const [selectedCycle, setSelectedCycle] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [employeeDraft, setEmployeeDraft] = useState({code:"",full_name_ar:"",branch:"",currency:"",hire_date:new Date().toISOString().slice(0,10),basic_salary:"0"});
  const [cycleDraft, setCycleDraft] = useState({period_start:new Date(new Date().getFullYear(),new Date().getMonth(),1).toISOString().slice(0,10),period_end:new Date().toISOString().slice(0,10),payment_date:new Date().toISOString().slice(0,10)});

  const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [employeeRows, cycleRows, branchRows, currencyRows] = await Promise.all([
        hrApi.employees(companyId), hrApi.payrollCycles(companyId), api.branches(companyId), api.currencies(),
      ]);
      setEmployees(employeeRows); setCycles(cycleRows);
      setBranches(branchRows.filter(x=>x.is_active && x.company===companyId));
      setCurrencies(currencyRows.filter(x=>x.is_active));
      setEmployeeDraft(old=>({...old,branch:old.branch || String(branchRows.find(x=>x.is_active&&x.company===companyId)?.id||""),currency:old.currency||String(baseCurrencyId||currencyRows[0]?.id||"")}));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات الموارد البشرية."); }
    finally { setBusy(false); }
  };
  useEffect(()=>{ setEmployees([]);setCycles([]);setResults([]);setError("");setNotice(""); if(companyId) void load(); },[companyId]);

  const submitEmployee = async (event:FormEvent) => {
    event.preventDefault(); if(!companyId) return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createEmployee({...employeeDraft,company:companyId,branch:Number(employeeDraft.branch),currency:Number(employeeDraft.currency)});
      setEmployeeDraft(old=>({...old,code:"",full_name_ar:"",basic_salary:"0"}));
      setNotice("تم حفظ ملف الموظف."); await load();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر حفظ الموظف."); }
    finally { setBusy(false); }
  };
  const submitCycle = async (event:FormEvent) => {
    event.preventDefault();if(!companyId)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createPayrollCycle({...cycleDraft,company:companyId,rule_set:null});
      setNotice("تم إنشاء دورة المرتبات كمسودة دون ترحيل محاسبي.");await load();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء دورة المرتبات."); }
    finally { setBusy(false); }
  };
  const action = async (fn:()=>Promise<unknown>, success:string) => {
    setBusy(true);setError("");setNotice("");
    try { await fn();setNotice(success);await load(); }
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تنفيذ الإجراء.");}
    finally{setBusy(false);}
  };
  const showResults = async (cycleId:number) => {
    setBusy(true);setError("");setSelectedCycle(cycleId);
    try {setResults(await hrApi.payrollResults(cycleId));}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تحميل نتائج المرتبات.");}
    finally{setBusy(false);}
  };
  const totals = useMemo(()=>results.reduce((a,r)=>({
    net:a.net+Number(r.net_pay), cost:a.cost+Number(r.company_cost),
    employer:a.employer+Number(r.employer_share_total),
  }),{net:0,cost:0,employer:0}),[results]);
  const money=(v:string|number)=>Number(v||0).toLocaleString("ar-EG",{maximumFractionDigits:2});

  if(!companyId) return <section className="work-panel"><div className="empty-state">اختر الشركة لفتح الموارد البشرية والمرتبات.</div></section>;
  return <section className="work-panel hr-payroll-panel">
    <div className="work-panel-head"><div><h2>الموارد البشرية والمرتبات</h2><p>إدارة الموظفين وعناصر الأجر ودورات المرتبات — تشغيل هجين بإدخال يدوي واحتساب تلقائي</p></div><span className="coming-soon">مرحلة التأسيس</span></div>
    <div className="hr-tabs" role="tablist" style={{display:"flex",gap:8,margin:"18px 0"}}>
      <button type="button" className={tab==="employees"?"primary-button":"subtle-button"} onClick={()=>setTab("employees")}>ملفات الموظفين</button>
      <button type="button" className={tab==="payroll"?"primary-button":"subtle-button"} onClick={()=>setTab("payroll")}>دورات المرتبات</button>
      <button type="button" className="subtle-button" onClick={()=>void load()} disabled={busy}>تحديث البيانات</button>
    </div>
    {error&&<div className="error-banner">{error}</div>}{notice&&<div className="success-banner">{notice}</div>}
    {tab==="employees"&&<>
      <form className="journal-form" onSubmit={submitEmployee}>
        <label>كود الموظف<input required maxLength={30} value={employeeDraft.code} onChange={e=>setEmployeeDraft({...employeeDraft,code:e.target.value})}/></label>
        <label>اسم الموظف<input required value={employeeDraft.full_name_ar} onChange={e=>setEmployeeDraft({...employeeDraft,full_name_ar:e.target.value})}/></label>
        <label>الفرع<select required value={employeeDraft.branch} onChange={e=>setEmployeeDraft({...employeeDraft,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(x=><option key={x.id} value={x.id}>{x.name_ar} ({x.code})</option>)}</select></label>
        <label>العملة<select required value={employeeDraft.currency} onChange={e=>setEmployeeDraft({...employeeDraft,currency:e.target.value})}>{currencies.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
        <label>تاريخ التعيين<input required type="date" value={employeeDraft.hire_date} onChange={e=>setEmployeeDraft({...employeeDraft,hire_date:e.target.value})}/></label>
        <label>الراتب الأساسي<input required type="number" min="0" step="0.01" value={employeeDraft.basic_salary} onChange={e=>setEmployeeDraft({...employeeDraft,basic_salary:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!branches.length||!currencies.length}>{busy?"جارٍ الحفظ…":"حفظ ملف الموظف"}</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>سجل الموظفين</h2><p>البيانات الأساسية والحالة الوظيفية</p></div><span className="module-count">{employees.length} موظف</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم الموظف</th><th>الفرع</th><th>تاريخ التعيين</th><th>الأساسي</th><th>الحالة</th></tr></thead><tbody>
        {employees.map(e=><tr key={e.id}><td className="code-cell">{e.code}</td><td>{e.full_name_ar}</td><td>{e.branch_name}</td><td>{e.hire_date}</td><td>{money(e.basic_salary)} {e.currency_code}</td><td><span className="status-pill">{statusLabels[e.status]||e.status}</span></td></tr>)}
      </tbody></table>{!employees.length&&<div className="table-empty">{busy?"جارٍ التحميل…":"لا يوجد موظفون مسجلون."}</div>}</div>
    </>}
    {tab==="payroll"&&<>
      <form className="journal-form" onSubmit={submitCycle}>
        <label>بداية الفترة<input required type="date" value={cycleDraft.period_start} onChange={e=>setCycleDraft({...cycleDraft,period_start:e.target.value})}/></label>
        <label>نهاية الفترة<input required type="date" value={cycleDraft.period_end} onChange={e=>setCycleDraft({...cycleDraft,period_end:e.target.value})}/></label>
        <label>تاريخ الصرف<input required type="date" value={cycleDraft.payment_date} onChange={e=>setCycleDraft({...cycleDraft,payment_date:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy}>إنشاء دورة جديدة</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>دورات المرتبات</h2><p>لا يتم إنشاء قيود أو صرف أموال في هذه المرحلة قبل استكمال المراجعة المحاسبية</p></div><span className="module-count">{cycles.length} دورة</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الفترة</th><th>تاريخ الصرف</th><th>عدد الموظفين</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
        {cycles.map(c=><tr key={c.id}><td>{c.period_start} — {c.period_end}</td><td>{c.payment_date}</td><td>{c.results_count}</td><td><span className="status-pill">{cycleLabels[c.status]||c.status}</span></td><td style={{display:"flex",gap:6,flexWrap:"wrap"}}>
          {["draft","calculated","review"].includes(c.status)&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.calculatePayroll(c.id),"تم احتساب الدورة. راجع النتائج قبل إرسالها للاعتماد.")}>احتساب</button>}
          {c.status==="calculated"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.sendPayrollToReview(c.id),"تم إرسال الدورة للمراجعة.")}>إرسال للمراجعة</button>}
          {c.status==="review"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.approvePayroll(c.id),"تم اعتماد الدورة.")}>اعتماد</button>}
          <button type="button" className="subtle-button" disabled={busy} onClick={()=>void showResults(c.id)}>النتائج</button>
        </td></tr>)}
      </tbody></table>{!cycles.length&&<div className="table-empty">لا توجد دورات مرتبات بعد.</div>}</div>
      {selectedCycle&&<><div className="section-heading" style={{marginTop:24}}><div><h2>نتائج الدورة رقم {selectedCycle}</h2><p>حصة صاحب العمل ظاهرة في جانبي الكشف كتسوية عرضية لا تؤثر على صافي الموظف</p></div></div>
        <div className="metrics-grid report-metrics"><article className="metric-card"><div className="metric-top">صافي المستحقات</div><strong className="metric-value">{money(totals.net)}</strong></article><article className="metric-card"><div className="metric-top">حصة صاحب العمل</div><strong className="metric-value">{money(totals.employer)}</strong></article><article className="metric-card"><div className="metric-top">تكلفة الشركة</div><strong className="metric-value">{money(totals.cost)}</strong></article></div>
        <div className="data-table-wrap"><table><thead><tr><th>الموظف</th><th>الاستحقاقات الظاهرة</th><th>الاستقطاعات الظاهرة</th><th>صافي الموظف</th><th>تكلفة الشركة</th></tr></thead><tbody>{results.map(r=><tr key={r.id}><td>{r.employee_code} — {r.employee_name}</td><td>{money(r.displayed_earnings_total)}</td><td>{money(r.displayed_deductions_total)}</td><td><strong>{money(r.net_pay)}</strong></td><td>{money(r.company_cost)}</td></tr>)}</tbody></table>{!results.length&&<div className="table-empty">لا توجد نتائج لهذه الدورة.</div>}</div>
      </>}
    </>}
    <p className="asset-report-note">تنبيه: نسب الضرائب والتأمينات والترحيل المحاسبي والصرف البنكي لم تُفعّل بعد. لا تعتمد دورة فعلية قبل اكتمالها واختبارها.</p>
  </section>;
}
