import { useEffect, useState, type FormEvent } from "react";
import { formatMoney } from "./formatters";
import { inventoryApi, type InventoryValuationAssessment, type Product, type Warehouse } from "./api";

type Props = { companyId: number; products: Product[]; warehouses: Warehouse[] };

export default function InventoryNRVPanel({ companyId, products, warehouses }: Props) {
  const [rows, setRows] = useState<InventoryValuationAssessment[]>([]);
  const [form, setForm] = useState({
    product: "", warehouse: "", assessment_number: "",
    assessment_date: new Date().toISOString().slice(0, 10),
    net_realizable_value_per_unit: "", notes: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = async () => {
    const data = await inventoryApi.valuationAssessments(companyId);
    setRows(data);
  };

  useEffect(() => {
    void refresh().catch((reason) => setError(reason instanceof Error ? reason.message : "تعذر تحميل تقييمات NRV."));
  }, [companyId]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.product || !form.warehouse || !form.assessment_number.trim()
      || form.net_realizable_value_per_unit === "" || Number(form.net_realizable_value_per_unit) < 0) {
      setError("أكمل الصنف والمخزن ورقم التقييم والتاريخ وصافي القيمة البيعية للوحدة.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const draft = await inventoryApi.createValuationAssessment({
        company: companyId,
        product: Number(form.product),
        warehouse: Number(form.warehouse),
        assessment_number: form.assessment_number.trim(),
        assessment_date: form.assessment_date,
        net_realizable_value_per_unit: form.net_realizable_value_per_unit,
        notes: form.notes,
      });
      await inventoryApi.postValuationAssessment(draft.id);
      setForm((old) => ({ ...old, assessment_number: "", net_realizable_value_per_unit: "", notes: "" }));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر ترحيل تقييم NRV.");
    } finally {
      setBusy(false);
    }
  };

  const reverse = async (row: InventoryValuationAssessment) => {
    const entryDate = window.prompt("تاريخ عكس التقييم (YYYY-MM-DD)", new Date().toISOString().slice(0, 10));
    if (!entryDate) return;
    setBusy(true);
    setError("");
    try {
      await inventoryApi.reverseValuationAssessment(row.id, entryDate);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر عكس التقييم.");
    } finally {
      setBusy(false);
    }
  };

  return <div className="inventory-reports">
    <section className="activity-panel inventory-report-section">
      <div className="activity-title"><div>
        <h3>تقييم المخزون بصافي القيمة البيعية — IAS 2</h3>
        <p>يقارن النظام تكلفة الرصيد بصافي القيمة البيعية للوحدة، ويثبت التخفيض أو عكسه الجزئي حتى حد التكلفة الأصلية مع قيد يومية.</p>
      </div></div>
      <form className="inventory-form" onSubmit={(event) => void submit(event)}>
        <label>الصنف المخزني<select required value={form.product} onChange={(event) => setForm({ ...form, product: event.target.value })}>
          <option value="">اختر الصنف</option>
          {products.filter((item) => item.is_active && item.product_type === "goods").map((item) =>
            <option key={item.id} value={item.id}>{item.code} — {item.name_ar}</option>)}
        </select></label>
        <label>المخزن<select required value={form.warehouse} onChange={(event) => setForm({ ...form, warehouse: event.target.value })}>
          <option value="">اختر المخزن</option>
          {warehouses.filter((item) => item.is_active).map((item) =>
            <option key={item.id} value={item.id}>{item.code} — {item.name_ar}</option>)}
        </select></label>
        <label>رقم التقييم<input required value={form.assessment_number} onChange={(event) => setForm({ ...form, assessment_number: event.target.value })} /></label>
        <label>تاريخ التقييم<input required type="date" value={form.assessment_date} onChange={(event) => setForm({ ...form, assessment_date: event.target.value })} /></label>
        <label>صافي القيمة البيعية للوحدة<input required type="number" min="0" step="0.000001" value={form.net_realizable_value_per_unit} onChange={(event) => setForm({ ...form, net_realizable_value_per_unit: event.target.value })} /></label>
        <label>ملاحظات<input value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} /></label>
        <div className="inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الترحيل…" : "إنشاء وترحيل تقييم NRV"}</button></div>
      </form>
      {error && <div className="error-banner">{error}</div>}
    </section>
    <section className="activity-panel inventory-report-section">
      <div className="activity-title"><div>
        <h3>سجل تقييمات المخزون</h3>
        <p>التقييمات المرحّلة قابلة للعكس فقط إذا لم تحدث حركة مخزنية أو تقييم أحدث بعدها.</p>
      </div></div>
      <div className="data-table-wrap"><table><thead><tr>
        <th>رقم التقييم</th><th>التاريخ</th><th>الصنف</th><th>المخزن</th><th>الكمية</th>
        <th>قبل</th><th>بعد</th><th>التخفيض / العكس</th><th>القيد</th><th>الحالة</th><th>إجراء</th>
      </tr></thead><tbody>
        {rows.map((row) => <tr key={row.id}>
          <td className="code-cell">{row.assessment_number}</td><td>{row.assessment_date}</td>
          <td>{row.product_code} — {row.product_name}</td><td>{row.warehouse_name}</td><td>{row.quantity}</td>
          <td>{formatMoney(row.carrying_value_before)}</td><td>{formatMoney(row.carrying_value_after)}</td>
          <td>{formatMoney(row.adjustment_amount)}</td><td>{row.journal_number || "—"}</td>
          <td>{row.status === "posted" ? "مرحّل" : row.status === "reversed" ? "معكوس" : "مسودة"}</td>
          <td>{row.status === "posted" ? <button className="subtle-button" type="button" disabled={busy} onClick={() => void reverse(row)}>عكس</button> : "—"}</td>
        </tr>)}
        {!rows.length && <tr><td colSpan={11}>لا توجد تقييمات مخزون مسجلة.</td></tr>}
      </tbody></table></div>
    </section>
  </div>;
}
