import { useEffect, useState } from "react";
import {
  stockApi,
  type Product,
  type StockTransfer,
  type StockTransferInTransit,
  type Warehouse,
} from "./api";

type DraftLine = { product: string; quantity: string; expiry_date: string };

const today = () => new Date().toISOString().slice(0, 10);
const statusLabel = (status: StockTransfer["status"]) => ({
  draft: "مسودة",
  in_transit: "بضاعة بالطريق",
  partially_received: "استلام جزئي",
  received: "مكتمل الاستلام",
  cancelled: "ملغي",
}[status]);

export default function StockTransfersPanel({
  companyId,
  products,
  warehouses,
}: {
  companyId: number;
  products: Product[];
  warehouses: Warehouse[];
}) {
  const [transfers, setTransfers] = useState<StockTransfer[]>([]);
  const [inTransit, setInTransit] = useState<StockTransferInTransit[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyTransfer, setBusyTransfer] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [receiptDate, setReceiptDate] = useState(today());
  const [receiveQuantities, setReceiveQuantities] = useState<Record<number, string>>({});
  const [form, setForm] = useState({
    transfer_number: "",
    transfer_date: today(),
    source_warehouse: "",
    destination_warehouse: "",
    notes: "",
  });
  const [lines, setLines] = useState<DraftLine[]>([{ product: "", quantity: "1", expiry_date: "" }]);

  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      const [allTransfers, openTransfers] = await Promise.all([
        stockApi.transfers(companyId),
        stockApi.inTransitTransfers(companyId),
      ]);
      setTransfers(allTransfers);
      setInTransit(openTransfers);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل تحويلات المخزون.");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, [companyId]);

  const createTransfer = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await stockApi.createTransfer({
        company: companyId,
        transfer_number: form.transfer_number.trim(),
        transfer_date: form.transfer_date,
        source_warehouse: Number(form.source_warehouse),
        destination_warehouse: Number(form.destination_warehouse),
        notes: form.notes,
        lines: lines.map((line) => ({
          product: Number(line.product),
          quantity: line.quantity,
          expiry_date: line.expiry_date || null,
        })),
      });
      setNotice("تم حفظ مسودة التحويل. راجعها ثم نفّذ الشحن.");
      setShowCreate(false);
      setForm({ transfer_number: "", transfer_date: today(), source_warehouse: "", destination_warehouse: "", notes: "" });
      setLines([{ product: "", quantity: "1", expiry_date: "" }]);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ التحويل.");
    } finally {
      setBusy(false);
    }
  };

  const dispatch = async (transfer: StockTransfer) => {
    setBusyTransfer(transfer.id);
    setError("");
    setNotice("");
    try {
      await stockApi.dispatchTransfer(transfer.id);
      setNotice(`تم شحن التحويل ${transfer.transfer_number} وإثبات الكمية في البضاعة بالطريق.`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر شحن التحويل.");
    } finally {
      setBusyTransfer(null);
    }
  };

  const receive = async (transfer: StockTransferInTransit) => {
    const receiptLines = transfer.lines
      .map((line) => ({ transfer_line: line.transfer_line, quantity: (receiveQuantities[line.transfer_line] || "").trim() }))
      .filter((line) => line.quantity && Number(line.quantity) > 0);
    if (!receiptLines.length) {
      setError("أدخل كمية واحدة على الأقل للاستلام.");
      return;
    }
    setBusyTransfer(transfer.id);
    setError("");
    setNotice("");
    try {
      const receipt = await stockApi.receiveTransfer(transfer.id, {
        receipt_date: receiptDate,
        lines: receiptLines,
      });
      setNotice(`تم تسجيل الاستلام ${receipt.receipt_number}. المتبقي يظل في البضاعة بالطريق.`);
      setReceiveQuantities((current) => {
        const next = { ...current };
        transfer.lines.forEach((line) => { delete next[line.transfer_line]; });
        return next;
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تسجيل الاستلام.");
    } finally {
      setBusyTransfer(null);
    }
  };

  const activeProducts = products.filter((item) => item.product_type === "goods" && item.is_active);
  const activeWarehouses = warehouses.filter((item) => item.is_active);
  const updateLine = (index: number, patch: Partial<DraftLine>) => {
    setLines((current) => current.map((line, lineIndex) => lineIndex === index ? { ...line, ...patch } : line));
  };

  return (
    <div className="inventory-reports">
      <section className="activity-panel inventory-report-section">
        <div className="activity-title">
          <div>
            <h3>التحويلات بين الفروع</h3>
            <p>الشحن ينقل الكمية إلى مخزن البضاعة بالطريق، والاستلام يضيف فقط الكمية التي وصلت فعليًا إلى مخزن الوجهة.</p>
          </div>
          <div className="inventory-actions">
            <button type="button" className="primary-button" onClick={() => setShowCreate((value) => !value)}>
              {showCreate ? "إغلاق" : "＋ تحويل جديد"}
            </button>
            <button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>↻ تحديث</button>
          </div>
        </div>
        {error && <div className="error-banner">{error}</div>}
        {notice && <div className="success-banner">{notice}</div>}
        {showCreate && (
          <form className="inventory-form" onSubmit={createTransfer}>
            <label>رقم التحويل<input required maxLength={40} value={form.transfer_number} onChange={(event) => setForm({ ...form, transfer_number: event.target.value })} /></label>
            <label>تاريخ التحويل<input type="date" required value={form.transfer_date} onChange={(event) => setForm({ ...form, transfer_date: event.target.value })} /></label>
            <label>مخزن المصدر<select required value={form.source_warehouse} onChange={(event) => setForm({ ...form, source_warehouse: event.target.value })}><option value="">اختر مخزن المصدر</option>{activeWarehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.code} - {warehouse.name_ar}</option>)}</select></label>
            <label>مخزن الوجهة<select required value={form.destination_warehouse} onChange={(event) => setForm({ ...form, destination_warehouse: event.target.value }}><option value="">اختر مخزن الوجهة</option>{activeWarehouses.filter((warehouse) => String(warehouse.id) !== form.source_warehouse).map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.code} - {warehouse.name_ar}</option>)}</select></label>
            <div className="inventory-wide inventory-line-editor">
              <div className="inventory-line-heading"><strong>أصناف التحويل</strong><button type="button" className="add-line-button" onClick={() => setLines((current) => [...current, { product: "", quantity: "1", expiry_date: "" }])}>＋ إضافة صنف</button></div>
              <div className="data-table-wrap"><table><thead><tr><th>#</th><th>الصنف</th><th>الكمية المشحونة</th><th>الصلاحية</th><th>إجراء</th></tr></thead><tbody>
                {lines.map((line, index) => {
                  const product = activeProducts.find((item) => String(item.id) === line.product);
                  return <tr key={index}>
                    <td>{index + 1}</td>
                    <td><select required value={line.product} onChange={(event) => updateLine(index, { product: event.target.value, expiry_date: "" })}><option value="">اختر الصنف</option>{activeProducts.map((item) => <option key={item.id} value={item.id}>{item.code} - {item.name_ar}</option>)}</select></td>
                    <td><input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(event) => updateLine(index, { quantity: event.target.value })} /></td>
                    <td>{product?.track_expiry ? <input required type="date" value={line.expiry_date} onChange={(event) => updateLine(index, { expiry_date: event.target.value })} /> : "—"}</td>
                    <td><button type="button" className="remove-line-button" disabled={lines.length === 1} onClick={() => setLines((current) => current.filter((_, lineIndex) => lineIndex !== index))}>×</button></td>
                  </tr>;
                })}
              </tbody></table></div>
            </div>
            <label className="inventory-wide">ملاحظات<textarea value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} /></label>
            <div className="inventory-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ المسودة"}</button><button className="subtle-button" type="button" onClick={() => setShowCreate(false)}>إلغاء</button></div>
          </form>
        )}
      </section>

      <section className="activity-panel inventory-report-section">
        <div className="activity-title"><div><h3>مستندات التحويل</h3><p>راجع المسودات وحالة كل تحويل وعدد مرات الاستلام.</p></div></div>
        <div className="data-table-wrap"><table>
          <thead><tr><th>رقم التحويل</th><th>التاريخ</th><th>من مخزن</th><th>إلى مخزن</th><th>الأصناف</th><th>الاستلامات</th><th>الحالة</th><th>الإجراء</th></tr></thead>
          <tbody>{transfers.map((transfer) => <tr key={transfer.id}>
            <td className="code-cell">{transfer.transfer_number}</td><td>{transfer.transfer_date}</td>
            <td>{transfer.source_warehouse_name}</td><td>{transfer.destination_warehouse_name}</td>
            <td>{transfer.lines.length}</td><td>{transfer.receipts_count}</td>
            <td><span className={transfer.status === "received" ? "status-pill posted" : transfer.status === "partially_received" ? "status-pill draft" : "status-pill"}>{statusLabel(transfer.status)}</span></td>
            <td>{transfer.status === "draft" ? <button type="button" className="primary-button" disabled={busyTransfer === transfer.id} onClick={() => void dispatch(transfer)}>{busyTransfer === transfer.id ? "جارٍ الشحن…" : "شحن التحويل"}</button> : "—"}</td>
          </tr>)}
          {!transfers.length && <tr><td colSpan={8}>{busy ? "جارٍ تحميل التحويلات…" : "لا توجد تحويلات مسجلة بعد."}</td></tr>}</tbody>
        </table></div>
      </section>

      <section className="activity-panel inventory-report-section">
        <div className="activity-title"><div><h3>البضاعة بالطريق والاستلام الجزئي</h3><p>أدخل الكمية التي وصلت الآن فقط؛ لا تُدخل الكمية المتبقية إلا عند وصولها.</p></div></div>
        <div className="stock-card-filters"><label>تاريخ الاستلام<input type="date" value={receiptDate} onChange={(event) => setReceiptDate(event.target.value)} /></label></div>
        {inTransit.map((transfer) => <article className="activity-panel inventory-report-section" key={transfer.id}>
          <div className="activity-title"><div><h4>{transfer.transfer_number}</h4><p>{transfer.source_warehouse} ← {transfer.destination_warehouse} · {statusLabel(transfer.status)}</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>المشحون</th><th>المستلم</th><th>المتبقي بالطريق</th><th>كمية الاستلام الآن</th></tr></thead><tbody>
            {transfer.lines.map((line) => <tr key={line.transfer_line}>
              <td>{line.product_code} - {line.product_name}</td>
              <td>{Number(line.dispatched_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} {line.unit}</td>
              <td>{Number(line.received_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} {line.unit}</td>
              <td><strong>{Number(line.remaining_quantity).toLocaleString("ar-EG", { maximumFractionDigits: 4 })} {line.unit}</strong></td>
              <td><input aria-label={`كمية استلام ${line.product_code}`} type="number" min="0" max={line.remaining_quantity} step="0.0001" placeholder="0" value={receiveQuantities[line.transfer_line] || ""} onChange={(event) => setReceiveQuantities((current) => ({ ...current, [line.transfer_line]: event.target.value }))} /></td>
            </tr>)}
          </tbody></table></div>
          <div className="inventory-actions"><button type="button" className="primary-button" disabled={busyTransfer === transfer.id} onClick={() => void receive(transfer)}>{busyTransfer === transfer.id ? "جارٍ تسجيل الاستلام…" : "تسجيل الاستلام"}</button></div>
        </article>)}
        {!inTransit.length && <div className="table-empty">لا توجد كميات في عهدة البضاعة بالطريق حاليًا.</div>}
      </section>
    </div>
  );
}
