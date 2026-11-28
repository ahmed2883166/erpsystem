/*
 * الموارد البشرية والمرتبات: ملفات الموظفين والحضور والإجازات والسلف ومكونات الأجر.
 * تدير دورات المرتبات والاحتساب والمراجعة والاعتماد والصرف وسجل تغييرات الراتب.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { formatMoney } from "./formatters";
import CustomExcelExportButton from "./CustomExcelExportButton";
import { api, hrApi, treasuryApi, type Branch, type Currency, type HrEmployee, type HrPayrollCycle, type HrPayrollResult, type HrPayrollComponent, type HrAttendance, type HrEmployeeComponent, type HrLeaveRequest, type HrEmployeeLoan, type HrPayrollRuleSet, type Account, type TreasuryAccount, type HrEmployeePayReport, type HrSalaryHistory } from "./api";

const statusLabels: Record<string, string> = {
  active: "نشط", leave: "إجازة", unpaid_leave: "إجازة بدون راتب",
  suspended: "موقوف", resigned: "مستقيل", retired: "متقاعد",
};
const cycleLabels: Record<string, string> = {
  draft: "مسودة", calculated: "تم الاحتساب", review: "تحت المراجعة",
  approved: "معتمدة", posted: "مرحّلة", payment_pending: "بانتظار اعتماد الصرف", paid: "تم الصرف",
};

// الشاشة الجامعة للموارد البشرية والمرتبات وإدارة تبويبات الموظفين والحضور والدورات.
export default function HrPayrollPanel({ companyId, baseCurrencyId, country }: { companyId: number | null; baseCurrencyId?: number; country?: "EG" | "SA" }) {
  const [tab, setTab] = useState<"employees" | "components" | "attendance" | "assignments" | "leaveLoans" | "rules" | "payroll">("employees");
  const [employees, setEmployees] = useState<HrEmployee[]>([]);
  const [cycles, setCycles] = useState<HrPayrollCycle[]>([]);
  const [ruleSets, setRuleSets] = useState<HrPayrollRuleSet[]>([]);
  const [components, setComponents] = useState<HrPayrollComponent[]>([]);
  const [attendance, setAttendance] = useState<HrAttendance[]>([]);
  const [employeeComponents, setEmployeeComponents] = useState<HrEmployeeComponent[]>([]);
  const [leaveRequests, setLeaveRequests] = useState<HrLeaveRequest[]>([]);
  const [loans, setLoans] = useState<HrEmployeeLoan[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [paymentAccountId, setPaymentAccountId] = useState("");
  const [loanDisbursementAccount, setLoanDisbursementAccount] = useState("");
  const [results, setResults] = useState<HrPayrollResult[]>([]);
  const [payReport, setPayReport] = useState<HrEmployeePayReport | null>(null);
  const [salaryHistoryRows, setSalaryHistoryRows] = useState<HrSalaryHistory[]>([]);
  const [payReportFrom, setPayReportFrom] = useState(new Date(new Date().getFullYear(),0,1).toISOString().slice(0,10));
  const [payReportTo, setPayReportTo] = useState(new Date().toISOString().slice(0,10));
  const [selectedCycle, setSelectedCycle] = useState<number | null>(null);
  const [selectedPayslip, setSelectedPayslip] = useState<HrPayrollResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [employeeSearch,setEmployeeSearch]=useState("");
  const [employeeDraft, setEmployeeDraft] = useState({code:"",full_name_ar:"",branch:"",currency:"",hire_date:new Date().toISOString().slice(0,10),basic_salary:"0"});
  const [componentDraft, setComponentDraft] = useState({code:"",name_ar:"",kind:"earning" as HrPayrollComponent["kind"],method:"fixed" as HrPayrollComponent["method"],default_value:"0",expense_account:"",payable_account:"",affects_taxable_base:false,affects_insurance_base:false,requires_approval:false});
  const [attendanceDraft, setAttendanceDraft] = useState({employee:"",work_date:new Date().toISOString().slice(0,10),status:"present" as HrAttendance["status"],worked_hours:"8",overtime_hours:"0",friday_worked:false,late_minutes:"0"});
  const [attendanceMonth, setAttendanceMonth] = useState(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}`;
  });
  const attendanceDays = useMemo(() => {
    const [year, month] = attendanceMonth.split("-").map(Number);
    if (!year || !month) return [];
    const count = new Date(year, month, 0).getDate();
    return Array.from({length: count}, (_, index) => {
      const day = index + 1;
      return `${year}-${String(month).padStart(2,"0")}-${String(day).padStart(2,"0")}`;
    });
  }, [attendanceMonth]);
  const [assignmentDraft, setAssignmentDraft] = useState({employee:"",component:"",value:"",effective_from:new Date().toISOString().slice(0,10),effective_to:""});
  const [leaveDraft, setLeaveDraft] = useState({employee:"",kind:"annual" as HrLeaveRequest["kind"],start_date:new Date().toISOString().slice(0,10),end_date:new Date().toISOString().slice(0,10),days:"1",reason:""});
  const [loanDraft, setLoanDraft] = useState({employee:"",reference:"",issue_date:new Date().toISOString().slice(0,10),first_due_date:new Date().toISOString().slice(0,10),receivable_account:"",principal:"0",installment_count:"1",installment_amount:"0",notes:""});
  const [salaryDraft, setSalaryDraft] = useState({employee:"",effective_date:new Date().toISOString().slice(0,10),raise_mode:"percentage" as "percentage"|"amount",raise_value:"0",change_type:"annual_raise",reason:""});
  const [ruleSetDraft, setRuleSetDraft] = useState({name:"",effective_from:new Date().toISOString().slice(0,10),effective_to:"",monthly_daily_divisor:"30",min_base:"",max_base:"",employee_rate:"",employer_rate:"",annual_personal_exemption:"",annual_brackets:"[]"});
  const [cycleDraft, setCycleDraft] = useState({rule_set:"",period_start:new Date(new Date().getFullYear(),new Date().getMonth(),1).toISOString().slice(0,10),period_end:new Date().toISOString().slice(0,10),payment_date:new Date().toISOString().slice(0,10)});

  // تحميل بيانات الموارد البشرية اللازمة للشركة: الموظفون والإعدادات والدورات والتقارير.
const load = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [employeeRows, cycleRows, branchRows, currencyRows, componentRows, attendanceRows, assignmentRows, leaveRows, loanRows, ruleRows, accountRows, treasuryRows] = await Promise.all([
        hrApi.employees(companyId), hrApi.payrollCycles(companyId), api.branches(companyId), api.currencies(), hrApi.payrollComponents(companyId), hrApi.attendance(companyId), hrApi.employeeComponents(companyId), hrApi.leaveRequests(companyId), hrApi.employeeLoans(companyId), hrApi.payrollRuleSets(companyId), api.accounts(companyId), treasuryApi.accounts(companyId),
      ]);
      setEmployees(employeeRows); setCycles(cycleRows); setComponents(componentRows); setAttendance(attendanceRows); setRuleSets(ruleRows);
      setEmployeeComponents(assignmentRows); setLeaveRequests(leaveRows); setLoans(loanRows); setAccounts(accountRows); setTreasuryAccounts(treasuryRows.filter(x=>x.is_active));
      setLoanDisbursementAccount(String(treasuryRows.find(x=>x.is_active)?.id || ""));
      setBranches(branchRows.filter(x=>x.is_active && x.company===companyId));
      setCurrencies(currencyRows.filter(x=>x.is_active));
      setEmployeeDraft(old=>({...old,branch:old.branch || String(branchRows.find(x=>x.is_active&&x.company===companyId)?.id||""),currency:String(baseCurrencyId||"")}));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات الموارد البشرية."); }
    finally { setBusy(false); }
  };
  useEffect(()=>{ setEmployees([]);setCycles([]);setResults([]);setPayReport(null);setSalaryHistoryRows([]);setError("");setNotice(""); if(companyId) void load(); },[companyId]);
  useEffect(()=>{
    if(!salaryDraft.employee){setSalaryHistoryRows([]);return;}
    let active=true;
    hrApi.salaryHistory(Number(salaryDraft.employee))
      .then(rows=>{if(active)setSalaryHistoryRows(rows);})
      .catch(reason=>{if(active)setError(reason instanceof Error?reason.message:"تعذر تحميل سجل تغييرات الراتب.");});
    return ()=>{active=false;};
  },[salaryDraft.employee,companyId]);

  // حفظ بيانات الموظف الجديدة أو تعديلات ملفه.
const submitEmployee = async (event:FormEvent) => {
    event.preventDefault(); if(!companyId) return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createEmployee({...employeeDraft,company:companyId,branch:Number(employeeDraft.branch),currency:Number(baseCurrencyId||employeeDraft.currency)});
      setEmployeeDraft(old=>({...old,code:"",full_name_ar:"",basic_salary:"0"}));
      setNotice("تم حفظ ملف الموظف."); await load();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر حفظ الموظف."); }
    finally { setBusy(false); }
  };
  // تسجيل عنصر استحقاق أو خصم يمكن ربطه بالموظف أو المرتبات.
const submitComponent = async (event:FormEvent) => {
    event.preventDefault();if(!companyId)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createPayrollComponent({...componentDraft,company:companyId,default_value:componentDraft.default_value,expense_account:componentDraft.expense_account?Number(componentDraft.expense_account):null,payable_account:componentDraft.payable_account?Number(componentDraft.payable_account):null,is_active:true});
      setComponentDraft({code:"",name_ar:"",kind:"earning",method:"fixed",default_value:"0",expense_account:"",payable_account:"",affects_taxable_base:false,affects_insurance_base:false,requires_approval:false});
      setNotice("تم حفظ عنصر المرتب وربط حساباته وإعداداته.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر حفظ عنصر المرتب.");}
    finally{setBusy(false);}
  };
  // تحديث حالة حضور موظف في يوم محدد ثم مزامنة بيانات الحضور.
const updateAttendanceCell = async (employeeId:number, workDate:string, nextStatus:HrAttendance["status"]) => {
    const existing = attendance.find(item => item.employee === employeeId && item.work_date === workDate);
    setBusy(true); setError(""); setNotice("");
    try {
      if (existing) {
        await hrApi.updateAttendance(existing.id, {status: nextStatus});
      } else {
        await hrApi.createAttendance({
          employee: employeeId, work_date: workDate, status: nextStatus,
          worked_hours: nextStatus === "present" ? "8" : "0",
          overtime_hours: "0", friday_worked: false, late_minutes: 0,
        });
      }
      setNotice("تم تحديث سجل الحضور.");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحديث سجل الحضور.");
    } finally { setBusy(false); }
  };

  // حفظ إدخال الحضور المجمع للفترة المحددة.
const submitAttendance = async (event:FormEvent) => {
    event.preventDefault();if(!attendanceDraft.employee)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createAttendance({...attendanceDraft,employee:Number(attendanceDraft.employee),worked_hours:attendanceDraft.worked_hours,overtime_hours:attendanceDraft.overtime_hours,late_minutes:Number(attendanceDraft.late_minutes)});
      setNotice("تم تسجيل الحضور.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر تسجيل الحضور.");}
    finally{setBusy(false);}
  };
  // حفظ ربط مكونات الأجر بالموظف وتواريخ سريانها.
const submitAssignment = async (event:FormEvent) => {
    event.preventDefault();if(!assignmentDraft.employee||!assignmentDraft.component)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createEmployeeComponent({employee:Number(assignmentDraft.employee),component:Number(assignmentDraft.component),value:assignmentDraft.value===""?null:assignmentDraft.value,effective_from:assignmentDraft.effective_from,effective_to:assignmentDraft.effective_to||null,is_active:true});
      setNotice("تم ربط عنصر المرتب بالموظف بتاريخ السريان المحدد.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر ربط عنصر المرتب.");}
    finally{setBusy(false);}
  };
  // تسجيل طلب إجازة الموظف وإرسال بياناته للتحقق والحفظ.
const submitLeave = async (event:FormEvent) => {
    event.preventDefault();if(!leaveDraft.employee)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createLeaveRequest({...leaveDraft,employee:Number(leaveDraft.employee)});
      setNotice("تم تسجيل طلب الإجازة في انتظار الاعتماد.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر تسجيل الإجازة.");}
    finally{setBusy(false);}
  };
  // تسجيل سلفة الموظف وجدول أو بيانات استردادها.
const submitLoan = async (event:FormEvent) => {
    event.preventDefault();if(!loanDraft.employee)return;
    const count=Number(loanDraft.installment_count), principal=Number(loanDraft.principal), regular=Number(loanDraft.installment_amount);
    if(count<1||regular<=0||regular*(count-1)>=principal){setError("قيمة الأقساط غير متوافقة مع أصل السلفة.");return;}
    setBusy(true);setError("");setNotice("");
    try {
      const first=new Date(loanDraft.first_due_date+"T12:00:00");
      const installments=Array.from({length:count},(_,i)=>{
        const due=new Date(first);const day=first.getDate();due.setDate(1);due.setMonth(first.getMonth()+i);
        const lastDay=new Date(due.getFullYear(),due.getMonth()+1,0).getDate();due.setDate(Math.min(day,lastDay));
        const amount=i===count-1?principal-regular*(count-1):regular;
        return {due_date:due.toISOString().slice(0,10),amount:amount.toFixed(4)};
      });
      await hrApi.createEmployeeLoanWithSchedule({...loanDraft,employee:Number(loanDraft.employee),receivable_account:Number(loanDraft.receivable_account),principal:principal.toFixed(4),installment_count:count,installment_amount:regular.toFixed(4),installments});
      setNotice("تم تسجيل السلفة وإنشاء جدول أقساطها دفعة واحدة.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر تسجيل السلفة أو جدول أقساطها.");}
    finally{setBusy(false);}
  };
  const disburseLoan = async (loanId:number) => {
    const loan=loans.find(row=>row.id===loanId);
    if(!loan)return;
    if(!loan.disbursement_transaction && !loanDisbursementAccount){setError("اختر الخزينة أو البنك الذي سيتم صرف السلفة منه.");return;}
    setBusy(true);setError("");setNotice("");
    try {
      const result=await hrApi.disburseEmployeeLoan(loanId,Number(loanDisbursementAccount || 0));
      setNotice(result.approval_pending ? (result.detail || "تم تجهيز مستند الصرف وينتظر الاعتماد المالي.") : "تم صرف السلفة وترحيل قيدها المحاسبي.");
      await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر صرف السلفة.");}
    finally{setBusy(false);}
  };
  const selectedSalaryEmployee=employees.find(employee=>employee.id===Number(salaryDraft.employee));
  const salaryBeforeEffectiveDate=(()=>{
    if(!selectedSalaryEmployee)return 0;
    const prior=salaryHistoryRows
      .filter(row=>row.effective_date<salaryDraft.effective_date)
      .sort((a,b)=>a.effective_date.localeCompare(b.effective_date)||a.id-b.id);
    if(prior.length)return Number(prior[prior.length-1].new_salary);
    const first=[...salaryHistoryRows].sort((a,b)=>a.effective_date.localeCompare(b.effective_date)||a.id-b.id)[0];
    return first?Number(first.previous_salary):Number(selectedSalaryEmployee.basic_salary);
  })();
  const salaryIncrease=Number(salaryDraft.raise_value||0);
  const calculatedNewSalary=salaryDraft.raise_mode==="percentage"
    ? salaryBeforeEffectiveDate*(1+salaryIncrease/100)
    : salaryBeforeEffectiveDate+salaryIncrease;
  // تسجيل تغيير الراتب مع تاريخ السريان وحفظ سجل تاريخ الأجر.
const submitSalaryChange = async (event:FormEvent) => {
    event.preventDefault();if(!salaryDraft.employee)return;
    if(!Number.isFinite(calculatedNewSalary)||calculatedNewSalary<0||salaryIncrease<0){
      setError("قيمة الزيادة أو الراتب الناتج غير صحيحة.");return;
    }
    if(salaryHistoryRows.some(row=>row.effective_date===salaryDraft.effective_date)){
      setError("يوجد تغيير راتب مسجل بالفعل في تاريخ السريان نفسه لهذا الموظف.");return;
    }
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createSalaryChange({
        employee:Number(salaryDraft.employee),effective_date:salaryDraft.effective_date,
        new_salary:calculatedNewSalary.toFixed(4),change_type:salaryDraft.change_type,reason:salaryDraft.reason,
      });
      setNotice("تم تسجيل الزيادة مع تاريخ السريان، وحُفظ الراتب السابق والجديد في سجل غير قابل للتعديل."); 
      setSalaryDraft(old=>({...old,raise_value:"0",reason:""}));
      setSalaryHistoryRows(await hrApi.salaryHistory(Number(salaryDraft.employee)));
      await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر تسجيل تغيير الراتب.");}
    finally{setBusy(false);}
  };
  // حفظ مجموعة قواعد المرتبات والتأمينات والإعدادات السنوية.
const submitRuleSet = async (event:FormEvent) => {
    event.preventDefault();if(!companyId||!country)return;
    setBusy(true);setError("");setNotice("");
    try {
      const brackets=JSON.parse(ruleSetDraft.annual_brackets);
      if(!Array.isArray(brackets)) throw new Error("شرائح الضريبة يجب أن تكون مصفوفة JSON.");
      const configuration={
        monthly_daily_divisor:Number(ruleSetDraft.monthly_daily_divisor),
        insurance:{min_base:Number(ruleSetDraft.min_base),max_base:Number(ruleSetDraft.max_base),employee_rate:Number(ruleSetDraft.employee_rate),employer_rate:Number(ruleSetDraft.employer_rate)},
        tax:{annual_personal_exemption:Number(ruleSetDraft.annual_personal_exemption),annual_brackets:brackets},
      };
      await hrApi.createPayrollRuleSet({company:companyId,country,name:ruleSetDraft.name,effective_from:ruleSetDraft.effective_from,effective_to:ruleSetDraft.effective_to||null,configuration,is_active:true});
      setNotice("تم حفظ مجموعة القواعد بتاريخ سريان محدد. راجع القيم القانونية قبل استخدامها.");await load();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر حفظ مجموعة القواعد.");}
    finally{setBusy(false);}
  };
  // إنشاء أو حفظ دورة المرتبات للفترة المختارة.
const submitCycle = async (event:FormEvent) => {
    event.preventDefault();if(!companyId)return;
    setBusy(true);setError("");setNotice("");
    try {
      await hrApi.createPayrollCycle({...cycleDraft,company:companyId,rule_set:cycleDraft.rule_set?Number(cycleDraft.rule_set):null});
      setNotice("تم إنشاء دورة المرتبات كمسودة دون ترحيل محاسبي.");await load();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء دورة المرتبات."); }
    finally { setBusy(false); }
  };
  // غلاف موحد لتنفيذ إجراءات الدورة وإظهار رسالة النجاح أو الخطأ وتحديث البيانات.
const action = async (fn:()=>Promise<unknown>, success:string) => {
    setBusy(true);setError("");setNotice("");
    try { await fn();setNotice(success);await load(); }
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تنفيذ الإجراء.");}
    finally{setBusy(false);}
  };
  // تحميل تفاصيل نتائج احتساب دورة مرتبات بعينها.
const showResults = async (cycleId:number) => {
    setBusy(true);setError("");setSelectedCycle(cycleId);
    try {setResults(await hrApi.payrollResults(cycleId));}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر تحميل نتائج المرتبات.");}
    finally{setBusy(false);}
  };

  // تحميل تقرير إجمالي مستحقات الموظف وفق نطاق التاريخ والفلاتر.
const loadEmployeePayReport = async () => {
    if (!companyId) return;
    setBusy(true); setError(""); setPayReport(null);
    try {
      setPayReport(await hrApi.employeePayReport(companyId, payReportFrom, payReportTo));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل تقرير إجمالي مرتبات الموظفين.");
    } finally { setBusy(false); }
  };
  const reportColumns = useMemo(() => {
    const columns = new Map<string,{code:string;name:string;kind:string}>();
    for (const employee of payReport?.employees ?? []) {
      for (const component of employee.components) {
        const key = `${component.code}|${component.kind}`;
        if (!columns.has(key)) columns.set(key,{code:component.code,name:component.name,kind:component.kind});
      }
    }
    return [...columns.values()];
  },[payReport]);
  // تصدير تقرير مستحقات الموظفين إلى ملف قابل للتنزيل.
const exportEmployeePayReport = () => {
    if (!payReport) return;
    const rows = [
      ["كود الموظف","الموظف","الفرع","القسم","العملة","عدد الدورات",...reportColumns.map(column=>column.name),"الاستحقاقات","استقطاعات الموظف","حصة صاحب العمل","صافي المرتب","تكلفة الشركة"],
      ...payReport.employees.map(employee=>[
        employee.employee_code,employee.employee_name,employee.branch,employee.department,employee.currency,employee.cycle_count,
        ...reportColumns.map(column=>employee.components.filter(item=>item.code===column.code&&item.kind===column.kind).reduce((sum,item)=>sum+Number(item.amount),0)),
        employee.earnings,employee.employee_deductions,employee.employer_share,employee.net_pay,employee.company_cost
      ])
    ];
    if (payReport.totals) rows.push(["","الإجمالي","","","",...Array(reportColumns.length).fill(""),payReport.totals.earnings,payReport.totals.employee_deductions,payReport.totals.employer_share,payReport.totals.net_pay,payReport.totals.company_cost]);
    const csvCell = (value: unknown) => { const text = String(value ?? ""); const safe = typeof value === "string" && /^\s*[=+\-@]/.test(text) ? "'" + text : text; return '"' + safe.replace(/"/g, '""') + '"' ; };
    const csv = rows.map(row => row.map(csvCell).join(",")).join("\r\n");
    const blob = new Blob(["\uFEFF",csv],{type:"text/csv;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `employee-payroll-${payReport.date_from}-${payReport.date_to}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const totals = useMemo(()=>results.reduce((a,r)=>({
    net:a.net+Number(r.net_pay), cost:a.cost+Number(r.company_cost),
    employer:a.employer+Number(r.employer_share_total),
  }),{net:0,cost:0,employer:0}),[results]);
  const dynamicPayrollColumns=Array.from(new Map(results.flatMap(r=>r.lines.filter(line=>line.code!=="EMPLOYER_SHARE_DISPLAY"&&line.code!=="EMPLOYER_SHARE_OFFSET").map(line=>[line.code,{code:line.code,name:line.name,kind:line.kind}]))).values());
  const money=(v:string|number)=>formatMoney(v);

  if(!companyId) return <section className="work-panel"><div className="empty-state">اختر الشركة لفتح الموارد البشرية والمرتبات.</div></section>;
  return <section className="work-panel hr-payroll-panel">
    <div className="work-panel-head"><div><h2>الموارد البشرية والمرتبات</h2><p>إدارة الموظفين وعناصر الأجر ودورات المرتبات — تشغيل هجين بإدخال يدوي واحتساب تلقائي</p></div><div className="table-actions">{tab === "employees" && <CustomExcelExportButton resource="employees" companyId={companyId} />}<span className="coming-soon">مرحلة التأسيس</span></div></div>
    <div className="hr-tabs" role="tablist" style={{display:"flex",gap:8,margin:"18px 0"}}>
      <button type="button" className={tab==="employees"?"primary-button":"subtle-button"} onClick={()=>setTab("employees")}>ملفات الموظفين</button>
      <button type="button" className={tab==="components"?"primary-button":"subtle-button"} onClick={()=>setTab("components")}>عناصر المرتب</button>
      <button type="button" className={tab==="attendance"?"primary-button":"subtle-button"} onClick={()=>setTab("attendance")}>الحضور</button>
      <button type="button" className={tab==="assignments"?"primary-button":"subtle-button"} onClick={()=>setTab("assignments")}>عناصر الموظفين</button>
      <button type="button" className={tab==="leaveLoans"?"primary-button":"subtle-button"} onClick={()=>setTab("leaveLoans")}>الإجازات والسلف</button>
      <button type="button" className={tab==="rules"?"primary-button":"subtle-button"} onClick={()=>setTab("rules")}>القواعد القانونية</button>
      <button type="button" className={tab==="payroll"?"primary-button":"subtle-button"} onClick={()=>setTab("payroll")}>دورات المرتبات</button>
      <button type="button" className="subtle-button" onClick={()=>void load()} disabled={busy}>تحديث البيانات</button>
    </div>
    {error&&<div className="error-banner">{error}</div>}{notice&&<div className="success-banner">{notice}</div>}
    {tab==="employees"&&<>
      <form className="journal-form" onSubmit={submitEmployee}>
        <label>كود الموظف<input required maxLength={30} value={employeeDraft.code} onChange={e=>setEmployeeDraft({...employeeDraft,code:e.target.value})}/></label>
        <label>اسم الموظف<input required value={employeeDraft.full_name_ar} onChange={e=>setEmployeeDraft({...employeeDraft,full_name_ar:e.target.value})}/></label>
        <label>الفرع<select required value={employeeDraft.branch} onChange={e=>setEmployeeDraft({...employeeDraft,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(x=><option key={x.id} value={x.id}>{x.name_ar} ({x.code})</option>)}</select></label>
        <label>عملة الراتب (عملة الشركة الأساسية)<select required disabled value={String(baseCurrencyId||employeeDraft.currency)}>{currencies.filter(x=>x.id===Number(baseCurrencyId||employeeDraft.currency)).map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
        <label>تاريخ التعيين<input required type="date" value={employeeDraft.hire_date} onChange={e=>setEmployeeDraft({...employeeDraft,hire_date:e.target.value})}/></label>
        <label>الراتب الأساسي<input required type="number" min="0" step="0.01" value={employeeDraft.basic_salary} onChange={e=>setEmployeeDraft({...employeeDraft,basic_salary:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!branches.length||!currencies.length}>{busy?"جارٍ الحفظ…":"حفظ ملف الموظف"}</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>سجل الموظفين</h2><p>البيانات الأساسية والحالة الوظيفية</p></div><span className="module-count">{employees.length} موظف</span></div>
      <div className="table-toolbar"><input aria-label="البحث في سجل الموظفين" placeholder="ابحث بكود الموظف أو الاسم أو الفرع…" value={employeeSearch} onChange={e=>setEmployeeSearch(e.target.value)}/><span className="module-count">{employees.filter(e=>`${e.code} ${e.full_name_ar} ${e.branch_name}`.toLocaleLowerCase("ar").includes(employeeSearch.trim().toLocaleLowerCase("ar"))).length} من {employees.length} موظف</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم الموظف</th><th>الفرع</th><th>تاريخ التعيين</th><th>الأساسي</th><th>الحالة</th></tr></thead><tbody>
        {employees.filter(e=>`${e.code} ${e.full_name_ar} ${e.branch_name}`.toLocaleLowerCase("ar").includes(employeeSearch.trim().toLocaleLowerCase("ar"))).map(e=><tr key={e.id}><td className="code-cell">{e.code}</td><td>{e.full_name_ar}</td><td>{e.branch_name}</td><td>{e.hire_date}</td><td>{money(e.basic_salary)} {e.currency_code}</td><td><span className="status-pill">{statusLabels[e.status]||e.status}</span></td></tr>)}
      </tbody></table>{!employees.length&&<div className="table-empty">{busy?"جارٍ التحميل…":"لا يوجد موظفون مسجلون."}</div>}{employees.length>0&&!employees.some(e=>`${e.code} ${e.full_name_ar} ${e.branch_name}`.toLocaleLowerCase("ar").includes(employeeSearch.trim().toLocaleLowerCase("ar")))&&<div className="table-empty">لا يوجد موظفون مطابقون للبحث.</div>}</div>
    </>}
    {tab==="components"&&<>
      <form className="journal-form" onSubmit={submitComponent}>
        <label>كود العنصر<input required maxLength={32} value={componentDraft.code} onChange={e=>setComponentDraft({...componentDraft,code:e.target.value})}/></label>
        <label>اسم العنصر<input required value={componentDraft.name_ar} onChange={e=>setComponentDraft({...componentDraft,name_ar:e.target.value})}/></label>
        <label>نوع العنصر<select value={componentDraft.kind} onChange={e=>setComponentDraft({...componentDraft,kind:e.target.value as HrPayrollComponent["kind"]})}><option value="earning">استحقاق</option><option value="deduction">استقطاع موظف</option><option value="employer_share">حصة صاحب العمل</option></select></label>
        <label>طريقة الحساب<select value={componentDraft.method} onChange={e=>setComponentDraft({...componentDraft,method:e.target.value as HrPayrollComponent["method"]})}><option value="fixed">مبلغ ثابت</option><option value="percent_basic">نسبة من الأساسي</option><option value="per_day">حسب الأيام</option><option value="per_hour">حسب الساعات</option><option value="friday_double_day">عمل الجمعة بضعف اليومية</option><option value="employee_insurance">تأمينات الموظف حسب القواعد</option><option value="employer_insurance">تأمينات صاحب العمل حسب القواعد</option><option value="progressive_tax">ضريبة شرائح حسب القواعد</option><option value="manual">إدخال يدوي</option></select></label>
        <label>القيمة الافتراضية<input type="number" min="0" step="0.0001" value={componentDraft.default_value} onChange={e=>setComponentDraft({...componentDraft,default_value:e.target.value})}/></label>
        <label>حساب المصروف (للاستحقاق/حصة صاحب العمل)<select value={componentDraft.expense_account} onChange={e=>setComponentDraft({...componentDraft,expense_account:e.target.value})}><option value="">الحساب الافتراضي لمصروف المرتبات</option>{accounts.filter(a=>a.nature==="expense"&&a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} — {a.name_ar}</option>)}</select></label>
        <label>حساب الالتزام (للاستقطاع/حصة صاحب العمل)<select value={componentDraft.payable_account} onChange={e=>setComponentDraft({...componentDraft,payable_account:e.target.value})}><option value="">اختر حساب الالتزام</option>{accounts.filter(a=>a.nature==="liability"&&a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} — {a.name_ar}</option>)}</select></label>
        <label className="checkbox-filter"><input type="checkbox" checked={componentDraft.affects_taxable_base} onChange={e=>setComponentDraft({...componentDraft,affects_taxable_base:e.target.checked})}/> يدخل في وعاء الضريبة</label>
        <label className="checkbox-filter"><input type="checkbox" checked={componentDraft.affects_insurance_base} onChange={e=>setComponentDraft({...componentDraft,affects_insurance_base:e.target.checked})}/> يدخل في وعاء التأمينات</label>
        <label className="checkbox-filter"><input type="checkbox" checked={componentDraft.requires_approval} onChange={e=>setComponentDraft({...componentDraft,requires_approval:e.target.checked})}/> يتطلب اعتمادًا قبل الصرف</label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy}>حفظ عنصر المرتب</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>عناصر المرتب المعرفة</h2><p>استحقاقات واستقطاعات الموظف وحصة صاحب العمل</p></div><span className="module-count">{components.length} عنصر</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم العنصر</th><th>النوع</th><th>طريقة الحساب</th><th>القيمة الافتراضية</th></tr></thead><tbody>{components.map(x=><tr key={x.id}><td>{x.code}</td><td>{x.name_ar}</td><td>{x.kind==="earning"?"استحقاق":x.kind==="deduction"?"استقطاع موظف":"حصة صاحب العمل"}</td><td>{x.method}</td><td>{money(x.default_value)}</td></tr>)}</tbody></table>{!components.length&&<div className="table-empty">لا توجد عناصر مرتبات معرفة.</div>}</div>
    </>}
    {tab==="attendance"&&<>
      <div className="asset-report-filters">
        <label>الشهر والسنة<input type="month" value={attendanceMonth} onChange={e=>setAttendanceMonth(e.target.value)}/></label>
        <span className="module-count">{attendance.filter(x=>x.work_date.startsWith(attendanceMonth)).length} سجل في الشهر</span>
      </div>
      <div className="asset-report-note">جدول الحضور الشهري: الموظفون في الصفوف والأيام في الأعمدة. تغيير الحالة يحفظها مباشرة؛ الأيام الفارغة لا تُسجل تلقائيًا.</div>
      <div className="data-table-wrap" style={{overflowX:"auto"}}>
        <table style={{minWidth:`${attendanceDays.length*86+220}px`}}>
          <thead><tr><th style={{position:"sticky",right:0,background:"var(--surface, #fff)",zIndex:2,minWidth:190}}>الموظف</th>{attendanceDays.map(day=><th key={day} style={{minWidth:82,textAlign:"center"}}>{Number(day.slice(-2))}</th>)}</tr></thead>
          <tbody>{employees.map(employee=><tr key={employee.id}>
            <th style={{position:"sticky",right:0,background:"var(--surface, #fff)",zIndex:1,textAlign:"right"}}>{employee.code} — {employee.full_name_ar}</th>
            {attendanceDays.map(day=>{
              const record=attendance.find(item=>item.employee===employee.id&&item.work_date===day);
              return <td key={day} style={{padding:"4px"}}>
                <select aria-label={`حالة ${employee.full_name_ar} بتاريخ ${day}`} value={record?.status||""}
                  disabled={busy} onChange={e=>{const value=e.target.value as HrAttendance["status"];if(value)void updateAttendanceCell(employee.id,day,value);}}
                  style={{width:"100%",minWidth:76,fontSize:12,padding:"5px 2px"}}>
                  <option value="">—</option><option value="present">حضور</option><option value="absent">غياب</option>
                  <option value="leave">إجازة</option><option value="unpaid_leave">بدون راتب</option><option value="holiday">عطلة</option>
                </select>
              </td>;
            })}
          </tr>)}
          {!employees.length&&<tr><td colSpan={attendanceDays.length+1}>لا يوجد موظفون لعرضهم.</td></tr>}
        </tbody>
        </table>
      </div>
      <form className="journal-form" onSubmit={submitAttendance} style={{marginTop:20}}>
        <div className="section-heading form-wide"><div><h3>تفاصيل يوم عمل</h3><p>لإضافة ساعات العمل والإضافي والتأخير وعمل الجمعة</p></div></div>
        <label>الموظف<select required value={attendanceDraft.employee} onChange={e=>setAttendanceDraft({...attendanceDraft,employee:e.target.value})}><option value="">اختر الموظف</option>{employees.map(e=><option key={e.id} value={e.id}>{e.code} — {e.full_name_ar}</option>)}</select></label>
        <label>التاريخ<input required type="date" value={attendanceDraft.work_date} onChange={e=>setAttendanceDraft({...attendanceDraft,work_date:e.target.value})}/></label>
        <label>الحالة<select value={attendanceDraft.status} onChange={e=>setAttendanceDraft({...attendanceDraft,status:e.target.value as HrAttendance["status"]})}><option value="present">حضور</option><option value="absent">غياب</option><option value="leave">إجازة</option><option value="unpaid_leave">إجازة بدون راتب</option><option value="holiday">عطلة</option></select></label>
        <label>ساعات العمل<input type="number" min="0" step="0.25" value={attendanceDraft.worked_hours} onChange={e=>setAttendanceDraft({...attendanceDraft,worked_hours:e.target.value})}/></label>
        <label>ساعات إضافية<input type="number" min="0" step="0.25" value={attendanceDraft.overtime_hours} onChange={e=>setAttendanceDraft({...attendanceDraft,overtime_hours:e.target.value})}/></label>
        <label>دقائق التأخير<input type="number" min="0" step="1" value={attendanceDraft.late_minutes} onChange={e=>setAttendanceDraft({...attendanceDraft,late_minutes:e.target.value})}/></label>
        <label className="checkbox-filter"><input type="checkbox" checked={attendanceDraft.friday_worked} onChange={e=>setAttendanceDraft({...attendanceDraft,friday_worked:e.target.checked})}/> عمل يوم الجمعة</label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!employees.length}>تسجيل / تحديث تفاصيل اليوم</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>سجل الحضور التفصيلي</h2><p>ساعات العمل والإضافي والتأخير</p></div><span className="module-count">{attendance.length} سجل</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>الكود</th><th>الموظف</th><th>الحالة</th><th>ساعات العمل</th><th>إضافي</th><th>تأخير</th></tr></thead><tbody>{attendance.filter(x=>x.work_date.startsWith(attendanceMonth)).map(x=><tr key={x.id}><td>{x.work_date}</td><td>{x.employee_code}</td><td>{x.employee_name}</td><td>{x.status}</td><td>{money(x.worked_hours)}</td><td>{money(x.overtime_hours)}</td><td>{x.late_minutes}</td></tr>)}</tbody></table>{!attendance.some(x=>x.work_date.startsWith(attendanceMonth))&&<div className="table-empty">لا توجد سجلات حضور في الشهر المحدد.</div>}</div>
    </>}
    {tab==="assignments"&&<>
      <form className="journal-form" onSubmit={submitAssignment}>
        <label>الموظف<select required value={assignmentDraft.employee} onChange={e=>setAssignmentDraft({...assignmentDraft,employee:e.target.value})}><option value="">اختر الموظف</option>{employees.map(e=><option key={e.id} value={e.id}>{e.code} — {e.full_name_ar}</option>)}</select></label>
        <label>عنصر المرتب<select required value={assignmentDraft.component} onChange={e=>setAssignmentDraft({...assignmentDraft,component:e.target.value})}><option value="">اختر العنصر</option>{components.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
        <label>قيمة خاصة للموظف<input type="number" min="0" step="0.0001" placeholder="اتركها فارغة لاستخدام القيمة الافتراضية" value={assignmentDraft.value} onChange={e=>setAssignmentDraft({...assignmentDraft,value:e.target.value})}/></label>
        <label>ساري من<input type="date" required value={assignmentDraft.effective_from} onChange={e=>setAssignmentDraft({...assignmentDraft,effective_from:e.target.value})}/></label>
        <label>ساري حتى<input type="date" value={assignmentDraft.effective_to} onChange={e=>setAssignmentDraft({...assignmentDraft,effective_to:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!employees.length||!components.length}>ربط العنصر بالموظف</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>الموظف</th><th>العنصر</th><th>القيمة الخاصة</th><th>من</th><th>إلى</th></tr></thead><tbody>{employeeComponents.map(x=><tr key={x.id}><td>{employees.find(e=>e.id===x.employee)?.full_name_ar||x.employee}</td><td>{x.component_name}</td><td>{x.value===null?"القيمة الافتراضية":money(x.value)}</td><td>{x.effective_from}</td><td>{x.effective_to||"مستمر"}</td></tr>)}</tbody></table>{!employeeComponents.length&&<div className="table-empty">لم يتم ربط عناصر بموظفين بعد.</div>}</div>
      <h3 style={{marginTop:24}}>سجل الراتب والزيادات السنوية</h3>
      <div className="asset-report-note">يمكن تسجيل الزيادة كنسبة مئوية أو مبلغ ثابت. يتم احتساب الراتب الجديد من الراتب الساري قبل تاريخ الزيادة، مع الاحتفاظ بكل تغيير سابق دون تعديله. لبدل منفصل عن الأساسي، أنشئ عنصرًا من نوع «استحقاق» واربطه بالموظف من شاشة عناصر الموظفين بتاريخ سريان.</div>
      <form className="journal-form" onSubmit={submitSalaryChange}>
        <label>الموظف<select required value={salaryDraft.employee} onChange={e=>setSalaryDraft({...salaryDraft,employee:e.target.value})}><option value="">اختر الموظف</option>{employees.map(e=><option key={e.id} value={e.id}>{e.code} — {e.full_name_ar}</option>)}</select></label>
        <label>تاريخ السريان<input type="date" required value={salaryDraft.effective_date} onChange={e=>setSalaryDraft({...salaryDraft,effective_date:e.target.value})}/></label>
        <label>طريقة الزيادة<select value={salaryDraft.raise_mode} onChange={e=>setSalaryDraft({...salaryDraft,raise_mode:e.target.value as "percentage"|"amount"})}><option value="percentage">نسبة مئوية %</option><option value="amount">مبلغ ثابت</option></select></label>
        <label>{salaryDraft.raise_mode==="percentage"?"نسبة الزيادة %":"قيمة الزيادة" }<input type="number" min="0" step="0.01" required value={salaryDraft.raise_value} onChange={e=>setSalaryDraft({...salaryDraft,raise_value:e.target.value})}/></label>
        <label>نوع التغيير<select value={salaryDraft.change_type} onChange={e=>setSalaryDraft({...salaryDraft,change_type:e.target.value})}><option value="annual_raise">زيادة سنوية</option><option value="promotion">ترقية</option><option value="adjustment">تعديل</option></select></label>
        <label className="form-wide">سبب التغيير<input value={salaryDraft.reason} onChange={e=>setSalaryDraft({...salaryDraft,reason:e.target.value})}/></label>
        <div className="form-wide asset-report-note">الراتب قبل السريان: <strong>{money(salaryBeforeEffectiveDate)}</strong> — الراتب الجديد بعد الزيادة: <strong>{money(calculatedNewSalary)}</strong></div>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!employees.length||!salaryDraft.employee||salaryIncrease<=0}>تسجيل تغيير الراتب</button></div>
      </form>
      <div className="section-heading" style={{marginTop:20}}><div><h3>التاريخ الوظيفي للراتب</h3><p>سجلات مرتبة بتاريخ السريان، تشمل الراتب السابق والجديد</p></div><span className="module-count">{salaryHistoryRows.length} تغيير</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>تاريخ السريان</th><th>الراتب السابق</th><th>الراتب الجديد</th><th>الفرق</th><th>نوع التغيير</th><th>السبب</th></tr></thead><tbody>
        {salaryHistoryRows.map(row=><tr key={row.id}><td>{row.effective_date}</td><td>{money(row.previous_salary)}</td><td>{money(row.new_salary)}</td><td>{money(Number(row.new_salary)-Number(row.previous_salary))}</td><td>{row.change_type==="annual_raise"?"زيادة سنوية":row.change_type==="promotion"?"ترقية":"تعديل"}</td><td>{row.reason||"—"}</td></tr>)}
      </tbody></table>{!salaryHistoryRows.length&&<div className="table-empty">{salaryDraft.employee?"لا يوجد سجل تغييرات لهذا الموظف.":"اختر موظفًا لعرض سجل الراتب."}</div>}</div>
    </>}
    {tab==="leaveLoans"&&<>
      <h3>طلبات الإجازات</h3>
      <form className="journal-form" onSubmit={submitLeave}>
        <label>الموظف<select required value={leaveDraft.employee} onChange={e=>setLeaveDraft({...leaveDraft,employee:e.target.value})}><option value="">اختر الموظف</option>{employees.map(e=><option key={e.id} value={e.id}>{e.code} — {e.full_name_ar}</option>)}</select></label>
        <label>نوع الإجازة<select value={leaveDraft.kind} onChange={e=>setLeaveDraft({...leaveDraft,kind:e.target.value as HrLeaveRequest["kind"]})}><option value="annual">سنوية</option><option value="sick">مرضية</option><option value="emergency">عارضة</option><option value="unpaid">بدون راتب</option></select></label>
        <label>من تاريخ<input type="date" required value={leaveDraft.start_date} onChange={e=>setLeaveDraft({...leaveDraft,start_date:e.target.value})}/></label>
        <label>إلى تاريخ<input type="date" required value={leaveDraft.end_date} onChange={e=>setLeaveDraft({...leaveDraft,end_date:e.target.value})}/></label>
        <label>عدد الأيام<input type="number" min="0.5" step="0.5" value={leaveDraft.days} onChange={e=>setLeaveDraft({...leaveDraft,days:e.target.value})}/></label>
        <label>السبب<input value={leaveDraft.reason} onChange={e=>setLeaveDraft({...leaveDraft,reason:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!employees.length}>تسجيل طلب الإجازة</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>الموظف</th><th>النوع</th><th>الفترة</th><th>الأيام</th><th>الحالة</th><th></th></tr></thead><tbody>{leaveRequests.map(x=><tr key={x.id}><td>{x.employee_name}</td><td>{x.kind}</td><td>{x.start_date} — {x.end_date}</td><td>{x.days}</td><td>{x.status}</td><td>{x.status==="pending"&&<div className="form-actions"><button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.approveLeave(x.id),"تم اعتماد الإجازة.")}>اعتماد</button><button type="button" className="subtle-button" disabled={busy} onClick={()=>{if(window.confirm("هل تريد رفض طلب الإجازة؟")) void action(()=>hrApi.rejectLeave(x.id),"تم رفض طلب الإجازة.");}}>رفض</button></div>}</td></tr>)}</tbody></table>{!leaveRequests.length&&<div className="table-empty">لا توجد طلبات إجازة.</div>}</div>
      <h3 style={{marginTop:24}}>السلف وجدول الأقساط</h3>
      <form className="journal-form" onSubmit={submitLoan}>
        <label>الموظف<select required value={loanDraft.employee} onChange={e=>setLoanDraft({...loanDraft,employee:e.target.value})}><option value="">اختر الموظف</option>{employees.map(e=><option key={e.id} value={e.id}>{e.code} — {e.full_name_ar}</option>)}</select></label>
        <label>رقم السلفة<input required value={loanDraft.reference} onChange={e=>setLoanDraft({...loanDraft,reference:e.target.value})}/></label>
        <label>تاريخ السلفة<input type="date" required value={loanDraft.issue_date} onChange={e=>setLoanDraft({...loanDraft,issue_date:e.target.value})}/></label>
        <label>أول قسط<input type="date" required value={loanDraft.first_due_date} onChange={e=>setLoanDraft({...loanDraft,first_due_date:e.target.value})}/></label>
        <label>حساب ذمم سلف الموظفين<select required value={loanDraft.receivable_account} onChange={e=>setLoanDraft({...loanDraft,receivable_account:e.target.value})}><option value="">اختر حساب أصل</option>{accounts.filter(a=>a.nature==="asset"&&a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} — {a.name_ar}</option>)}</select></label>
        <label>قيمة السلفة<input type="number" min="0.01" step="0.01" value={loanDraft.principal} onChange={e=>setLoanDraft({...loanDraft,principal:e.target.value})}/></label>
        <label>عدد الأقساط<input type="number" min="1" step="1" value={loanDraft.installment_count} onChange={e=>setLoanDraft({...loanDraft,installment_count:e.target.value})}/></label>
        <label>قيمة القسط<input type="number" min="0.01" step="0.01" value={loanDraft.installment_amount} onChange={e=>setLoanDraft({...loanDraft,installment_amount:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!employees.length}>إنشاء السلفة وجدول الأقساط</button></div>
      </form>
      <label style={{maxWidth:360,marginTop:14}}>الخزينة أو البنك لصرف السلفة<select value={loanDisbursementAccount} onChange={e=>setLoanDisbursementAccount(e.target.value)}><option value="">اختر حساب الخزينة</option>{treasuryAccounts.map(account=><option key={account.id} value={account.id}>{account.code} — {account.name_ar} ({account.currency_code})</option>)}</select></label>
      <div className="data-table-wrap"><table><thead><tr><th>رقم السلفة</th><th>الموظف</th><th>الأصل</th><th>الرصيد</th><th>قيمة القسط</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{loans.map(x=><tr key={x.id}><td>{x.reference}</td><td>{x.employee_name}</td><td>{money(x.principal)}</td><td>{money(x.outstanding_balance)}</td><td>{money(x.installment_amount)}</td><td>{{draft:"مسودة",pending:"بانتظار الاعتماد",active:"سارية",settled:"مسددة",suspended:"موقوفة"}[x.status] || x.status}</td><td>{(x.status==="draft"||x.status==="pending") ? <button type="button" className="subtle-button" disabled={busy||(x.status==="draft"&&!loanDisbursementAccount)} onClick={()=>void disburseLoan(x.id)}>{x.status==="pending"?"إعادة محاولة الصرف":"صرف السلفة"}</button> : x.disbursement_transaction ? "تم الصرف" : "—"}</td></tr>)}</tbody></table>{!loans.length&&<div className="table-empty">لا توجد سلف مسجلة.</div>}</div>
    </>}
    {tab==="rules"&&<>
      <div className="asset-report-note">لا توجد نسب قانونية افتراضية. أدخل القيم المعتمدة للشركة والسنة من مستنداتك الرسمية. محرك الحساب لن يستخدم قواعد ناقصة.</div>
      <form className="journal-form" onSubmit={submitRuleSet}>
        <label>اسم مجموعة القواعد<input required value={ruleSetDraft.name} onChange={e=>setRuleSetDraft({...ruleSetDraft,name:e.target.value})}/></label>
        <label>الدولة<input readOnly value={country==="SA"?"السعودية":"مصر"}/></label>
        <label>تاريخ السريان<input type="date" required value={ruleSetDraft.effective_from} onChange={e=>setRuleSetDraft({...ruleSetDraft,effective_from:e.target.value})}/></label>
        <label>تاريخ الانتهاء<input type="date" value={ruleSetDraft.effective_to} onChange={e=>setRuleSetDraft({...ruleSetDraft,effective_to:e.target.value})}/></label>
        <label>مقسم اليوم الشهري<input type="number" min="1" step="1" required value={ruleSetDraft.monthly_daily_divisor} onChange={e=>setRuleSetDraft({...ruleSetDraft,monthly_daily_divisor:e.target.value})}/></label>
        <label>الحد الأدنى لوعاء التأمين<input type="number" min="0" step="0.01" required value={ruleSetDraft.min_base} onChange={e=>setRuleSetDraft({...ruleSetDraft,min_base:e.target.value})}/></label>
        <label>الحد الأقصى لوعاء التأمين<input type="number" min="0" step="0.01" required value={ruleSetDraft.max_base} onChange={e=>setRuleSetDraft({...ruleSetDraft,max_base:e.target.value})}/></label>
        <label>نسبة الموظف %<input type="number" min="0" max="100" step="0.0001" required value={ruleSetDraft.employee_rate} onChange={e=>setRuleSetDraft({...ruleSetDraft,employee_rate:e.target.value})}/></label>
        <label>نسبة صاحب العمل %<input type="number" min="0" max="100" step="0.0001" required value={ruleSetDraft.employer_rate} onChange={e=>setRuleSetDraft({...ruleSetDraft,employer_rate:e.target.value})}/></label>
        <label>الإعفاء الضريبي السنوي<input type="number" min="0" step="0.01" required value={ruleSetDraft.annual_personal_exemption} onChange={e=>setRuleSetDraft({...ruleSetDraft,annual_personal_exemption:e.target.value})}/></label>
        <label className="form-wide">شرائح الضريبة السنوية — JSON<textarea rows={4} value={ruleSetDraft.annual_brackets} onChange={e=>setRuleSetDraft({...ruleSetDraft,annual_brackets:e.target.value})}/></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy||!country}>حفظ مجموعة القواعد</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>الاسم</th><th>الدولة</th><th>من</th><th>إلى</th><th>الحالة</th></tr></thead><tbody>{ruleSets.map(x=><tr key={x.id}><td>{x.name}</td><td>{x.country}</td><td>{x.effective_from}</td><td>{x.effective_to||"مستمرة"}</td><td>{x.is_active?"نشطة":"غير نشطة"}</td></tr>)}</tbody></table>{!ruleSets.length&&<div className="table-empty">لا توجد مجموعات قواعد محفوظة.</div>}</div>
    </>}
    {tab==="payroll"&&<>
      <form className="journal-form" onSubmit={submitCycle}>
        <label>بداية الفترة<input required type="date" value={cycleDraft.period_start} onChange={e=>setCycleDraft({...cycleDraft,period_start:e.target.value})}/></label>
        <label>نهاية الفترة<input required type="date" value={cycleDraft.period_end} onChange={e=>setCycleDraft({...cycleDraft,period_end:e.target.value})}/></label>
        <label>تاريخ الصرف<input required type="date" value={cycleDraft.payment_date} onChange={e=>setCycleDraft({...cycleDraft,payment_date:e.target.value})}/></label>
        <label>مجموعة القواعد القانونية<select value={cycleDraft.rule_set} onChange={e=>setCycleDraft({...cycleDraft,rule_set:e.target.value})}><option value="">بدون مجموعة (للعناصر غير القانونية فقط)</option>{ruleSets.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.name} · {x.effective_from}</option>)}</select></label>
        <label>حساب الصرف (للاستخدام بعد الترحيل)<select value={paymentAccountId} onChange={e=>setPaymentAccountId(e.target.value)}><option value="">اختر البنك أو الخزينة</option>{treasuryAccounts.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar} ({x.currency_code})</option>)}</select></label>
        <div className="form-actions form-wide"><button className="primary-button" disabled={busy}>إنشاء دورة جديدة</button></div>
      </form>
      <div className="section-heading" style={{marginTop:24}}><div><h2>دورات المرتبات</h2><p>لا يتم إنشاء قيود أو صرف أموال في هذه المرحلة قبل استكمال المراجعة المحاسبية</p></div><span className="module-count">{cycles.length} دورة</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الفترة</th><th>تاريخ الصرف</th><th>عدد الموظفين</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
        {cycles.map(c=><tr key={c.id}><td>{c.period_start} — {c.period_end}</td><td>{c.payment_date}</td><td>{c.results_count}</td><td><span className="status-pill">{cycleLabels[c.status]||c.status}</span></td><td style={{display:"flex",gap:6,flexWrap:"wrap"}}>
          {["draft","calculated","review"].includes(c.status)&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.calculatePayroll(c.id),"تم احتساب الدورة. راجع النتائج قبل إرسالها للاعتماد.")}>احتساب</button>}
          {c.status==="calculated"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.sendPayrollToReview(c.id),"تم إرسال الدورة للمراجعة.")}>إرسال للمراجعة</button>}
          {c.status==="review"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void action(()=>hrApi.approvePayroll(c.id),"تم اعتماد الدورة.")}>اعتماد</button>}
          {c.status==="approved"&&<button type="button" className="primary-button" disabled={busy} onClick={()=>void action(()=>hrApi.postPayroll(c.id),"تم ترحيل استحقاق المرتبات محاسبيًا لكل فرع.")}>ترحيل محاسبي</button>}
          {c.status==="posted"&&<button type="button" className="primary-button" disabled={busy||!paymentAccountId} onClick={()=>void action(()=>hrApi.preparePayrollPayment(c.id,Number(paymentAccountId)),"تم إعداد مستندات الصرف؛ راجع طلبات الموافقة المالية قبل تأكيد الصرف.")}>إعداد الصرف</button>}
          {c.status==="payment_pending"&&<><button type="button" className="primary-button" disabled={busy} onClick={()=>void action(()=>hrApi.completePayrollPayment(c.id),"تم صرف المرتبات وتحديث أقساط السلف.")}>تأكيد الصرف</button><button type="button" className="subtle-button" disabled={busy} onClick={()=>{if(window.confirm("سيتم إلغاء مستندات الصرف المسودة وإعادة دورة المرتبات إلى حالة مرحلة. هل تريد المتابعة؟")) void action(()=>hrApi.cancelPayrollPaymentPreparation(c.id),"تم إلغاء إعداد الصرف بأمان؛ لم يتم المساس بقيد استحقاق المرتبات.");}}>إلغاء إعداد الصرف</button></>}
          <button type="button" className="subtle-button" disabled={busy} onClick={()=>void showResults(c.id)}>النتائج</button>
        </td></tr>)}
      </tbody></table>{!cycles.length&&<div className="table-empty">لا توجد دورات مرتبات بعد.</div>}</div>

      <section className="report-drilldown" style={{marginTop:24}}>
        <div className="section-heading"><div><h2>إجمالي مرتبات الموظفين خلال فترة</h2><p>يعرض الدورات المرحلة أو التي تم إعداد صرفها أو صرفها فعليًا، مع الاحتفاظ بتفاصيل كل عنصر كما كان وقت الاحتساب</p></div>
          <button type="button" className="subtle-button" disabled={!payReport} onClick={exportEmployeePayReport}>تصدير CSV (Excel)</button>
        </div>
        <div className="asset-report-filters">
          <label>من تاريخ<input type="date" value={payReportFrom} onChange={e=>setPayReportFrom(e.target.value)}/></label>
          <label>إلى تاريخ<input type="date" value={payReportTo} onChange={e=>setPayReportTo(e.target.value)}/></label>
          <button type="button" className="primary-button" disabled={busy||!companyId||payReportFrom>payReportTo} onClick={()=>void loadEmployeePayReport()}>{busy?"جارٍ التحميل…":"عرض إجمالي المرتبات"}</button>
        </div>
        {payReport && <>
          <p className="asset-report-note">{payReport.currency_note}</p>
          {payReport.totals && <div className="metrics-grid report-metrics">
            <article className="metric-card"><div className="metric-top">الاستحقاقات</div><strong className="metric-value">{money(payReport.totals.earnings)}</strong></article>
            <article className="metric-card"><div className="metric-top">الاستقطاعات</div><strong className="metric-value">{money(payReport.totals.employee_deductions)}</strong></article>
            <article className="metric-card"><div className="metric-top">صافي المرتبات</div><strong className="metric-value">{money(payReport.totals.net_pay)}</strong></article>
            <article className="metric-card"><div className="metric-top">تكلفة الشركة</div><strong className="metric-value">{money(payReport.totals.company_cost)}</strong></article>
          </div>}
          <div className="data-table-wrap"><table><thead><tr><th>الموظف</th><th>الفرع</th><th>القسم</th><th>العملة</th><th>الدورات</th>{reportColumns.map(col=><th key={col.code+col.kind}>{col.name}</th>)}<th>الاستحقاقات</th><th>الاستقطاعات</th><th>حصة الشركة</th><th>الصافي</th><th>تكلفة الشركة</th><th>الفترات</th></tr></thead><tbody>
            {payReport.employees.map(employee=><tr key={employee.employee_id}><td>{employee.employee_code} — {employee.employee_name}</td><td>{employee.branch}</td><td>{employee.department||"—"}</td><td>{employee.currency}</td><td>{employee.cycle_count}</td>
              {reportColumns.map(col=><td key={col.code+col.kind}>{money(employee.components.filter(item=>item.code===col.code&&item.kind===col.kind).reduce((sum,item)=>sum+Number(item.amount),0))}</td>)}
              <td>{money(employee.earnings)}</td><td>{money(employee.employee_deductions)}</td><td>{money(employee.employer_share)}</td><td><strong>{money(employee.net_pay)}</strong></td><td>{money(employee.company_cost)}</td>
              <td><details><summary>عرض الفترات</summary><ul>{employee.periods.map(period=><li key={period.cycle_id}>{period.period_start} — {period.period_end}: {money(period.net_pay)} ({cycleLabels[period.status]||period.status})</li>)}</ul></details></td>
            </tr>)}
          </tbody></table>{!payReport.employees.length&&<div className="table-empty">لا توجد مرتبات مرحلة خلال الفترة المحددة.</div>}</div>
        </>}
      </section>

      {selectedCycle&&<><div className="section-heading" style={{marginTop:24}}><div><h2>نتائج الدورة رقم {selectedCycle}</h2><p>حصة صاحب العمل ظاهرة في جانبي الكشف كتسوية عرضية لا تؤثر على صافي الموظف</p></div></div>
        <div className="metrics-grid report-metrics"><article className="metric-card"><div className="metric-top">صافي المستحقات</div><strong className="metric-value">{money(totals.net)}</strong></article><article className="metric-card"><div className="metric-top">حصة صاحب العمل</div><strong className="metric-value">{money(totals.employer)}</strong></article><article className="metric-card"><div className="metric-top">تكلفة الشركة</div><strong className="metric-value">{money(totals.cost)}</strong></article></div>
        <div className="data-table-wrap"><table><thead><tr><th>الموظف</th>{dynamicPayrollColumns.map(column=><th key={column.code}>{column.name}</th>)}<th>الاستحقاقات الظاهرة</th><th>الاستقطاعات الظاهرة</th><th>صافي الموظف</th><th>تكلفة الشركة</th><th>تفاصيل</th></tr></thead><tbody>{results.map(r=><tr key={r.id}><td>{r.employee_code} — {r.employee_name}</td>{dynamicPayrollColumns.map(column=><td key={column.code}>{money(r.lines.filter(line=>line.code===column.code).reduce((sum,line)=>sum+Number(line.amount),0))}</td>)}<td>{money(r.displayed_earnings_total)}</td><td>{money(r.displayed_deductions_total)}</td><td><strong>{money(r.net_pay)}</strong></td><td>{money(r.company_cost)}</td><td><button type="button" className="text-action-button" onClick={()=>setSelectedPayslip(r)}>قسيمة المرتب</button></td></tr>)}</tbody></table>{!results.length&&<div className="table-empty">لا توجد نتائج لهذه الدورة.</div>}</div>

      {selectedPayslip && selectedCycle && <section className="report-drilldown payroll-slip" style={{marginTop:20}}>
        <div className="work-panel-head"><div><h3>قسيمة مرتب: {selectedPayslip.employee_name}</h3><p>كود الموظف: {selectedPayslip.employee_code} · دورة رقم {selectedCycle} · {cycles.find(c=>c.id===selectedCycle)?.period_start||""} — {cycles.find(c=>c.id===selectedCycle)?.period_end||""}</p></div>
          <div className="report-actions"><button type="button" className="subtle-button" onClick={()=>{const target=document.querySelector(".payroll-slip");if(target){target.classList.add("print-active");window.print();target.classList.remove("print-active");}}}>طباعة القسيمة / PDF</button><button type="button" className="subtle-button" onClick={()=>setSelectedPayslip(null)}>إغلاق</button></div>
        </div>
        <div className="asset-cashflow-totals"><span>الراتب الأساسي: {money(selectedPayslip.basic_salary_snapshot)}</span><span>حصة صاحب العمل: {money(selectedPayslip.employer_share_total)}</span><b>صافي المستحق: {money(selectedPayslip.net_pay)}</b></div>
        <div className="data-table-wrap"><table><thead><tr><th>البند</th><th>النوع</th><th>القيمة</th></tr></thead><tbody>
          {selectedPayslip.lines.filter(line=>line.code!=="EMPLOYER_SHARE_DISPLAY"&&line.code!=="EMPLOYER_SHARE_OFFSET").map((line,index)=><tr key={index}><td>{line.name}</td><td>{line.kind==="earning"?"استحقاق":line.kind==="deduction"?"استقطاع":line.kind==="employer_share"?"حصة صاحب العمل":"بند"}</td><td>{money(line.amount)}</td></tr>)}
        </tbody></table></div>
        <div className="asset-cashflow-totals"><span>إجمالي الاستحقاقات الظاهرة: {money(selectedPayslip.displayed_earnings_total)}</span><span>إجمالي الاستقطاعات الظاهرة: {money(selectedPayslip.displayed_deductions_total)}</span><b>صافي المرتب: {money(selectedPayslip.net_pay)}</b><span>إجمالي تكلفة الشركة: {money(selectedPayslip.company_cost)}</span></div>
        <p className="asset-report-note">تظهر حصة صاحب العمل ضمن جانبي القسيمة للشفافية، ولا تؤثر على صافي الموظف. القيم المعروضة محفوظة كلقطة تاريخية لدورة المرتبات.</p>
      </section>}

      </>}
    </>}
    <p className="asset-report-note">ملاحظة تشغيلية: الترحيل المحاسبي والصرف يمران بمراحل اعتماد منفصلة. يجب ربط حسابات الاستقطاعات وحصة صاحب العمل، وإدخال القواعد القانونية المعتمدة قبل احتساب دورة فعلية. لا يتم تحديث أقساط السلف إلا بعد نجاح صرف مستندات الخزينة.</p>
  </section>;
}
