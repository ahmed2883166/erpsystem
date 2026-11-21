import { useCallback, useEffect, useState } from "react";
import { api, treasuryApi, type Account, type Branch, type FiscalYear, type TreasuryAccount, type TreasuryTransaction } from "./api";

type Props = { companyId: number };
const today = () => new Date().toISOString().slice(0, 10);
const money = (v: string | number) => Number(v || 0).toLocaleString("ar-EG", {maximumFractionDigits:2});

export default function TreasuryPanel({companyId}: Props) {
  const [tab,setTab]=useState<"transactions"|"accounts">("transactions");
  const [accounts,setAccounts]=useState<TreasuryAccount[]>([]);
  const [transactions,setTransactions]=useState<TreasuryTransaction[]>([]);
  const [branches,setBranches]=useState<Branch[]>([]);
  const [years,setYears]=useState<FiscalYear[]>([]);
  const [gl,setGl]=useState<Account[]>([]);
  const [loading,setLoading]=useState(true), [busy,setBusy]=useState(false), [error,setError]=useState("");
  const [acc,setAcc]=useState({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:"EGP"});
  const [doc,setDoc]=useState({branch:"",fiscal_year:"",source_account:"",destination_account:"",counter_account:"",kind:"receipt",document_number:"",transaction_date:today(),amount:"",description:"",reference:""});
  const load=useCallback(async()=>{
    setLoading(true);setError("");
    try {
      const [a,t,b,y,g]=await Promise.all([treasuryApi.accounts(companyId),treasuryApi.transactions(companyId),api.branches(companyId),api.fiscalYears(companyId),api.accounts(companyId)]);
      setAccounts(a);setTransactions(t);setBranches(b);setYears(y);setGl(g);
      setDoc(old=>({...old,branch:old.branch||String(b[0]?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||""),source_account:old.source_account||String(a.find(v=>v.is_active)?.id||"")}));
    } catch(e){setError((e as Error).message||"تعذر تحميل بيانات الخزينة.");} finally{setLoading(false);}
  },[companyId]);
  useEffect(()=>{void load();},[load]);
  const saveAcc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    await treasuryApi.createAccount({company:companyId,branch:acc.branch?Number(acc.branch):null,account:Number(acc.account),kind:acc.kind,code:acc.code,name_ar:acc.name_ar,bank_name:acc.bank_name,account_number:acc.account_number,iban:acc.iban,currency_code:acc.currency_code});
    setAcc({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:"EGP",opening_balance:"0"});await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ الحساب.");}finally{setBusy(false);}};
  const saveDoc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    await treasuryApi.createTransaction({company:companyId,branch:Number(doc.branch),fiscal_year:Number(doc.fiscal_year),source_account:Number(doc.source_account),destination_account:doc.kind==="transfer"?Number(doc.destination_account):null,counter_account:doc.kind==="transfer"?null:Number(doc.counter_account),kind:doc.kind,document_number:doc.document_number,transaction_date:doc.transaction_date,amount:doc.amount,description:doc.description,reference:doc.reference});
    setDoc(old=>({...old,document_number:"",amount:"",description:"",reference:""}));await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ المستند.");}finally{setBusy(false);}};
  const post=async(id:number)=>{setBusy(true);setError("");try{await treasuryApi.postTransaction(id);await load();}catch(e){setError((e as Error).message||"تعذر الترحيل.");}finally{setBusy(false);}};
  const active=accounts.filter(a=>a.is_active);
  const balance=(id:number)=>transactions.filter(t=>t.status==="posted").reduce((n,t)=>n+(t.source_account===id?(t.kind==="receipt"?Number(t.amount):-Number(t.amount)):t.kind==="transfer"&&t.destination_account===id?Number(t.amount):0),Number(accounts.find(a=>a.id===id)?.opening_balance||0));
  return <section className="work-panel treasury-panel">
    <div className="work-panel-head"><div><h2>الخزينة والبنوك</h2><p>حسابات مستقلة وسندات مرتبطة بقيود اليومية</p></div><span className="coming-soon">{accounts.length} حساب</span></div>
    {error&&<div className="error-banner">{error}</div>}
    <div className="module-tabs"><button className={tab==="transactions"?"active":""} onClick={()=>setTab("transactions")}>سندات القبض والصرف والتحويل</button><button className={tab==="accounts"?"active":""} onClick={()=>setTab("accounts")}>الخزائن والحسابات البنكية</button></div>
    {tab==="accounts"?<>
      <form className="treasury-form" onSubmit={saveAcc}>
        <label>النوع<select value={acc.kind} onChange={e=>setAcc({...acc,kind:e.target.value})}><option value="cash">خزينة نقدية</option><option value="bank">حساب بنكي</option></select></label>
        <label>الكود<input required value={acc.code} onChange={e=>setAcc({...acc,code:e.target.value})}/></label>
        <label>الاسم<input required value={acc.name_ar} onChange={e=>setAcc({...acc,name_ar:e.target.value})}/></label>
        <label>الحساب بدليل الحسابات<select required value={acc.account} onChange={e=>setAcc({...acc,account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select value={acc.branch} onChange={e=>setAcc({...acc,branch:e.target.value})}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>العملة<input maxLength={3} value={acc.currency_code} onChange={e=>setAcc({...acc,currency_code:e.target.value.toUpperCase()})}/></label>
        {acc.kind==="bank"&&<><label>اسم البنك<input value={acc.bank_name} onChange={e=>setAcc({...acc,bank_name:e.target.value})}/></label><label>رقم الحساب<input value={acc.account_number} onChange={e=>setAcc({...acc,account_number:e.target.value})}/></label><label>IBAN<input value={acc.iban} onChange={e=>setAcc({...acc,iban:e.target.value})}/></label></>}
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy}>حفظ الحساب</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الاسم</th><th>النوع</th><th>الحساب المحاسبي</th><th>العملة</th><th>افتتاحي</th><th>الرصيد بالحركات</th></tr></thead><tbody>{accounts.map(a=><tr key={a.id}><td>{a.code}</td><td>{a.name_ar}{a.bank_name?" · "+a.bank_name:""}</td><td>{a.kind==="cash"?"خزينة":"بنك"}</td><td>{a.account_code} · {a.account_name}</td><td>{a.currency_code}</td><td>{money(a.opening_balance)}</td><td>{money(balance(a.id))}</td></tr>)}</tbody></table>{!loading&&!accounts.length&&<div className="table-empty">لا توجد حسابات بعد.</div>}</div>
    </>:<>
      <form className="treasury-form" onSubmit={saveDoc}>
        <label>النوع<select value={doc.kind} onChange={e=>setDoc({...doc,kind:e.target.value,destination_account:"",counter_account:""})}><option value="receipt">سند قبض</option><option value="payment">سند صرف</option><option value="transfer">تحويل</option></select></label>
        <label>رقم المستند<input required value={doc.document_number} onChange={e=>setDoc({...doc,document_number:e.target.value})}/></label>
        <label>التاريخ<input required type="date" value={doc.transaction_date} onChange={e=>setDoc({...doc,transaction_date:e.target.value})}/></label>
        <label>الفرع<select required value={doc.branch} onChange={e=>setDoc({...doc,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>السنة المالية<select required value={doc.fiscal_year} onChange={e=>setDoc({...doc,fiscal_year:e.target.value})}><option value="">السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
        <label>الخزينة / البنك<select required value={doc.source_account} onChange={e=>setDoc({...doc,source_account:e.target.value})}><option value="">اختر الحساب</option>{active.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        {doc.kind==="transfer"?<label>إلى حساب<select required value={doc.destination_account} onChange={e=>setDoc({...doc,destination_account:e.target.value})}><option value="">الحساب المستلم</option>{active.filter(a=>String(a.id)!==doc.source_account).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>:<label>الحساب المقابل<select required value={doc.counter_account} onChange={e=>setDoc({...doc,counter_account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>}
        <label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={doc.amount} onChange={e=>setDoc({...doc,amount:e.target.value})}/></label>
        <label className="form-wide">البيان<input required value={doc.description} onChange={e=>setDoc({...doc,description:e.target.value})}/></label>
        <label>المرجع<input value={doc.reference} onChange={e=>setDoc({...doc,reference:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!active.length||!years.some(y=>y.status==="open")}>حفظ كمسودة</button></div>
      </form>
      <div className="data-table-wrap"><table><thead><tr><th>المستند</th><th>التاريخ</th><th>النوع</th><th>الحساب</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>إجراء</th></tr></thead><tbody>{transactions.map(t=><tr key={t.id}><td>{t.document_number}</td><td>{t.transaction_date}</td><td>{t.kind==="receipt"?"قبض":t.kind==="payment"?"صرف":"تحويل"}</td><td>{t.source_name}{t.destination_name?" ← "+t.destination_name:""}</td><td>{money(t.amount)}</td><td><span className={"status-pill "+t.status}>{t.status==="posted"?"مرحّل":"مسودة"}</span></td><td>{t.journal_number||"—"}</td><td>{t.status==="draft"?<button className="subtle-button" disabled={busy} onClick={()=>void post(t.id)}>ترحيل</button>:"—"}</td></tr>)}</tbody></table>{!loading&&!transactions.length&&<div className="table-empty">لا توجد مستندات حتى الآن.</div>}</div>
    </>}
  </section>;
}
