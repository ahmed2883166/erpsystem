import { useEffect, useMemo, useState, type FormEvent } from "react";
import { formatMoney } from "./formatters";
import { salesApi, type CustomerRefund, type SalesInvoice, type SalesReturn, type CustomerReceiptBatch, type TreasuryAccount } from "./api";

type Props = {
  companyId: number;
  invoices: SalesInvoice[];
  returns: SalesReturn[];
  receiptBatches: CustomerReceiptBatch[];
  treasuryAccounts: TreasuryAccount[];
  baseCurrencyCode: string;
  onRefresh: () => Promise<void>;
};

const today = () => new Date().toISOString().slice(0, 10);
const money = (value: string) => formatMoney(value);

export default function CustomerRefundsPanel({
  companyId, invoices, returns, receiptBatches, treasuryAccounts, baseCurrencyCode, onRefresh,
}: Props) {
  const [rows, setRows] = useState<CustomerRefund[]>([]);
  const [sourceKind, setSourceKind] = useState<"return" | "advance">("return");
  const [sourceReturn, setSourceReturn] = useState("");
  const [sourceBatch, setSourceBatch] = useState("");
  const [treasuryAccount, setTreasuryAccount] = useState("");
  const [refundNumber, setRefundNumber] = useState("");
  const [refundDate, setRefundDate] = useState(today());
  const [amount, setAmount] = useState("");
  const [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refundedByReturn = useMemo(() => {
    const totals = new Map<number, number>();
    for (const row of rows) {
      if (row.source_return !== null && row.status === "posted") {
        totals.set(row.source_return, (totals.get(row.source_return) || 0) + Number(row.amount));
      }
    }
    return totals;
  }, [rows]);
  const availableReturns = useMemo(
    () => returns.filter(row => row.status === "posted"
      && Number(row.grand_total) - (refundedByReturn.get(row.id) || 0) > 0),
    [returns, refundedByReturn],
  );
  const availableBatches = useMemo(
    () => receiptBatches.filter(row => row.status === "posted" && Number(row.unallocated_amount) > 0),
    [receiptBatches],
  );
  const selectedReturn = availableReturns.find(row => row.id === Number(sourceReturn));
  const selectedBatch = availableBatches.find(row => row.id === Number(sourceBatch));
  const selectedInvoice = selectedReturn ? invoices.find(row => row.id === selectedReturn.invoice) : undefined;
  const selectedReturnAvailable = selectedReturn
    ? Math.max(Number(selectedReturn.grand_total) - (refundedByReturn.get(selectedReturn.id) || 0), 0)
    : 0;
  const customerId = selectedReturn ? selectedInvoice?.customer : selectedBatch?.customer;

  const refresh = async () => {
    const result = await salesApi.refunds(companyId);
    setRows(result);
  };

  useEffect(() => {
    void refresh().catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل ردود العملاء."));
  }, [companyId]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
      await onRefresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تنفيذ العملية.");
    } finally {
      setBusy(false);
    }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!customerId) {
      setError("تعذر تحديد العميل من المستند الأصلي.");
      return;
    }
    await run(async () => {
      await salesApi.createRefund({
        company: companyId,
        customer: customerId,
        refund_number: refundNumber.trim(),
        refund_date: refundDate,
        ...(sourceKind === "return"
          ? { source_return: Number(sourceReturn) }
          : { source_receipt_batch: Number(sourceBatch) }),
        treasury_account: Number(treasuryAccount),
        amount,
        reference,
      });
      setRefundNumber("");
      setAmount("");
      setReference("");
    });
  };

  const sourceLabel = (row: CustomerRefund) =>
    row.source_return_number
      ? `مردود مبيعات ${row.source_return_number}`
      : `دفعة مقدمة ${row.source_receipt_number || "—"}`;

  return <div className="inventory-section">
    <div className="work-panel-head">
      <div>
        <h3>رد مبالغ العملاء</h3>
        <p>صرف قيمة مردود المبيعات أو رد جزء من الدفعة المقدمة من الخزينة أو البنك.</p>
      </div>
      <button type="button" className="subtle-button" disabled={busy} onClick={() => void run(refresh)}>تحديث</button>
    </div>
    {error && <div className="error-banner">{error}</div>}
    <form className="inventory-form" onSubmit={create}>
      <label>نوع الرد
        <select value={sourceKind} onChange={e => { setSourceKind(e.target.value as "return" | "advance"); setSourceReturn(""); setSourceBatch(""); }}>
          <option value="return">رد قيمة مردود مبيعات</option>
          <option value="advance">رد جزء من دفعة مقدمة</option>
        </select>
      </label>
      {sourceKind === "return" ? <label>مردود المبيعات
        <select required value={sourceReturn} onChange={e => setSourceReturn(e.target.value)}>
          <option value="">اختر مردودًا مرحّلًا</option>
          {availableReturns.map(row => <option key={row.id} value={row.id}>
            {row.return_number} · {row.customer_name} · المتبقي من قيمة المردود {money(String(Math.max(Number(row.grand_total) - (refundedByReturn.get(row.id) || 0), 0)))}
          </option>)}
        </select>
      </label> : <label>الدفعة المقدمة
        <select required value={sourceBatch} onChange={e => setSourceBatch(e.target.value)}>
          <option value="">اختر دفعة مقدمة مرحّلة</option>
          {availableBatches.map(row => <option key={row.id} value={row.id}>
            {row.receipt_number} · {row.customer_name} · المتاح {money(row.unallocated_amount)}
          </option>)}
        </select>
      </label>}
      <label>حساب الصرف
        <select required value={treasuryAccount} onChange={e => setTreasuryAccount(e.target.value)}>
          <option value="">اختر الخزينة أو البنك</option>
          {treasuryAccounts.filter(row => row.is_active && row.currency_code === baseCurrencyCode).map(row => <option key={row.id} value={row.id}>
            {row.name_ar} · {row.kind === "cash" ? "خزينة" : "بنك"} · {row.currency_code}
          </option>)}
        </select>
      </label>
      <label>رقم سند الرد
        <input required maxLength={40} value={refundNumber} onChange={e => setRefundNumber(e.target.value)} />
      </label>
      <label>تاريخ الرد
        <input required type="date" value={refundDate} onChange={e => setRefundDate(e.target.value)} />
      </label>
      <label>المبلغ ({baseCurrencyCode || "عملة الشركة الأساسية"})
        <input required type="number" min="0.01" step="0.01" value={amount} onChange={e => setAmount(e.target.value)} />
      </label>
      <label>مرجع / ملاحظات
        <input maxLength={120} value={reference} onChange={e => setReference(e.target.value)} />
      </label>
      <div className="inventory-form-actions inventory-wide">
        <button className="primary-button" type="submit" disabled={busy || !customerId || !treasuryAccount || !refundNumber.trim() || !amount}>
          {busy ? "جارٍ الحفظ…" : "إنشاء مسودة رد"}
        </button>
      </div>
      {selectedReturn && <p className="inventory-report-note inventory-wide">العميل: {selectedReturn.customer_name} · المتبقي القابل للرد بالعملة الأساسية: {money(String(selectedReturnAvailable))}. الصرف يقتصر على الرصيد الدائن الصافي للعميل بعد احتساب جميع فواتيره وتحصيلاته ومردوداته.</p>}
      {selectedBatch && <p className="inventory-report-note inventory-wide">العميل: {selectedBatch.customer_name} · المتاح غير المخصص حاليًا: {money(selectedBatch.unallocated_amount)}.</p>}
    </form>
    <div className="data-table-wrap">
      <table>
        <thead><tr><th>رقم السند</th><th>العميل</th><th>المصدر</th><th>التاريخ</th><th>حساب الصرف</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead>
        <tbody>{rows.map(row => <tr key={row.id}>
          <td className="code-cell">{row.refund_number}</td>
          <td>{row.customer_name}</td>
          <td>{sourceLabel(row)}</td>
          <td>{row.refund_date}</td>
          <td>{row.treasury_account_name}</td>
          <td>{money(row.amount)}</td>
          <td><span className={`status-pill ${row.status === "posted" ? "posted" : row.status === "reversed" ? "reversed" : "draft"}`}>
            {row.status === "posted" ? "مرحّل" : row.status === "reversed" ? "معكوس" : "مسودة"}
          </span></td>
          <td>{row.status === "reversed" ? row.reversal_journal_number || "—" : row.journal_number || "—"}</td>
          <td><div className="table-actions">
            {row.status === "draft" && <button type="button" className="primary-button" disabled={busy} onClick={() => void run(() => salesApi.postRefund(row.id))}>صرف وترحيل</button>}
            {row.status === "posted" && <button type="button" className="subtle-button" disabled={busy} onClick={() => {
              if (window.confirm("سيتم إثبات قبض مقابل من نفس الخزينة وعكس أثر رد المبلغ على حساب العميل. هل تريد المتابعة؟")) {
                void run(() => salesApi.reverseRefund(row.id));
              }
            }}>عكس الرد</button>}
            {row.status === "reversed" && <span>تم العكس</span>}
          </div></td>
        </tr>)}</tbody>
      </table>
      {!busy && rows.length === 0 && <div className="table-empty">لا توجد عمليات رد مبالغ للعملاء.</div>}
    </div>
  </div>;
}
