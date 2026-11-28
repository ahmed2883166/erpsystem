import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, salesApi, type Branch, type Customer, type CustomerReceiptBatch, type CustomerRefund, type SalesInvoice, type TreasuryAccount } from "./api";
import { formatMoney } from "./formatters";

type AllocationDraft = { invoice:string; amount:string };
type PendingAllocation = { batchId:number; invoice:string; amount:string; allocation_date:string };
type Props = {
  companyId:number|null;
  customers:Customer[];
  invoices:SalesInvoice[];
  treasuryAccounts:TreasuryAccount[];
  onRefresh:()=>Promise<void>;
  onPrint:(batch:CustomerReceiptBatch)=>void;
};
const today = () => {
  const value = new Date();
  return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;
};
const money = (value:string) => formatMoney(value);

export default function CustomerReceiptBatchesPanel({
  companyId,customers,invoices,treasuryAccounts,onRefresh,onPrint,
}:Props) {
  const [branches,setBranches] = useState<Branch[]>([]);
  const [batches,setBatches] = useState<CustomerReceiptBatch[]>([]);
  const [refunds,setRefunds] = useState<CustomerRefund[]>([]);
  const [refundForm,setRefundForm] = useState<{batchId:number;refund_number:string;refund_date:string;amount:string;treasury_account:string}|null>(null);
  const [form,setForm] = useState({
    customer:"",branch:"",receipt_number:"",receipt_date:today(),
    method:"bank" as "cash"|"bank",payment_account:"",amount:"",reference:"",
  });
  const [allocations,setAllocations] = useState<AllocationDraft[]>([]);
  const [pendingAllocation,setPendingAllocation] = useState<PendingAllocation|null>(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");

  const load = async () => {
    if(!companyId)return;
    const [batchRows,branchRows,refundRows] = await Promise.all([
      salesApi.receiptBatches(companyId),api.branches(companyId),salesApi.refunds(companyId),
    ]);
    setBatches(batchRows);setBranches(branchRows);setRefunds(refundRows);
  };
  useEffect(()=>{
    let active=true;
    if(!companyId){setBatches([]);setBranches([]);return;}
    Promise.all([salesApi.receiptBatches(companyId),api.branches(companyId),salesApi.refunds(companyId)]).then(([batchRows,branchRows,refundRows])=>{
      if(active){setBatches(batchRows);setBranches(branchRows);setRefunds(refundRows);}
    }).catch(reason=>{if(active)setError(reason instanceof Error?reason.message:"تعذر تحميل سندات القبض المجمعة.");});
    return ()=>{active=false;};
  },[companyId]);

  const eligibleInvoices = useMemo(()=>invoices.filter(invoice=>
    invoice.status==="posted"
    && !invoice.transaction_currency
    && Number(invoice.balance_due||0)>0
    && (!form.customer || invoice.customer===Number(form.customer))
  ),[invoices,form.customer]);
  const selectedAllocations = allocations.reduce((sum,row)=>sum+Number(row.amount||0),0);
  const unallocated = Math.max(Number(form.amount||0)-selectedAllocations,0);

  const createBatch = async (event:FormEvent) => {
    event.preventDefault();
    if(!companyId)return;
    setBusy(true);setError("");
    try{
      await salesApi.createReceiptBatch({
        company:companyId,customer:Number(form.customer),branch:Number(form.branch),
        receipt_number:form.receipt_number,receipt_date:form.receipt_date,
        method:form.method,payment_account:form.payment_account?Number(form.payment_account):null,
        amount:form.amount,reference:form.reference,
        allocations:allocations.filter(row=>row.invoice&&Number(row.amount)>0).map(row=>({
          invoice:Number(row.invoice),amount:row.amount,
        })),
      });
      setForm(old=>({...old,customer:"",receipt_number:"",amount:"",reference:""}));
      setAllocations([]);
      await load();await onRefresh();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر إنشاء سند القبض المجمع.");}
    finally{setBusy(false);}
  };

  const postBatch = async (batch:CustomerReceiptBatch) => {
    setBusy(true);setError("");
    try{await salesApi.postReceiptBatch(batch.id);await load();await onRefresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر ترحيل سند القبض.");}
    finally{setBusy(false);}
  };
  const reverseBatch = async (batch:CustomerReceiptBatch) => {
    if(!window.confirm("سيتم عكس قيد سند القبض وإلغاء كل تخصيصاته على الفواتير. هل تريد المتابعة؟"))return;
    setBusy(true);setError("");
    try{await salesApi.reverseReceiptBatch(batch.id);await load();await onRefresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر عكس سند القبض.");}
    finally{setBusy(false);}
  };
  const allocateAdvance = async (event:FormEvent) => {
    event.preventDefault();
    if(!pendingAllocation)return;
    setBusy(true);setError("");
    try{
      await salesApi.allocateReceiptBatch(
        pendingAllocation.batchId,Number(pendingAllocation.invoice),pendingAllocation.amount,pendingAllocation.allocation_date,
      );
      setPendingAllocation(null);await load();await onRefresh();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر تخصيص الدفعة المقدمة.");}
    finally{setBusy(false);}
  };

  const createRefund = async (event:FormEvent) => {
    event.preventDefault();
    if(!companyId||!refundForm)return;
    const batch=batches.find(row=>row.id===refundForm.batchId);
    if(!batch)return;
    setBusy(true);setError("");
    try{
      await salesApi.createRefund({
        company:companyId,customer:batch.customer,branch:batch.branch,
        source_receipt_batch:batch.id,refund_number:refundForm.refund_number,
        refund_date:refundForm.refund_date,amount:refundForm.amount,
        treasury_account:Number(refundForm.treasury_account),
      });
      setRefundForm(null);await load();await onRefresh();
    }catch(reason){setError(reason instanceof Error?reason.message:"تعذر إنشاء مردود العميل.");}
    finally{setBusy(false);}
  };
  const postRefund = async (refund:CustomerRefund) => {
    setBusy(true);setError("");
    try{await salesApi.postRefund(refund.id);await load();await onRefresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر ترحيل مردود العميل.");}
    finally{setBusy(false);}
  };
  const reverseRefund = async (refund:CustomerRefund) => {
    if(!window.confirm("سيتم عكس قيد مردود العميل وإعادة المبلغ إلى رصيده المقدم. هل تريد المتابعة؟"))return;
    setBusy(true);setError("");
    try{await salesApi.reverseRefund(refund.id);await load();await onRefresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر عكس مردود العميل.");}
    finally{setBusy(false);}
  };

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>التحصيل المجمع والدفعات المقدمة</h2><p>سند واحد يوزع على عدة فواتير، والجزء غير المخصص يبقى رصيدًا مقدمًا للعميل.</p></div></div>
    {!companyId&&<div className="table-empty">اختر شركة لعرض التحصيلات.</div>}
    {companyId&&<form className="inventory-form" onSubmit={createBatch}>
      <label>العميل<select required value={form.customer} onChange={event=>{setForm(old=>({...old,customer:event.target.value}));setAllocations([]);}}><option value="">اختر العميل</option>{customers.filter(row=>row.is_active).map(row=><option key={row.id} value={row.id}>{row.code} · {row.name_ar}</option>)}</select></label>
      <label>الفرع<select required value={form.branch} onChange={event=>setForm(old=>({...old,branch:event.target.value}))}><option value="">اختر الفرع</option>{branches.filter(row=>row.is_active).map(row=><option key={row.id} value={row.id}>{row.code} · {row.name_ar}</option>)}</select></label>
      <label>رقم سند القبض<input required maxLength={40} value={form.receipt_number} onChange={event=>setForm(old=>({...old,receipt_number:event.target.value}))}/></label>
      <label>تاريخ السند<input required type="date" value={form.receipt_date} onChange={event=>setForm(old=>({...old,receipt_date:event.target.value}))}/></label>
      <label>إجمالي المبلغ<input required type="number" min="0.0001" step="0.0001" value={form.amount} onChange={event=>setForm(old=>({...old,amount:event.target.value}))}/></label>
      <label>طريقة التحصيل<select value={form.method} onChange={event=>setForm(old=>({...old,method:event.target.value as "cash"|"bank",payment_account:""}))}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label className="inventory-wide">حساب الخزينة / البنك<select value={form.payment_account} onChange={event=>setForm(old=>({...old,payment_account:event.target.value}))}><option value="">الحساب الافتراضي حسب الطريقة</option>{treasuryAccounts.filter(row=>row.is_active&&row.kind===form.method&&(!row.branch||row.branch===Number(form.branch))).map(row=><option key={row.id} value={row.account}>{row.code} - {row.name_ar} ({row.currency_code})</option>)}</select></label>
      <label className="inventory-wide">مرجع التحويل / الإيصال<input value={form.reference} onChange={event=>setForm(old=>({...old,reference:event.target.value}))}/></label>
      <div className="inventory-line-editor inventory-wide">
        <div className="inventory-line-heading"><strong>توزيع المبلغ على الفواتير (اختياري)</strong><button type="button" className="subtle-button" disabled={!form.customer} onClick={()=>setAllocations(old=>[...old,{invoice:"",amount:""}])}>إضافة فاتورة</button></div>
        {allocations.map((row,index)=><div className="inventory-line-row" key={index}>
          <label>الفاتورة<select value={row.invoice} onChange={event=>{const invoice=eligibleInvoices.find(item=>item.id===Number(event.target.value));setAllocations(old=>old.map((item,i)=>i===index?{...item,invoice:event.target.value,amount:invoice?.balance_due||""}:item));}}><option value="">اختر فاتورة أساسية مستحقة</option>{eligibleInvoices.map(invoice=><option key={invoice.id} value={invoice.id}>{invoice.invoice_number} · متبقي {money(invoice.balance_due)}</option>)}</select></label>
          <label>المبلغ المخصص<input type="number" min="0.0001" step="0.0001" value={row.amount} onChange={event=>setAllocations(old=>old.map((item,i)=>i===index?{...item,amount:event.target.value}:item))}/></label>
          <button type="button" className="subtle-button" onClick={()=>setAllocations(old=>old.filter((_,i)=>i!==index))}>حذف</button>
        </div>)}
        <p className="inventory-report-note">إجمالي المخصص: {money(String(selectedAllocations))} · المتبقي كدفعة مقدمة: {money(String(unallocated))}</p>
      </div>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy||!form.customer||!form.branch||!form.amount}>{busy?"جارٍ الحفظ…":"حفظ سند القبض كمسودة"}</button></div>
    </form>}
    {error&&<div className="error-banner">{error}</div>}

    {refundForm&&<form className="inventory-form" onSubmit={createRefund}>
      <h3 className="inventory-wide">مردود دفعة مقدمة · {batches.find(row=>row.id===refundForm.batchId)?.receipt_number}</h3>
      <label>رقم المردود<input required maxLength={40} value={refundForm.refund_number} onChange={event=>setRefundForm(old=>old?{...old,refund_number:event.target.value}:old)}/></label>
      <label>تاريخ المردود<input required type="date" value={refundForm.refund_date} min={batches.find(row=>row.id===refundForm.batchId)?.receipt_date} onChange={event=>setRefundForm(old=>old?{...old,refund_date:event.target.value}:old)}/></label>
      <label>المبلغ<input required type="number" min="0.0001" max={batches.find(row=>row.id===refundForm.batchId)?.unallocated_amount||"0"} step="0.0001" value={refundForm.amount} onChange={event=>setRefundForm(old=>old?{...old,amount:event.target.value}:old)}/></label>
      <label>حساب الصرف<select required value={refundForm.treasury_account} onChange={event=>setRefundForm(old=>old?{...old,treasury_account:event.target.value}:old)}><option value="">اختر الخزينة / البنك</option>{treasuryAccounts.filter(row=>row.is_active&&(!row.branch||row.branch===batches.find(item=>item.id===refundForm.batchId)?.branch)).map(row=><option key={row.id} value={row.id}>{row.code} - {row.name_ar} ({row.kind==="cash"?"خزينة":"بنك"})</option>)}</select></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!refundForm.treasury_account||Number(refundForm.amount)<=0}>حفظ مردود العميل كمسودة</button><button type="button" className="subtle-button" onClick={()=>setRefundForm(null)}>إلغاء</button></div>
    </form>}
    <div className="data-table-wrap"><table><thead><tr><th>رقم المردود</th><th>العميل</th><th>سند القبض الأصلي</th><th>التاريخ</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>
      {refunds.map(refund=><tr key={refund.id}><td className="code-cell">{refund.refund_number}</td><td>{refund.customer_name}</td><td>{refund.source_receipt_number}</td><td>{refund.refund_date}</td><td>{money(refund.amount)}</td><td>{refund.status==="reversed"?"معكوس":refund.status==="posted"?"مرحّل":"مسودة"}</td><td>{refund.status==="reversed"?refund.reversal_journal_number||"—":refund.journal_number||"—"}</td><td><div className="table-actions">{refund.status==="draft"&&<button type="button" className="primary-button" disabled={busy} onClick={()=>void postRefund(refund)}>ترحيل</button>}{refund.status==="posted"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void reverseRefund(refund)}>عكس</button>}</div></td></tr>)}
    </tbody></table>{!refunds.length&&<div className="table-empty">لا توجد مردودات دفعات مقدمة.</div>}</div>
    {pendingAllocation&&<form className="inventory-form" onSubmit={allocateAdvance}>
      <h3 className="inventory-wide">تخصيص دفعة مقدمة · {batches.find(row=>row.id===pendingAllocation.batchId)?.receipt_number}</h3>
      <label>الفاتورة<select required value={pendingAllocation.invoice} onChange={event=>setPendingAllocation(old=>old?{...old,invoice:event.target.value}:old)}><option value="">اختر فاتورة مستحقة</option>{invoices.filter(invoice=>invoice.customer===batches.find(row=>row.id===pendingAllocation.batchId)?.customer&&invoice.status==="posted"&&!invoice.transaction_currency&&Number(invoice.balance_due||0)>0).map(invoice=><option key={invoice.id} value={invoice.id}>{invoice.invoice_number} · متبقي {money(invoice.balance_due)}</option>)}</select></label>
      <label>تاريخ التخصيص<input required type="date" value={pendingAllocation.allocation_date} onChange={event=>setPendingAllocation(old=>old?{...old,allocation_date:event.target.value}:old)}/></label>
      <label>مبلغ التخصيص<input required type="number" min="0.0001" step="0.0001" value={pendingAllocation.amount} onChange={event=>setPendingAllocation(old=>old?{...old,amount:event.target.value}:old)}/></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy||!pendingAllocation.invoice}>حفظ التخصيص</button><button className="subtle-button" type="button" onClick={()=>setPendingAllocation(null)}>إلغاء</button></div>
    </form>}
    <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>العميل</th><th>التاريخ</th><th>المبلغ</th><th>المخصص</th><th>دفعة مقدمة</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>
      {batches.map(batch=><tr key={batch.id}>
        <td className="code-cell">{batch.receipt_number}<details className="aging-invoice-details"><summary>التخصيصات ({batch.allocation_lines.length})</summary><div className="aging-invoice-list">{batch.allocation_lines.map(row=><div key={row.id}><span>{row.invoice_number}</span><strong>{money(row.amount)}</strong></div>)}</div></details></td>
        <td>{batch.customer_name}</td><td>{batch.receipt_date}</td><td>{money(batch.amount)}</td><td>{money(batch.allocated_amount)}</td><td>{money(batch.unallocated_amount)}</td>
        <td>{batch.status==="reversed"?"معكوس":batch.status==="posted"?"مرحّل":"مسودة"}</td>
        <td>{batch.status==="reversed"?batch.reversal_journal_number||"—":batch.journal_number||"—"}</td>
        <td><div className="table-actions">
          <button type="button" className="subtle-button" onClick={()=>onPrint(batch)}>معاينة / طباعة</button>
          {batch.status==="draft"&&<button type="button" className="primary-button" disabled={busy} onClick={()=>void postBatch(batch)}>ترحيل</button>}
          {batch.status==="posted"&&<><button type="button" className="subtle-button" disabled={busy||Number(batch.unallocated_amount)<=0} onClick={()=>setPendingAllocation({batchId:batch.id,invoice:"",amount:batch.unallocated_amount,allocation_date:today()})}>تخصيص دفعة</button><button type="button" className="subtle-button" disabled={busy||Number(batch.unallocated_amount)<=0} onClick={()=>setRefundForm({batchId:batch.id,refund_number:"",refund_date:today(),amount:batch.unallocated_amount,treasury_account:""})}>رد مبلغ</button><button type="button" className="subtle-button" disabled={busy} onClick={()=>void reverseBatch(batch)}>عكس</button></>}
        </div></td>
      </tr>)}
    </tbody></table>{!batches.length&&<div className="table-empty">لا توجد سندات قبض مجمعة.</div>}</div>
  </section>;
}
