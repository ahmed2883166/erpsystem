/*
 * إدارة العملاء: البحث وعرض بيانات العملاء وإنشاءها وتعديلها.
 * تشمل بيانات التعريف والاتصال والحد الائتماني وشروط السداد.
 */
import { useEffect, useMemo, useState } from "react";
import { formatMoney } from "./formatters";
import CustomExcelExportButton from "./CustomExcelExportButton";
import { api, inventoryApi, salesApi, type Customer, type CustomerStatement, type CustomerDiscountRule, type Product, type InventoryCategory } from "./api";

type Props = { companyId: number | null };

const money = (value: string) => formatMoney(value);

// شاشة إدارة العملاء والبحث في السجل وفتح نموذج الإضافة أو التعديل.
export default function CustomersPanel({ companyId }: Props) {
  const [rows, setRows] = useState<Customer[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [statementCustomer, setStatementCustomer] = useState<Customer | null>(null);
  const [statement, setStatement] = useState<CustomerStatement | null>(null);
  const [statementBusy, setStatementBusy] = useState(false);
  const [discountRules, setDiscountRules] = useState<CustomerDiscountRule[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [categories, setCategories] = useState<InventoryCategory[]>([]);
  const [showDiscountRules, setShowDiscountRules] = useState(false);
  const [editingRuleId, setEditingRuleId] = useState<number | null>(null);
  const [ruleSaving, setRuleSaving] = useState(false);
  const [ruleForm, setRuleForm] = useState({customer:"",customer_type:"",product:"",category:"",min_quantity:"",min_invoice_amount:"",discount_percent:"0",priority:"100",effective_from:new Date().toISOString().slice(0,10),effective_to:"",notes:""});
  const [statementFrom, setStatementFrom] = useState(`${new Date().getFullYear()}-01-01`);
  const [statementTo, setStatementTo] = useState(new Date().toISOString().slice(0, 10));
  const [form, setForm] = useState({
    code: "", customer_type: "company" as "company" | "individual",
    name_ar: "", tax_number: "", phone: "", mobile: "", email: "",
    credit_limit: "0", payment_terms_days: "0", default_discount_percent: "0", discount_approval_limit: "", address: "",
  });
  const resetForm = () => {
    setEditingId(null);
    setForm({ code: "", customer_type: "company", name_ar: "", tax_number: "", phone: "", mobile: "", email: "", credit_limit: "0", payment_terms_days: "0", default_discount_percent: "0", discount_approval_limit: "", address: "" });
  };

  // تحميل قائمة العملاء للشركة الحالية وتحديث نتائج العرض.
const refresh = async () => {
    if (!companyId) { setRows([]); return; }
    setLoading(true);
    setError("");
    try { setRows(await api.customers(companyId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل العملاء."); }
    finally { setLoading(false); }
  };

  const refreshDiscountRules = async () => {
    if (!companyId) { setDiscountRules([]); setProducts([]); setCategories([]); return; }
    try {
      const [rules, productRows, categoryRows] = await Promise.all([api.discountRules(companyId), inventoryApi.products(companyId), inventoryApi.categories(companyId)]);
      setDiscountRules(rules); setProducts(productRows); setCategories(categoryRows);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل قواعد الخصم."); }
  };
  useEffect(() => { void refresh(); void refreshDiscountRules(); resetForm(); setShowForm(false); setShowDiscountRules(false); setEditingRuleId(null); }, [companyId]);

  const filtered = useMemo(() => rows.filter((customer) =>
    [customer.code, customer.name_ar, customer.name_en, customer.tax_number, customer.phone, customer.mobile]
      .join(" ").toLocaleLowerCase("ar").includes(search.toLocaleLowerCase("ar"))
  ), [rows, search]);

  // التحقق من نموذج العميل ثم إنشاء السجل أو تحديثه عبر API.
const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!companyId) return;
    setSaving(true);
    setError("");
    try {
      const payload = {
        ...form,
        company: companyId,
        credit_limit: form.credit_limit || "0",
        payment_terms_days: Number(form.payment_terms_days || 0),
        default_discount_percent: form.default_discount_percent || "0",
        discount_approval_limit: form.discount_approval_limit || null,
      };
      if (editingId) await api.updateCustomer(editingId, payload);
      else await api.createCustomer(payload);
      setForm({
        code: "", customer_type: "company", name_ar: "", tax_number: "",
        phone: "", mobile: "", email: "", credit_limit: "0",
        payment_terms_days: "0", default_discount_percent: "0", discount_approval_limit: "", address: "",
      });
      setShowForm(false);
      setEditingId(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ العميل.");
    } finally { setSaving(false); }
  };

  // فتح نموذج تعديل العميل مع تحميل بياناته الحالية.
  const editCustomer = (customer: Customer) => {
    setEditingId(customer.id);
    setForm({
      code: customer.code, customer_type: customer.customer_type, name_ar: customer.name_ar,
      tax_number: customer.tax_number || "", phone: customer.phone || "", mobile: customer.mobile || "",
      email: customer.email || "", credit_limit: customer.credit_limit || "0",
      payment_terms_days: String(customer.payment_terms_days || 0),
      default_discount_percent: customer.default_discount_percent || "0",
      discount_approval_limit: customer.discount_approval_limit || "", address: customer.address || "",
    });
    setShowForm(true);
    setError("");
  };

  // إيقاف العميل أو إعادة تنشيطه دون حذف المستندات السابقة.
  const toggleCustomerActive = async (customer: Customer) => {
    const action = customer.is_active ? "إيقاف" : "إعادة تنشيط";
    if (!window.confirm("هل تريد " + action + " العميل \"" + customer.name_ar + "\"؟ لن تُحذف أي مستندات أو حركات سابقة.")) return;
    setSaving(true);
    setError("");
    try { await api.updateCustomer(customer.id, { is_active: !customer.is_active }); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة العميل."); }
    finally { setSaving(false); }
  };

  // تحميل كشف حركة العميل ورصيد أول وآخر المدة.
  const loadStatement = async (customer: Customer) => {
    if (!companyId) return;
    setStatementCustomer(customer);
    setStatementBusy(true);
    setError("");
    try {
      setStatement(await salesApi.customerStatement(companyId, customer.id, statementFrom, statementTo));
    } catch (reason) {
      setStatement(null);
      setError(reason instanceof Error ? reason.message : "تعذر تحميل كشف حساب العميل.");
    } finally { setStatementBusy(false); }
  };

  // تصدير كشف الحساب الحالي بصيغة CSV متوافقة مع Excel.

  const resetRuleForm = () => {
    setEditingRuleId(null);
    setRuleForm({customer:"",customer_type:"",product:"",category:"",min_quantity:"",min_invoice_amount:"",discount_percent:"0",priority:"100",effective_from:new Date().toISOString().slice(0,10),effective_to:"",notes:""});
  };
  const editDiscountRule = (rule: CustomerDiscountRule) => {
    setEditingRuleId(rule.id);
    setRuleForm({customer:rule.customer?String(rule.customer):"",customer_type:rule.customer_type,product:rule.product?String(rule.product):"",category:rule.category?String(rule.category):"",min_quantity:rule.min_quantity||"",min_invoice_amount:rule.min_invoice_amount||"",discount_percent:rule.discount_percent,priority:String(rule.priority),effective_from:rule.effective_from,effective_to:rule.effective_to||"",notes:rule.notes||""});
  };
  const saveDiscountRule = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!companyId) return;
    setRuleSaving(true); setError("");
    const payload = {company:companyId,customer:ruleForm.customer?Number(ruleForm.customer):null,customer_type:ruleForm.customer_type,product:ruleForm.product?Number(ruleForm.product):null,category:ruleForm.category?Number(ruleForm.category):null,min_quantity:ruleForm.min_quantity||null,min_invoice_amount:ruleForm.min_invoice_amount||null,discount_percent:ruleForm.discount_percent,priority:Number(ruleForm.priority||100),effective_from:ruleForm.effective_from,effective_to:ruleForm.effective_to||null,notes:ruleForm.notes};
    try { if(editingRuleId) await api.updateDiscountRule(editingRuleId,payload); else await api.createDiscountRule(payload); resetRuleForm(); await refreshDiscountRules(); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر حفظ قاعدة الخصم."); }
    finally { setRuleSaving(false); }
  };
  const toggleDiscountRule = async (rule: CustomerDiscountRule) => {
    setRuleSaving(true); setError("");
    try { await api.updateDiscountRule(rule.id,{is_active:!rule.is_active}); await refreshDiscountRules(); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحديث قاعدة الخصم."); }
    finally { setRuleSaving(false); }
  };

  const exportStatement = () => {
    if (!statement) return;
    const values = [
      ["التاريخ", "نوع المستند", "رقم المستند", "البيان", "مدين", "دائن", "الرصيد"],
      ...statement.rows.map(row => [row.date, row.document_type === "invoice" ? "فاتورة" : "سند قبض", row.document_number, row.description, row.debit, row.credit, row.balance]),
    ];
    const safe = (value: string) => {
      const text = String(value ?? "");
      const protectedText = /^[=+@-]/.test(text) || text.startsWith("\t") || text.startsWith("\r") ? "'" + text : text;
      return '"' + protectedText.replace(/"/g, '""') + '"';
    };
    const csv = "\uFEFF" + values.map(row => row.map(value => safe(String(value))).join(";")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `customer-statement-${statement.customer}-${statement.date_from || "all"}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section className="work-panel customers-panel">
      <div className="work-panel-head">
        <div><h2>سجل العملاء</h2><p>بيانات العملاء وحدود الائتمان وشروط السداد للشركة المحددة.</p></div>
        <div className="table-actions">
          <CustomExcelExportButton resource="customers" companyId={companyId} />
          <button className="subtle-button" type="button" disabled={!companyId} onClick={() => setShowDiscountRules(value => !value)}>قواعد الخصم</button>
          <button className="primary-button" type="button" disabled={!companyId} onClick={() => { if(showForm){setShowForm(false);resetForm();}else{resetForm();setShowForm(true);} }}>
            <span>＋</span>{showForm ? "إغلاق النموذج" : "إضافة عميل"}
          </button>
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {!companyId && <div className="table-empty">اختر شركة لعرض سجل العملاء.</div>}
      {showForm && companyId && <form className="customer-form" onSubmit={(event) => void submit(event)}>
        <div className="customer-form-wide"><strong>{editingId ? "تعديل بيانات العميل" : "إضافة عميل جديد"}</strong></div>
        <label>كود العميل<input required maxLength={40} disabled={saving || Boolean(editingId)} value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} placeholder="CUS-001" /></label>
        <label>نوع العميل<select value={form.customer_type} onChange={(event) => setForm({ ...form, customer_type: event.target.value as "company" | "individual" })}><option value="company">شركة</option><option value="individual">فرد</option></select></label>
        <label>اسم العميل<input required value={form.name_ar} onChange={(event) => setForm({ ...form, name_ar: event.target.value })} /></label>
        <label>الرقم الضريبي<input value={form.tax_number} onChange={(event) => setForm({ ...form, tax_number: event.target.value })} /></label>
        <label>الهاتف<input value={form.phone} onChange={(event) => setForm({ ...form, phone: event.target.value })} /></label>
        <label>المحمول<input value={form.mobile} onChange={(event) => setForm({ ...form, mobile: event.target.value })} /></label>
        <label>البريد الإلكتروني<input type="email" value={form.email} onChange={(event) => setForm({ ...form, email: event.target.value })} /></label>
        <label>حد الائتمان<input type="number" min="0" step="0.01" value={form.credit_limit} onChange={(event) => setForm({ ...form, credit_limit: event.target.value })} /></label>
        <label>مدة السداد (يوم)<input type="number" min="0" max="32767" value={form.payment_terms_days} onChange={(event) => setForm({ ...form, payment_terms_days: event.target.value })} /></label>
        <label>الخصم الافتراضي %<input type="number" min="0" max="100" step="0.0001" value={form.default_discount_percent} onChange={(event) => setForm({ ...form, default_discount_percent: event.target.value })} /></label>
        <label>حد الخصم دون موافقة المدير %<input type="number" min="0" max="100" step="0.0001" value={form.discount_approval_limit} onChange={(event) => setForm({ ...form, discount_approval_limit: event.target.value })} placeholder="بدون حد خاص" /></label>
        <label className="customer-form-wide">العنوان<textarea rows={2} value={form.address} onChange={(event) => setForm({ ...form, address: event.target.value })} /></label>
        <div className="customer-form-actions customer-form-wide">
          <button className="subtle-button" type="button" disabled={saving} onClick={() => { setShowForm(false); resetForm(); }}>إلغاء</button>
          <button className="primary-button" type="submit" disabled={saving}>{saving ? "جارٍ الحفظ…" : editingId ? "حفظ التعديلات" : "حفظ العميل"}</button>
        </div>
      </form>}
      {companyId && <div className="customer-toolbar">
        <div className="customer-count">عدد العملاء <strong>{filtered.length.toLocaleString("ar-EG")}</strong></div>
        <input aria-label="بحث في العملاء" placeholder="ابحث بالكود أو الاسم أو الرقم الضريبي…" value={search} onChange={(event) => setSearch(event.target.value)} />
        <button className="subtle-button" type="button" onClick={() => void refresh()} disabled={loading}>{loading ? "تحديث…" : "تحديث القائمة"}</button>
      </div>}
      {companyId && <div className="data-table-wrap"><table>
        <thead><tr><th>الكود</th><th>اسم العميل</th><th>النوع</th><th>الهاتف</th><th>الرقم الضريبي</th><th>حد الائتمان</th><th>السداد</th><th>الحالة</th><th>كشف الحساب</th><th>إجراءات</th></tr></thead>
        <tbody>{filtered.map((customer) => <tr key={customer.id}>
          <td className="code-cell">{customer.code}</td><td>{customer.name_ar}</td>
          <td>{customer.customer_type === "company" ? "شركة" : "فرد"}</td>
          <td>{customer.mobile || customer.phone || "—"}</td><td>{customer.tax_number || "—"}</td>
          <td>{money(customer.credit_limit)}</td><td>{customer.payment_terms_days} يوم</td>
          <td><span className={customer.is_active ? "customer-status-active" : "customer-status-inactive"}>{customer.is_active ? "نشط" : "موقوف"}</span></td>
          <td><button className="text-action-button" type="button" onClick={() => void loadStatement(customer)}>كشف الحساب ←</button></td>
          <td><div className="table-actions"><button className="subtle-button" type="button" disabled={saving} onClick={() => editCustomer(customer)}>تعديل</button><button className="subtle-button" type="button" disabled={saving} onClick={() => void toggleCustomerActive(customer)}>{customer.is_active ? "إيقاف" : "تنشيط"}</button></div></td>
        </tr>)}</tbody>
      </table>{!loading && filtered.length === 0 && <div className="table-empty">لا توجد سجلات مطابقة. أضف أول عميل للشركة.</div>}</div>}

      {showDiscountRules && companyId && <section className="customer-statement">
        <div className="customer-statement-head"><div><h3>قواعد الخصم التجاري</h3><p>قاعدة واحدة فقط لكل بند: الأولوية الأعلى ثم الأكثر تخصيصًا. الخصم اليدوي له الأولوية.</p></div><button className="subtle-button" type="button" onClick={() => {resetRuleForm();setShowDiscountRules(false);}}>إغلاق</button></div>
        <form className="customer-form" onSubmit={(event) => void saveDiscountRule(event)}>
          <div className="customer-form-wide"><strong>{editingRuleId ? "تعديل قاعدة خصم" : "إضافة قاعدة خصم"}</strong></div>
          <label>عميل محدد<select value={ruleForm.customer} onChange={event=>setRuleForm({...ruleForm,customer:event.target.value})}><option value="">كل العملاء المطابقين</option>{rows.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
          <label>نوع العميل<select value={ruleForm.customer_type} onChange={event=>setRuleForm({...ruleForm,customer_type:event.target.value})}><option value="">الكل</option><option value="company">شركة</option><option value="individual">فرد</option></select></label>
          <label>الصنف<select value={ruleForm.product} onChange={event=>setRuleForm({...ruleForm,product:event.target.value,category:""})}><option value="">كل الأصناف</option>{products.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
          <label>مجموعة الأصناف<select value={ruleForm.category} onChange={event=>setRuleForm({...ruleForm,category:event.target.value,product:""})}><option value="">كل المجموعات</option>{categories.map(x=><option key={x.id} value={x.id}>{x.code} — {x.name_ar}</option>)}</select></label>
          <label>أقل كمية<input type="number" min="0.0001" step="0.0001" value={ruleForm.min_quantity} onChange={event=>setRuleForm({...ruleForm,min_quantity:event.target.value})} /></label>
          <label>أقل قيمة فاتورة<input type="number" min="0" step="0.01" value={ruleForm.min_invoice_amount} onChange={event=>setRuleForm({...ruleForm,min_invoice_amount:event.target.value})} /></label>
          <label>نسبة الخصم %<input required type="number" min="0" max="100" step="0.0001" value={ruleForm.discount_percent} onChange={event=>setRuleForm({...ruleForm,discount_percent:event.target.value})} /></label>
          <label>الأولوية<input type="number" min="0" max="32767" value={ruleForm.priority} onChange={event=>setRuleForm({...ruleForm,priority:event.target.value})} /></label>
          <label>ساري من<input required type="date" value={ruleForm.effective_from} onChange={event=>setRuleForm({...ruleForm,effective_from:event.target.value})} /></label>
          <label>ساري حتى<input type="date" value={ruleForm.effective_to} onChange={event=>setRuleForm({...ruleForm,effective_to:event.target.value})} /></label>
          <label className="customer-form-wide">اسم القاعدة / ملاحظة<input maxLength={500} value={ruleForm.notes} onChange={event=>setRuleForm({...ruleForm,notes:event.target.value})} /></label>
          <div className="customer-form-actions customer-form-wide"><button className="subtle-button" type="button" disabled={ruleSaving} onClick={resetRuleForm}>مسح</button><button className="primary-button" type="submit" disabled={ruleSaving}>{ruleSaving?"جارٍ الحفظ…":editingRuleId?"حفظ القاعدة":"إضافة القاعدة"}</button></div>
        </form>
        <div className="data-table-wrap"><table><thead><tr><th>القاعدة</th><th>العميل / الفئة</th><th>الصنف / المجموعة</th><th>الكمية</th><th>قيمة الفاتورة</th><th>الخصم</th><th>الأولوية</th><th>الصلاحية</th><th>الحالة</th><th>إجراءات</th></tr></thead>
          <tbody>{discountRules.map(rule=><tr key={rule.id}><td>{rule.notes || "قاعدة #"+rule.id}</td><td>{rule.customer_name || (rule.customer_type==="company"?"شركات":rule.customer_type==="individual"?"أفراد":"كل العملاء")}</td><td>{rule.product_name || rule.category_name || "كل الأصناف"}</td><td>{rule.min_quantity||"—"}</td><td>{rule.min_invoice_amount||"—"}</td><td>{rule.discount_percent}%</td><td>{rule.priority}</td><td>{rule.effective_from} — {rule.effective_to||"مفتوح"}</td><td>{rule.is_active?"نشطة":"موقوفة"}</td><td><div className="table-actions"><button className="subtle-button" type="button" disabled={ruleSaving} onClick={()=>editDiscountRule(rule)}>تعديل</button><button className="subtle-button" type="button" disabled={ruleSaving} onClick={()=>void toggleDiscountRule(rule)}>{rule.is_active?"إيقاف":"تنشيط"}</button></div></td></tr>)}</tbody>
        </table>{discountRules.length===0 && <div className="table-empty">لا توجد قواعد خصم للشركة.</div>}</div>
      </section>}
      {statementCustomer && <section className="customer-statement">
        <div className="customer-statement-head">
          <div><h3>كشف حساب: {statementCustomer.name_ar}</h3><p>الحركات المرحلة فقط، مع رصيد متحرك حسب التاريخ</p></div>
          <button className="subtle-button" type="button" onClick={() => { setStatementCustomer(null); setStatement(null); }}>إغلاق الكشف</button>
        </div>
        <div className="customer-statement-filters">
          <label>من تاريخ<input type="date" value={statementFrom} onChange={event => setStatementFrom(event.target.value)} /></label>
          <label>إلى تاريخ<input type="date" value={statementTo} onChange={event => setStatementTo(event.target.value)} /></label>
          <button className="primary-button" type="button" disabled={statementBusy || !statementFrom || !statementTo || statementFrom > statementTo} onClick={() => void loadStatement(statementCustomer)}>{statementBusy ? "جارٍ التحميل…" : "عرض الكشف"}</button>
          <button className="subtle-button" type="button" disabled={!statement} onClick={exportStatement}>تصدير Excel (CSV)</button>
        </div>
        {statementBusy && <div className="table-empty">جارٍ تحميل كشف الحساب…</div>}
        {statement && <>
          <div className="customer-statement-summary">
            <div><span>رصيد أول المدة</span><strong>{money(statement.opening_balance)}</strong></div>
            <div><span>رصيد آخر المدة</span><strong>{money(statement.closing_balance)}</strong></div>
            <div><span>عدد الحركات</span><strong>{statement.rows.length.toLocaleString("ar-EG")}</strong></div>
          </div>
          <div className="data-table-wrap"><table>
            <thead><tr><th>التاريخ</th><th>نوع المستند</th><th>رقم المستند</th><th>البيان</th><th>مدين</th><th>دائن</th><th>الرصيد</th></tr></thead>
            <tbody>{statement.rows.map((row, index) => <tr key={`${row.document_type}-${row.document_number}-${index}`}>
              <td>{row.date}</td><td>{row.document_type === "invoice" ? "فاتورة مبيعات" : "سند قبض"}</td><td>{row.document_number}</td>
              <td>{row.description}</td><td>{money(row.debit)}</td><td>{money(row.credit)}</td><td>{money(row.balance)}</td>
            </tr>)}</tbody>
          </table>{statement.rows.length === 0 && <div className="table-empty">لا توجد حركات خلال الفترة المحددة.</div>}</div>
        </>}
      </section>}
    </section>
  );
}
