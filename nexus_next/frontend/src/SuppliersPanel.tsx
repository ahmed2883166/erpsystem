/*
 * إدارة الموردين: البحث وعرض بيانات الموردين وإنشاءها وتعديلها.
 * تشمل بيانات التعريف والاتصال والحد الائتماني وشروط السداد.
 */
import { useEffect, useMemo, useState } from "react";
import { formatMoney } from "./formatters";
import CustomExcelExportButton from "./CustomExcelExportButton";
import { api, type Supplier } from "./api";

type Props = { companyId: number | null };
const money = (value: string) => formatMoney(value);

// شاشة إدارة الموردين والبحث في السجل وفتح نموذج الإضافة أو التعديل.
export default function SuppliersPanel({ companyId }: Props) {
  const [rows, setRows] = useState<Supplier[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState({
    code: "", supplier_type: "company" as "company" | "individual",
    name_ar: "", tax_number: "", phone: "", mobile: "", email: "",
    credit_limit: "0", payment_terms_days: "0", address: "",
  });
  const resetForm = () => {
    setEditingId(null);
    setForm({ code: "", supplier_type: "company", name_ar: "", tax_number: "", phone: "", mobile: "", email: "", credit_limit: "0", payment_terms_days: "0", address: "" });
  };

  // تحميل قائمة الموردين للشركة الحالية وتحديث نتائج العرض.
const refresh = async () => {
    if (!companyId) { setRows([]); return; }
    setLoading(true); setError("");
    try { setRows(await api.suppliers(companyId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل الموردين."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); resetForm(); setShowForm(false); }, [companyId]);

  const filtered = useMemo(() => rows.filter((supplier) =>
    [supplier.code, supplier.name_ar, supplier.name_en, supplier.tax_number, supplier.phone, supplier.mobile]
      .join(" ").toLocaleLowerCase("ar").includes(search.toLocaleLowerCase("ar"))
  ), [rows, search]);

  // التحقق من نموذج المورد ثم إنشاء السجل أو تحديثه عبر API.
const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!companyId) return;
    setSaving(true); setError("");
    try {
      const payload = { ...form, company: companyId, credit_limit: form.credit_limit || "0", payment_terms_days: Number(form.payment_terms_days || 0) };
      if (editingId) await api.updateSupplier(editingId, payload);
      else await api.createSupplier(payload);
      setForm({ code: "", supplier_type: "company", name_ar: "", tax_number: "", phone: "", mobile: "", email: "", credit_limit: "0", payment_terms_days: "0", address: "" });
      setShowForm(false); setEditingId(null); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ المورد."); }
    finally { setSaving(false); }
  };

  // فتح نموذج تعديل المورد مع تحميل بياناته الحالية.
  const editSupplier = (supplier: Supplier) => {
    setEditingId(supplier.id);
    setForm({
      code: supplier.code, supplier_type: supplier.supplier_type, name_ar: supplier.name_ar,
      tax_number: supplier.tax_number || "", phone: supplier.phone || "", mobile: supplier.mobile || "",
      email: supplier.email || "", credit_limit: supplier.credit_limit || "0",
      payment_terms_days: String(supplier.payment_terms_days || 0), address: supplier.address || "",
    });
    setShowForm(true);
    setError("");
  };

  // إيقاف المورد أو إعادة تنشيطه دون حذف المستندات السابقة.
  const toggleSupplierActive = async (supplier: Supplier) => {
    const action = supplier.is_active ? "إيقاف" : "إعادة تنشيط";
    if (!window.confirm("هل تريد " + action + " المورد \"" + supplier.name_ar + "\"؟ لن تُحذف أي مستندات أو حركات سابقة.")) return;
    setSaving(true);
    setError("");
    try { await api.updateSupplier(supplier.id, { is_active: !supplier.is_active }); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة المورد."); }
    finally { setSaving(false); }
  };

  return (
    <section className="work-panel customers-panel">
      <div className="work-panel-head">
        <div><h2>سجل الموردين</h2><p>بيانات الموردين والرقم الضريبي وحدود الائتمان وشروط السداد.</p></div>
        <CustomExcelExportButton resource="suppliers" companyId={companyId} />
        <button className="primary-button" type="button" disabled={!companyId} onClick={() => {
          if (showForm) { setShowForm(false); resetForm(); }
          else { resetForm(); setShowForm(true); }
        }}><span>＋</span>{showForm ? "إغلاق النموذج" : "إضافة مورد"}</button>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!companyId && <div className="table-empty">اختر شركة لعرض سجل الموردين.</div>}
      {showForm && companyId && <form className="customer-form" onSubmit={(event) => void submit(event)}>
        <div className="customer-form-wide"><strong>{editingId ? "تعديل بيانات المورد" : "إضافة مورد جديد"}</strong></div>
        <label>كود المورد<input required maxLength={40} disabled={saving || Boolean(editingId)} value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} placeholder="SUP-001" /></label>
        <label>نوع المورد<select value={form.supplier_type} onChange={(event) => setForm({ ...form, supplier_type: event.target.value as "company" | "individual" })}><option value="company">شركة</option><option value="individual">فرد</option></select></label>
        <label>اسم المورد<input required value={form.name_ar} onChange={(event) => setForm({ ...form, name_ar: event.target.value })} /></label>
        <label>الرقم الضريبي<input value={form.tax_number} onChange={(event) => setForm({ ...form, tax_number: event.target.value })} /></label>
        <label>الهاتف<input value={form.phone} onChange={(event) => setForm({ ...form, phone: event.target.value })} /></label>
        <label>المحمول<input value={form.mobile} onChange={(event) => setForm({ ...form, mobile: event.target.value })} /></label>
        <label>البريد الإلكتروني<input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label>
        <label>حد الائتمان<input type="number" min="0" step="0.01" value={form.credit_limit} onChange={(event) => setForm({ ...form, credit_limit: event.target.value })} /></label>
        <label>مدة السداد (يوم)<input type="number" min="0" max="32767" value={form.payment_terms_days} onChange={(event) => setForm({ ...form, payment_terms_days: event.target.value })} /></label>
        <label className="customer-form-wide">العنوان<textarea rows={2} value={form.address} onChange={(event) => setForm({ ...form, address: event.target.value })} /></label>
        <div className="customer-form-actions customer-form-wide"><button className="subtle-button" type="button" disabled={saving} onClick={() => { setShowForm(false); resetForm(); }}>إلغاء</button><button className="primary-button" type="submit" disabled={saving}>{saving ? "جارٍ الحفظ…" : editingId ? "حفظ التعديلات" : "حفظ المورد"}</button></div>
      </form>}
      {companyId && <div className="customer-toolbar">
        <div className="customer-count">عدد الموردين <strong>{filtered.length.toLocaleString("ar-EG")}</strong></div>
        <input aria-label="بحث في الموردين" placeholder="ابحث بالكود أو الاسم أو الرقم الضريبي…" value={search} onChange={(event) => setSearch(event.target.value)} />
        <button className="subtle-button" type="button" onClick={() => void refresh()} disabled={loading}>{loading ? "تحديث…" : "تحديث القائمة"}</button>
      </div>}
      {companyId && <div className="data-table-wrap"><table>
        <thead><tr><th>الكود</th><th>اسم المورد</th><th>النوع</th><th>الهاتف</th><th>الرقم الضريبي</th><th>حد الائتمان</th><th>السداد</th><th>الحالة</th><th>إجراءات</th></tr></thead>
        <tbody>{filtered.map((supplier) => <tr key={supplier.id}>
          <td className="code-cell">{supplier.code}</td><td>{supplier.name_ar}</td><td>{supplier.supplier_type === "company" ? "شركة" : "فرد"}</td>
          <td>{supplier.mobile || supplier.phone || "—"}</td><td>{supplier.tax_number || "—"}</td><td>{money(supplier.credit_limit)}</td><td>{supplier.payment_terms_days} يوم</td>
          <td><span className={supplier.is_active ? "customer-status-active" : "customer-status-inactive"}>{supplier.is_active ? "نشط" : "موقوف"}</span></td>
          <td><div className="table-actions"><button className="subtle-button" type="button" disabled={saving} onClick={() => editSupplier(supplier)}>تعديل</button><button className="subtle-button" type="button" disabled={saving} onClick={() => void toggleSupplierActive(supplier)}>{supplier.is_active ? "إيقاف" : "تنشيط"}</button></div></td>
        </tr>)}</tbody>
      </table>{!loading && filtered.length === 0 && <div className="table-empty">لا توجد سجلات مطابقة. أضف أول مورد للشركة.</div>}</div>}
    </section>
  );
}
