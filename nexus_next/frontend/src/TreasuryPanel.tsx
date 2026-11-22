/*
 * الخزينة والبنوك: إدارة حسابات النقدية والبنوك وحركات القبض والصرف والتحويل.
 * تشمل التسوية البنكية ومطابقة كشف البنك مع الحركات المسجلة.
 */
import { useCallback, useEffect, useState } from "react";
import { api, treasuryApi, bankReconciliationApi, importBankStatementCsv, type Account, type Branch, type FiscalYear, type TreasuryAccount, type TreasuryTransaction, type BankReconciliation, type BankReconciliationSummary, type Currency, type ExchangeRate, type TreasuryFxRevaluation } from "./api";

type Props = { companyId: number };
const today = () => new Date().toISOString().slice(0, 10);
const money = (v: string | number) => Number(v || 0).toLocaleString("ar-EG", {maximumFractionDigits:2});

// شاشة الخزينة والبنوك وإدارة الحسابات والحركات والتسويات البنكية.
export default function TreasuryPanel({companyId}: Props) {
  const [tab,setTab]=useState<"transactions"|"accounts"|"reconciliation">("transactions");
  const [accounts,setAccounts]=useState<TreasuryAccount[]>([]);
  const [transactions,setTransactions]=useState<TreasuryTransaction[]>([]);
  const [branches,setBranches]=useState<Branch[]>([]);
  const [years,setYears]=useState<FiscalYear[]>([]);
  const [gl,setGl]=useState<Account[]>([]);
  const [currencies,setCurrencies]=useState<Currency[]>([]);
  const [exchangeRates,setExchangeRates]=useState<ExchangeRate[]>([]);
  const [baseCurrencyCode,setBaseCurrencyCode]=useState("EGP");
  const [reval,setReval]=useState({account:"",fiscal_year:"",revaluation_date:today(),rate_date:today()});
  const [fxHistory,setFxHistory]=useState<TreasuryFxRevaluation[]>([]);
  const [fxHistoryAccount,setFxHistoryAccount]=useState("");
  const [reconciliations,setReconciliations]=useState<BankReconciliation[]>([]);
  const [selectedRecon,setSelectedRecon]=useState("");
  const [summary,setSummary]=useState<BankReconciliationSummary|null>(null);
  const [loading,setLoading]=useState(true), [busy,setBusy]=useState(false), [error,setError]=useState("");
  const [acc,setAcc]=useState({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:"EGP",opening_balance:"0"});
  const [doc,setDoc]=useState({branch:"",fiscal_year:"",source_account:"",destination_account:"",counter_account:"",kind:"receipt",document_number:"",transaction_date:today(),amount:"",transaction_amount:"",exchange_rate:"",exchange_rate_date:today(),description:"",reference:""});
  const [rec,setRec]=useState({bank_account:"",statement_number:"",statement_date:today(),opening_balance:"0",closing_balance:"0"});
  const [line,setLine]=useState({line_number:"1",transaction_date:today(),description:"",reference:"",amount:""});
  const [matchIds,setMatchIds]=useState<Record<number,string>>({});
  const [csvFile,setCsvFile]=useState<File|null>(null);
  const load=useCallback(async()=>{
    setLoading(true);setError("");
    try {
      const [a,t,b,y,g,rs,cs,xr,companies]=await Promise.all([treasuryApi.accounts(companyId),treasuryApi.transactions(companyId),api.branches(companyId),api.fiscalYears(companyId),api.accounts(companyId),bankReconciliationApi.list(companyId),api.currencies(),api.exchangeRates(companyId),api.companies()]);
      const companyBase=companies.find(v=>v.id===companyId)?.base_currency_code||"EGP";
      setAccounts(a);setTransactions(t);setBranches(b);setYears(y);setGl(g);setReconciliations(rs);setCurrencies(cs);setExchangeRates(xr);setBaseCurrencyCode(companyBase);
      setAcc(old=>({...old,currency_code:old.currency_code==="EGP"&&companyBase!=="EGP"?companyBase:old.currency_code}));
      setReval(old=>({...old,account:old.account||String(a.find(v=>v.is_active&&v.currency_code!==companyBase)?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||"")}));
      setDoc(old=>({...old,branch:old.branch||String(b[0]?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||""),source_account:old.source_account||String(a.find(v=>v.is_active)?.id||"")}));
      setRec(old=>({...old,bank_account:old.bank_account||String(a.find(v=>v.kind==="bank"&&v.is_active)?.id||"")}));
      setSelectedRecon(old=>old||String(rs[0]?.id||""));
    } catch(e){setError((e as Error).message||"تعذر تحميل بيانات الخزينة.");} finally{setLoading(false);}
  },[companyId]);
  useEffect(()=>{void load();},[load]);
  const refreshSummary=useCallback(async(id:string)=>{if(!id){setSummary(null);return;}try{setSummary(await bankReconciliationApi.summary(Number(id)));}catch(e){setError((e as Error).message||"تعذر تحميل التسوية.");}},[]);
  useEffect(()=>{if(tab==="reconciliation"&&selectedRecon)void refreshSummary(selectedRecon);},[tab,selectedRecon,refreshSummary]);
  // حفظ حساب خزينة أو بنك جديد ببيانات الفرع والحساب المقابل.
const saveAcc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    await treasuryApi.createAccount({company:companyId,branch:acc.branch?Number(acc.branch):null,account:Number(acc.account),kind:acc.kind,code:acc.code,name_ar:acc.name_ar,bank_name:acc.bank_name,account_number:acc.account_number,iban:acc.iban,currency_code:acc.currency_code,opening_balance:acc.opening_balance});
    setAcc({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:"EGP",opening_balance:"0"});await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ الحساب.");}finally{setBusy(false);}};
  // تسجيل مستند حركة نقدية أو بنكية حسب نوع العملية المختار.
const saveDoc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    const source=accounts.find(a=>a.id===Number(doc.source_account));
    const foreign=!!source&&source.currency_code!==baseCurrencyCode;
    const currency=foreign?currencies.find(v=>v.code===source?.currency_code):undefined;
    if(foreign&&(!currency||Number(doc.transaction_amount)<=0||Number(doc.exchange_rate)<=0||!doc.exchange_rate_date))throw new Error("أدخل المبلغ الأجنبي وسعر الصرف وتاريخه المعتمد.");
    const baseAmount=foreign?(Number(doc.transaction_amount)*Number(doc.exchange_rate)).toFixed(4):doc.amount;
    if(foreign&&!exchangeRates.some(v=>v.currency_code===source?.currency_code&&v.rate_date===doc.exchange_rate_date&&Number(v.rate)===Number(doc.exchange_rate)))throw new Error("سعر الصرف غير موجود ضمن الأسعار المعتمدة للشركة في التاريخ المحدد.");
    await treasuryApi.createTransaction({company:companyId,branch:Number(doc.branch),fiscal_year:Number(doc.fiscal_year),source_account:Number(doc.source_account),destination_account:doc.kind==="transfer"?Number(doc.destination_account):null,counter_account:doc.kind==="transfer"?null:Number(doc.counter_account),kind:doc.kind,document_number:doc.document_number,transaction_date:doc.transaction_date,amount:baseAmount,...(foreign?{transaction_currency:currency!.id,transaction_amount:doc.transaction_amount,exchange_rate:doc.exchange_rate,exchange_rate_date:doc.exchange_rate_date}:{}),description:doc.description,reference:doc.reference});
    setDoc(old=>({...old,document_number:"",amount:"",transaction_amount:"",exchange_rate:"",description:"",reference:""}));await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ المستند.");}finally{setBusy(false);}};
  // ترحيل حركة الخزينة المحددة ثم تحديث الأرصدة والحركات.
const loadFxHistory=async()=>{if(!reval.account)return;setBusy(true);setError("");try{setFxHistory(await treasuryApi.fxRevaluations(Number(reval.account)));setFxHistoryAccount(reval.account);}catch(e){setError((e as Error).message||"تعذر تحميل سجل إعادة التقييم.");}finally{setBusy(false);}};
  const submitRevaluation=async(e:React.FormEvent)=>{e.preventDefault();if(!reval.account)return;setBusy(true);setError("");try{const result=await treasuryApi.revalueAccount(Number(reval.account),{fiscal_year:Number(reval.fiscal_year),revaluation_date:reval.revaluation_date,rate_date:reval.rate_date});await load();setError(result.difference==="0.0000"?"تم تسجيل إعادة التقييم دون فرق محاسبي.":`تمت إعادة التقييم؛ فرق العملة ${money(result.difference)} ${baseCurrencyCode}. ${result.journal_number?`القيد ${result.journal_number}`:""}`);setFxHistory(await treasuryApi.fxRevaluations(Number(reval.account)));setFxHistoryAccount(reval.account);}catch(e){setError((e as Error).message||"تعذرت إعادة التقييم.");}finally{setBusy(false);}};
  const post=async(id:number)=>{setBusy(true);setError("");try{await treasuryApi.postTransaction(id);await load();}catch(e){setError((e as Error).message||"تعذر الترحيل.");}finally{setBusy(false);}};
  // إنشاء جلسة تسوية بنكية مرتبطة بحساب البنك وتاريخ كشف الحساب.
const createRecon=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    const created=await bankReconciliationApi.create({company:companyId,bank_account:Number(rec.bank_account),statement_number:rec.statement_number,statement_date:rec.statement_date,opening_balance:rec.opening_balance,closing_balance:rec.closing_balance});
    await load();setSelectedRecon(String(created.id));setRec(old=>({...old,statement_number:""}));await refreshSummary(String(created.id));
  }catch(e){setError((e as Error).message||"تعذر إنشاء التسوية.");}finally{setBusy(false);}};
  // إضافة سطر من كشف البنك إلى جلسة التسوية الحالية.
const addLine=async(e:React.FormEvent)=>{e.preventDefault();if(!selectedRecon)return;setBusy(true);setError("");try{
    await bankReconciliationApi.addLine(Number(selectedRecon),{line_number:Number(line.line_number),transaction_date:line.transaction_date,description:line.description,reference:line.reference,amount:line.amount});
    setLine(old=>({...old,line_number:String(Number(old.line_number)+1),description:"",reference:"",amount:""}));await refreshSummary(selectedRecon);await load();
  }catch(e){setError((e as Error).message||"تعذر إضافة سطر الكشف.");}finally{setBusy(false);}};
  // استيراد ملف كشف البنك CSV وتحميل سطوره إلى التسوية المختارة.
const importCsv=async()=>{if(!selectedRecon||!csvFile)return;setBusy(true);setError("");try{const result=await importBankStatementCsv(Number(selectedRecon),csvFile);setCsvFile(null);setError(result.message);await refreshSummary(selectedRecon);await load();}catch(e){setError((e as Error).message||"تعذر استيراد كشف البنك.");}finally{setBusy(false);}};
  // مطابقة سطر كشف البنك مع حركة خزينة مسجلة.
const match=async(lineId:number)=>{if(!selectedRecon)return;setBusy(true);setError("");try{await bankReconciliationApi.match(Number(selectedRecon),lineId,Number(matchIds[lineId]));await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذرت المطابقة.");}finally{setBusy(false);}};
  // إلغاء المطابقة الحالية لسطر كشف البنك.
const unmatch=async(lineId:number)=>{if(!selectedRecon)return;setBusy(true);setError("");try{await bankReconciliationApi.unmatch(Number(selectedRecon),lineId);await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذر فك المطابقة.");}finally{setBusy(false);}};
  // اعتماد التسوية البنكية بعد استكمال المطابقات والتحقق من الفروق.
const finalize=async()=>{if(!selectedRecon)return;setBusy(true);setError("");try{await bankReconciliationApi.reconcile(Number(selectedRecon));await load();await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذر اعتماد التسوية.");}finally{setBusy(false);}};
  const active=accounts.filter(a=>a.is_active);
  const bankAccounts=active.filter(a=>a.kind==="bank");
  // حساب الرصيد الجاري لحساب الخزينة من الرصيد الافتتاحي والحركات المرحلة.
const balance=(id:number)=>{const account=accounts.find(a=>a.id===id);if(account&&account.currency_code!==baseCurrencyCode)return Number(account.fx_position_foreign_amount||0);return transactions.filter(t=>t.status==="posted").reduce((n,t)=>n+(t.source_account===id?(t.kind==="receipt"?Number(t.amount):-Number(t.amount)):t.kind==="transfer"&&t.destination_account===id?Number(t.amount):0),Number(account?.opening_balance||0));};
  const currentRecon=reconciliations.find(r=>String(r.id)===selectedRecon);
  // تحديد الحركات المرحلة المرشحة لمطابقة مبلغ سطر كشف البنك.
const eligible=(lineAmount:string)=>{const bank=accounts.find(a=>a.id===currentRecon?.bank_account);const foreign=!!bank&&bank.currency_code!==baseCurrencyCode;return transactions.filter(t=>t.status==="posted"&&currentRecon&&t.transaction_date<=currentRecon.statement_date&&((t.source_account===currentRecon.bank_account&&((t.kind==="receipt"?1:-1)*Number(foreign?t.transaction_amount||0:t.amount)===Number(lineAmount)))||(t.kind==="transfer"&&t.destination_account===currentRecon.bank_account&&Number(foreign?t.transaction_amount||0:t.amount)===Number(lineAmount))));};
  return <section className="work-panel treasury-panel">
    <div className="work-panel-head"><div><h2>الخزينة والبنوك</h2><p>حسابات مستقلة وسندات وتسويات بنكية مرتبطة بالدفاتر</p></div><span className="coming-soon">{accounts.length} حساب</span></div>
    {error&&<div className="error-banner">{error}</div>}
    <div className="module-tabs"><button className={tab==="transactions"?"active":""} onClick={()=>setTab("transactions")}>سندات القبض والصرف والتحويل</button><button className={tab==="accounts"?"active":""} onClick={()=>setTab("accounts")}>الخزائن والحسابات البنكية</button><button className={tab==="reconciliation"?"active":""} onClick={()=>setTab("reconciliation")}>التسوية البنكية</button></div>
    {tab==="accounts"?<>
      <form className="treasury-form" onSubmit={saveAcc}>
        <label>النوع<select value={acc.kind} onChange={e=>setAcc({...acc,kind:e.target.value})}><option value="cash">خزينة نقدية</option><option value="bank">حساب بنكي</option></select></label>
        <label>الكود<input required value={acc.code} onChange={e=>setAcc({...acc,code:e.target.value})}/></label>
        <label>الاسم<input required value={acc.name_ar} onChange={e=>setAcc({...acc,name_ar:e.target.value})}/></label>
        <label>الحساب بدليل الحسابات<select required value={acc.account} onChange={e=>setAcc({...acc,account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select value={acc.branch} onChange={e=>setAcc({...acc,branch:e.target.value})}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>العملة<select value={acc.currency_code} onChange={e=>setAcc({...acc,currency_code:e.target.value})}>{currencies.map(v=><option key={v.id} value={v.code}>{v.code} · {v.name_ar}</option>)}</select></label>
        <label>الرصيد الافتتاحي<input type="number" min="0" step="0.0001" disabled={acc.currency_code!==baseCurrencyCode} value={acc.opening_balance} onChange={e=>setAcc({...acc,opening_balance:e.target.value})}/>{acc.currency_code!==baseCurrencyCode&&<small>ابدأ بصفر؛ الرصيد الافتتاحي الأجنبي يتطلب شاشة افتتاحية موثقة.</small>}</label>
        {acc.kind==="bank"&&<><label>اسم البنك<input value={acc.bank_name} onChange={e=>setAcc({...acc,bank_name:e.target.value})}/></label><label>رقم الحساب<input value={acc.account_number} onChange={e=>setAcc({...acc,account_number:e.target.value})}/></label><label>IBAN<input value={acc.iban} onChange={e=>setAcc({...acc,iban:e.target.value})}/></label></>}
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy}>حفظ الحساب</button></div>
      </form>
      <form className="treasury-form" onSubmit={submitRevaluation}><h3 className="form-wide">إعادة تقييم أرصدة العملات الأجنبية</h3><label>حساب أجنبي<select required value={reval.account} onChange={e=>setReval({...reval,account:e.target.value})}><option value="">اختر الحساب</option>{active.filter(a=>a.currency_code!==baseCurrencyCode).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar} · {a.currency_code}</option>)}</select></label><label>السنة المالية<select required value={reval.fiscal_year} onChange={e=>setReval({...reval,fiscal_year:e.target.value})}><option value="">اختر السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label><label>تاريخ إعادة التقييم<input required type="date" value={reval.revaluation_date} onChange={e=>setReval({...reval,revaluation_date:e.target.value})}/></label><label>تاريخ سعر الصرف المعتمد<input required type="date" value={reval.rate_date} onChange={e=>setReval({...reval,rate_date:e.target.value})}/></label><div className="treasury-form-actions"><button className="primary-button" disabled={busy||!reval.account||!reval.fiscal_year}>تنفيذ إعادة التقييم</button><button type="button" className="subtle-button" disabled={busy||!reval.account} onClick={()=>void loadFxHistory()}>عرض سجل إعادة التقييم</button></div><p className="form-wide">يُنشأ قيد فرق عملة غير محقق تلقائيًا، ولا يمكن تكرار التقييم للحساب في التاريخ نفسه. الحسابات ذات الأرصدة الافتتاحية الأجنبية القديمة تتطلب معالجة افتتاحية أولًا.</p></form>
      <div className="data-table-wrap"><h3>سجل إعادة التقييم {fxHistoryAccount?`· ${accounts.find(a=>String(a.id)===fxHistoryAccount)?.name_ar||""}`:""}</h3><table><thead><tr><th>تاريخ التقييم</th><th>تاريخ السعر</th><th>سعر الصرف</th><th>الرصيد الأجنبي</th><th>القيمة السابقة</th><th>القيمة الجديدة</th><th>الفرق</th><th>القيد</th></tr></thead><tbody>{fxHistory.map(row=><tr key={row.id}><td>{row.revaluation_date}</td><td>{row.rate_date}</td><td>{row.rate}</td><td>{money(row.foreign_amount)} {accounts.find(a=>a.id===Number(fxHistoryAccount))?.currency_code}</td><td>{money(row.previous_carrying_amount)} {baseCurrencyCode}</td><td>{money(row.new_carrying_amount)} {baseCurrencyCode}</td><td>{money(row.difference)} {baseCurrencyCode}</td><td>{row.journal_number||"—"}</td></tr>)}{!fxHistory.length&&<tr><td colSpan={8}>لا يوجد سجل إعادة تقييم لهذا الحساب.</td></tr>}</tbody></table></div>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الاسم</th><th>النوع</th><th>الحساب المحاسبي</th><th>العملة</th><th>افتتاحي</th><th>الرصيد بالعملة</th><th>القيمة الدفترية</th></tr></thead><tbody>{accounts.map(a=><tr key={a.id}><td>{a.code}</td><td>{a.name_ar}{a.bank_name?" · "+a.bank_name:""}</td><td>{a.kind==="cash"?"خزينة":"بنك"}</td><td>{a.account_code} · {a.account_name}</td><td>{a.currency_code}</td><td>{money(a.opening_balance)}</td><td>{money(balance(a.id))} {a.currency_code}</td><td>{a.currency_code!==baseCurrencyCode?`${money(a.fx_position_carrying_base_amount||0)} ${baseCurrencyCode}`:"—"}</td></tr>)}</tbody></table>{!loading&&!accounts.length&&<div className="table-empty">لا توجد حسابات بعد.</div>}</div>
    </>:tab==="transactions"?<>
      <form className="treasury-form" onSubmit={saveDoc}>
        <label>النوع<select value={doc.kind} onChange={e=>setDoc({...doc,kind:e.target.value,destination_account:"",counter_account:""})}><option value="receipt">سند قبض</option><option value="payment">سند صرف</option><option value="transfer">تحويل</option></select></label>
        <label>رقم المستند<input required value={doc.document_number} onChange={e=>setDoc({...doc,document_number:e.target.value})}/></label>
        <label>التاريخ<input required type="date" value={doc.transaction_date} onChange={e=>setDoc({...doc,transaction_date:e.target.value})}/></label>
        <label>الفرع<select required value={doc.branch} onChange={e=>setDoc({...doc,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>السنة المالية<select required value={doc.fiscal_year} onChange={e=>setDoc({...doc,fiscal_year:e.target.value})}><option value="">السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
        <label>الخزينة / البنك<select required value={doc.source_account} onChange={e=>setDoc({...doc,source_account:e.target.value})}><option value="">اختر الحساب</option>{active.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        {doc.kind==="transfer"?<label>إلى حساب<select required value={doc.destination_account} onChange={e=>setDoc({...doc,destination_account:e.target.value})}><option value="">الحساب المستلم</option>{active.filter(a=>String(a.id)!==doc.source_account&&a.currency_code===accounts.find(v=>v.id===Number(doc.source_account))?.currency_code).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>:<label>الحساب المقابل<select required value={doc.counter_account} onChange={e=>setDoc({...doc,counter_account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>}
        {accounts.find(a=>a.id===Number(doc.source_account))?.currency_code!==baseCurrencyCode&&doc.source_account?<><label>المبلغ بالعملة الأجنبية<input required type="number" min="0.0001" step="0.0001" value={doc.transaction_amount} onChange={e=>setDoc({...doc,transaction_amount:e.target.value})}/></label><label>سعر الصرف المعتمد<input required type="number" min="0.0000000001" step="0.0000000001" value={doc.exchange_rate} onChange={e=>setDoc({...doc,exchange_rate:e.target.value})}/></label><label>تاريخ سعر الصرف<input required type="date" value={doc.exchange_rate_date} onChange={e=>setDoc({...doc,exchange_rate_date:e.target.value})}/></label><label>القيمة بالعملة الأساسية<input readOnly value={(Number(doc.transaction_amount)>0&&Number(doc.exchange_rate)>0?(Number(doc.transaction_amount)*Number(doc.exchange_rate)).toFixed(4):"")}/></label></>:<label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={doc.amount} onChange={e=>setDoc({...doc,amount:e.target.value})}/></label>}
        <label className="form-wide">البيان<input required value={doc.description} onChange={e=>setDoc({...doc,description:e.target.value})}/></label>
        <label>المرجع<input value={doc.reference} onChange={e=>setDoc({...doc,reference:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!active.length||!years.some(y=>y.status==="open")}>حفظ كمسودة</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>المستند</th><th>التاريخ</th><th>النوع</th><th>الحساب</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>إجراء</th></tr></thead><tbody>{transactions.map(t=><tr key={t.id}><td>{t.document_number}</td><td>{t.transaction_date}</td><td>{t.kind==="receipt"?"قبض":t.kind==="payment"?"صرف":"تحويل"}</td><td>{t.source_name}{t.destination_name?" ← "+t.destination_name:""}</td><td>{money(t.amount)} {baseCurrencyCode}{t.transaction_amount?` · ${money(t.transaction_amount)} ${currencies.find(v=>v.id===t.transaction_currency)?.code||""}`:""}</td><td><span className={"status-pill "+t.status}>{t.status==="posted"?"مرحّل":"مسودة"}</span></td><td>{t.journal_number||"—"}</td><td>{t.status==="draft"?<button className="subtle-button" disabled={busy} onClick={()=>void post(t.id)}>ترحيل</button>:"—"}</td></tr>)}</tbody></table>{!loading&&!transactions.length&&<div className="table-empty">لا توجد مستندات حتى الآن.</div>}</div>
    </>:<>
      <form className="treasury-form" onSubmit={createRecon}>
        <label>الحساب البنكي<select required value={rec.bank_account} onChange={e=>setRec({...rec,bank_account:e.target.value})}><option value="">اختر البنك</option>{bankAccounts.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>رقم كشف البنك<input required value={rec.statement_number} onChange={e=>setRec({...rec,statement_number:e.target.value})}/></label>
        <label>تاريخ الكشف<input required type="date" value={rec.statement_date} onChange={e=>setRec({...rec,statement_date:e.target.value})}/></label>
        <label>رصيد أول المدة<input required type="number" step="0.0001" value={rec.opening_balance} onChange={e=>setRec({...rec,opening_balance:e.target.value})}/></label>
        <label>رصيد آخر المدة<input required type="number" step="0.0001" value={rec.closing_balance} onChange={e=>setRec({...rec,closing_balance:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!bankAccounts.length}>إنشاء تسوية</button></div>
      </form>
      <div className="treasury-recon-select"><label>التسوية الحالية<select value={selectedRecon} onChange={e=>setSelectedRecon(e.target.value)}><option value="">اختر كشفًا</option>{reconciliations.map(r=><option key={r.id} value={r.id}>{r.bank_name} · {r.statement_number} · {r.statement_date} {r.status==="reconciled"?"(معتمدة)":""}</option>)}</select></label></div>
      {summary&&<>
        <div className="treasury-summary-grid">
          <div><small>رصيد كشف البنك</small><strong>{money(summary.reconciliation.closing_balance)}</strong></div>
          <div><small>رصيد الدفاتر</small><strong>{money(summary.book_balance)}</strong></div>
          <div><small>حركات دفترية معلقة</small><strong>{money(summary.outstanding_total)}</strong></div>
          <div><small>الفرق غير المفسر</small><strong>{money(summary.unexplained_difference)}</strong></div>
        </div>
        {summary.reconciliation.status==="draft"&&<div className="treasury-csv-import"><div><strong>استيراد كشف البنك من CSV</strong><small>UTF-8 · الأعمدة المطلوبة: date, amount · الاختياري: description, reference</small></div><input type="file" accept=".csv,text/csv" onChange={e=>setCsvFile(e.target.files?.[0]||null)}/><button className="primary-button" type="button" disabled={busy||!csvFile||summary.lines.length>0} onClick={()=>void importCsv()}>استيراد الحركات</button>{summary.lines.length>0&&<small>الاستيراد متاح قبل إدخال أي سطور فقط حتى لا يتم استبدال بيانات موجودة.</small>}</div>}
        {summary.reconciliation.status==="draft"&&<form className="treasury-form" onSubmit={addLine}>
          <label>رقم السطر<input required type="number" min="1" value={line.line_number} onChange={e=>setLine({...line,line_number:e.target.value})}/></label>
          <label>تاريخ الحركة<input required type="date" value={line.transaction_date} onChange={e=>setLine({...line,transaction_date:e.target.value})}/></label>
          <label>المبلغ (+ إيداع / - خصم)<input required type="number" step="0.0001" value={line.amount} onChange={e=>setLine({...line,amount:e.target.value})}/></label>
          <label>البيان<input value={line.description} onChange={e=>setLine({...line,description:e.target.value})}/></label>
          <label>المرجع<input value={line.reference} onChange={e=>setLine({...line,reference:e.target.value})}/></label>
          <div className="treasury-form-actions"><button className="primary-button" disabled={busy}>إضافة سطر كشف البنك</button></div>
        </form>}
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>البيان</th><th>المرجع</th><th>المبلغ</th><th>المطابقة</th><th>ربط بحركة دفترية</th><th>إجراء</th></tr></thead><tbody>{summary.lines.map(row=><tr key={row.id}><td>{row.transaction_date}</td><td>{row.description}</td><td>{row.reference}</td><td>{money(row.amount)}</td><td>{row.matched_document||"غير مطابق"}</td><td>{!row.matched_transaction&&summary.reconciliation.status==="draft"?<select value={matchIds[row.id]||""} onChange={e=>setMatchIds({...matchIds,[row.id]:e.target.value})}><option value="">اختر حركة بنفس القيمة والاتجاه</option>{eligible(row.amount).map(t=><option key={t.id} value={t.id}>{t.document_number} · {t.transaction_date} · {money(t.amount)}</option>)}</select>:row.matched_document}</td><td>{row.matched_transaction&&summary.reconciliation.status==="draft"?<button className="subtle-button" disabled={busy} onClick={()=>void unmatch(row.id)}>فك المطابقة</button>:!row.matched_transaction&&summary.reconciliation.status==="draft"?<button className="subtle-button" disabled={busy||!matchIds[row.id]} onClick={()=>void match(row.id)}>مطابقة</button>:"—"}</td></tr>)}</tbody></table></div>
        <div className="treasury-recon-footer"><span>مطابق: {summary.matched_lines} · غير مطابق: {summary.unmatched_lines} · حركات دفترية معلقة: {summary.outstanding_transactions.length}</span>{summary.reconciliation.status==="draft"?<button className="primary-button" disabled={busy||summary.unmatched_lines>0||Number(summary.statement_difference)!==0||Number(summary.unexplained_difference)!==0} onClick={()=>void finalize()}>اعتماد التسوية</button>:<strong>تم اعتماد التسوية بتاريخ {summary.reconciliation.reconciled_at?.slice(0,10)}</strong>}</div>
        {summary.outstanding_transactions.length>0&&<div className="data-table-wrap"><h3>حركات دفترية لم تظهر في كشف البنك بعد</h3><table><thead><tr><th>التاريخ</th><th>المستند</th><th>البيان</th><th>القيمة</th></tr></thead><tbody>{summary.outstanding_transactions.map(t=><tr key={t.id}><td>{t.date}</td><td>{t.document_number}</td><td>{t.description}</td><td>{money(t.amount)}</td></tr>)}</tbody></table></div>}
      </>}
    </>}
  </section>;
}
