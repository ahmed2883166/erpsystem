import { useEffect, useMemo, useState } from "react";
import { api, inventoryApi, purchasingApi, type Product, type PurchaseInvoice, type PurchaseReceipt, type Supplier, type SupplierPayment, type SupplierStatement, type Warehouse } from "./api";

type Props = { companyId: number | null };
type ReceiptDraftLine = { product: string; quantity: string; unit_cost: string; expiry_date: string };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

export default function PurchasingPanel({ companyId }: Props) {
  const [tab, setTab] = useState<"receipts" | "invoices" | "payments" | "statement">("receipts");
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [receipts, setReceipts] = useState<PurchaseReceipt[]>([]);
  const [invoices, setInvoices] = useState<PurchaseInvoice[]>([]);
  const [payments, setPayments] = useState<SupplierPayment[]>([]);
  const [statementSupplier, setStatementSupplier] = useState("");
  const [statementFrom, setStatementFrom] = useState("");
  const [statementTo, setStatementTo] = useState("");
  const [statement, setStatement] = useState<SupplierStatement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const [receiptForm, setReceiptForm] = useState({ supplier: "", warehouse: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), supplier_delivery_ref: "", notes: "" });
  const [receiptLines, setReceiptLines] = useState<ReceiptDraftLine[]>([{ product: "", quantity: "1", unit_cost: "0", expiry_date: "" }]);
  const [invoiceForm, setInvoiceForm] = useState({ receipt: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), tax_rate: "0", notes: "" });
  const [paymentForm, setPaymentForm] = useState({ invoice: "", payment_number: "", payment_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", amount: "", reference: "" });

  const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [s, p, w, r, i, pay] = await Promise.all([
        api.suppliers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId),
        purchasingApi.receipts(companyId), purchasingApi.invoices(companyId), purchasingApi.payments(companyId),
      ]);
      setSuppliers(s.filter((x) => x.is_active)); setProducts(p.filter((x) => x.is_active && x.product_type === "goods"));
      setWarehouses(w.filter((x) => x.is_active)); setReceipts(r); setInvoices(i); setPayments(pay);
      setReceiptForm((old) => ({ ...old, supplier: old.supplier || String(s.find((x) => x.is_active)?.id || ""), warehouse: old.warehouse || String(w.find((x) => x.is_active)?.id || "") }));
      setReceiptLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? String(p.find((x) => x.is_active && x.product_type === "goods")?.id || "") : "") })));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المشتريات."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  const paymentableInvoices = useMemo(() => invoices.filter((invoice) => invoice.status === "posted" && Number(invoice.balance_due) > 0), [invoices]);
  const invoiceableReceipts = useMemo(() => receipts.filter((receipt) =>
    receipt.movement_status === "posted" && !invoices.some((invoice) => invoice.receipt === receipt.id)
  ), [receipts, invoices]);
  const filteredReceipts = receipts.filter((row) => [row.receipt_number, row.supplier_name, row.warehouse_name, row.supplier_delivery_ref].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredInvoices = invoices.filter((row) => [row.invoice_number, row.supplier_name, row.receipt_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredPayments = payments.filter((row) => [row.payment_number, row.supplier_name, row.invoice_number, row.reference].join(" ").toLowerCase().includes(search.toLowerCase()));

  const loadStatement = async () => {
    if (!companyId || !statementSupplier) { setError("اختر المورد لعرض كشف حسابه."); return; }
    if (statementFrom && statementTo && statementFrom > statementTo) { setError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية."); return; }
    setBusy(true); setError("");
    try {
      const result = await purchasingApi.supplierStatement(companyId, Number(statementSupplier), statementFrom, statementTo);
      setStatement(result);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل كشف حساب المورد."); }
    finally { setBusy(false); }
  };

  const saveReceipt = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      if (!receiptForm.supplier || !receiptForm.warehouse || !receiptLines.length || receiptLines.some((line) => !line.product || Number(line.quantity) <= 0 || Number(line.unit_cost) < 0)) throw new Error("راجع المورد والمخزن والأصناف والكميات وتكلفة الوحدة.");
      await purchasingApi.createReceipt({
        company: companyId, supplier: Number(receiptForm.supplier), warehouse: Number(receiptForm.warehouse),
        receipt_number: receiptForm.receipt_number, receipt_date: receiptForm.receipt_date,
        supplier_delivery_ref: receiptForm.supplier_delivery_ref, notes: receiptForm.notes,
        lines: receiptLines.map((line) => ({ product: Number(line.product), quantity: line.quantity, unit_cost: line.unit_cost, expiry_date: line.expiry_date || null })),
      });
      setShowForm(false); setReceiptForm((old) => ({ ...old, receipt_number: "", supplier_delivery_ref: "", notes: "" }));
      setReceiptLines([{ product: String(products[0]?.id || ""), quantity: "1", unit_cost: "0", expiry_date: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن الاستلام."); }
    finally { setBusy(false); }
  };

  const saveInvoice = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    const receipt = receipts.find((row) => row.id === Number(invoiceForm.receipt));
    if (!receipt) { setError("اختر إذن استلام مرحّلًا."); return; }
    setBusy(true); setError("");
    try {
      await purchasingApi.createInvoice({
        company: companyId, supplier: receipt.supplier, receipt: receipt.id,
        invoice_number: invoiceForm.invoice_number, invoice_date: invoiceForm.invoice_date, notes: invoiceForm.notes,
        lines: receipt.line_items.map((line) => ({ receipt_line: line.id, unit_price: line.unit_cost, tax_rate: invoiceForm.tax_rate })),
      });
      setShowForm(false); setInvoiceForm((old) => ({ ...old, receipt: "", invoice_number: "", notes: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء فاتورة المورد."); }
    finally { setBusy(false); }
  };

  const savePayment = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    const invoice = invoices.find((row) => row.id === Number(paymentForm.invoice));
    if (!invoice) { setError("اختر فاتورة مورد لها رصيد مستحق."); return; }
    setBusy(true); setError("");
    try {
      await purchasingApi.createPayment({ company: companyId, supplier: invoice.supplier, invoice: invoice.id, payment_number: paymentForm.payment_number, payment_date: paymentForm.payment_date, method: paymentForm.method, amount: paymentForm.amount, reference: paymentForm.reference });
      setShowForm(false); setPaymentForm((old) => ({ ...old, invoice: "", payment_number: "", amount: "", reference: "" })); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء سند الصرف."); }
    finally { setBusy(false); }
  };
  const postPayment = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postPayment(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل سند الصرف."); }
    finally { setBusy(false); }
  };

  const postReceipt = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postReceipt(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل إذن الاستلام."); }
    finally { setBusy(false); }
  };
  const postInvoice = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postInvoice(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل فاتورة المورد."); }
    finally { setBusy(false); }
  };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر شركة لعرض دورة المشتريات.</div></section>;
  return <section className="work-panel inventory-panel">
    <div className="work-panel-head"><div><h2>دورة المشتريات</h2><p>استلام البضاعة أولاً، ثم فاتورة المورد وتسوية حساب البضاعة المستلمة غير المفوترة.</p></div>
      <button className="primary-button" type="button" disabled={busy || tab === "statement" || ((tab === "invoices" && invoiceableReceipts.length === 0) || (tab === "payments" && paymentableInvoices.length === 0))} onClick={() => setShowForm((v) => !v)}>{showForm ? "إغلاق النموذج" : tab === "receipts" ? "＋ إذن استلام جديد" : tab === "invoices" ? "＋ فاتورة مورد جديدة" : tab === "payments" ? "＋ سند صرف جديد" : "كشف الحساب"}</button>
    </div>
    <div className="inventory-tabs"><button className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); setSearch(""); }}>أذون الاستلام <b>{receipts.length}</b></button><button className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); setSearch(""); }}>فواتير الموردين <b>{invoices.length}</b></button><button className={tab === "payments" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("payments"); setShowForm(false); setSearch(""); }}>سندات الصرف <b>{payments.length}</b></button><button className={tab === "statement" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("statement"); setShowForm(false); setStatementSupplier((old) => old || String(suppliers[0]?.id || "")); setStatement(null); }}>كشف حساب المورد</button></div>
    {error && <div className="error-banner">{error}</div>}
    {showForm && tab === "receipts" && <form className="inventory-form" onSubmit={(event) => void saveReceipt(event)}>
      <label>المورد<select required value={receiptForm.supplier} onChange={(e) => setReceiptForm({ ...receiptForm, supplier: e.target.value })}><option value="">اختر المورد</option>{suppliers.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>مخزن الاستلام<select required value={receiptForm.warehouse} onChange={(e) => setReceiptForm({ ...receiptForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>رقم إذن الاستلام<input required maxLength={32} value={receiptForm.receipt_number} onChange={(e) => setReceiptForm({ ...receiptForm, receipt_number: e.target.value })} placeholder="GR-0001" /></label>
      <label>تاريخ الاستلام<input required type="date" value={receiptForm.receipt_date} onChange={(e) => setReceiptForm({ ...receiptForm, receipt_date: e.target.value })} /></label>
      <label>مرجع مستند المورد<input value={receiptForm.supplier_delivery_ref} onChange={(e) => setReceiptForm({ ...receiptForm, supplier_delivery_ref: e.target.value })} /></label>
      <div className="inventory-wide"><div className="work-panel-head"><div><h3>أصناف الاستلام</h3><p>أضف كل الأصناف والكميات وتكلفة الوحدة قبل الحفظ.</p></div><button className="subtle-button" type="button" onClick={() => setReceiptLines((old) => [...old, { product: "", quantity: "1", unit_cost: "0", expiry_date: "" }])}>＋ إضافة صنف</button></div></div>
      {receiptLines.map((line, index) => <div className="inventory-form-row inventory-wide" key={index}>
        <label>الصنف<select required value={line.product} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, product: e.target.value } : x))}><option value="">اختر الصنف</option>{products.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, quantity: e.target.value } : x))} /></label>
        <label>تكلفة الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_cost} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, unit_cost: e.target.value } : x))} /></label>
        {products.find((x) => x.id === Number(line.product))?.track_expiry && <label>تاريخ الصلاحية<input required type="date" value={line.expiry_date} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, expiry_date: e.target.value } : x))} /></label>}
        <button className="subtle-button" type="button" disabled={receiptLines.length === 1} onClick={() => setReceiptLines((old) => old.filter((_, i) => i !== index))}>حذف السطر</button>
      </div>)}
      <label className="inventory-wide">ملاحظات<textarea value={receiptForm.notes} onChange={(e) => setReceiptForm({ ...receiptForm, notes: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إذن الاستلام كمسودة"}</button></div>
    </form>}
    {showForm && tab === "invoices" && <form className="inventory-form" onSubmit={(event) => void saveInvoice(event)}>
      <label className="inventory-wide">إذن الاستلام المرحّل<select required value={invoiceForm.receipt} onChange={(e) => setInvoiceForm({ ...invoiceForm, receipt: e.target.value })}><option value="">اختر إذن الاستلام</option>{invoiceableReceipts.map((x) => <option key={x.id} value={x.id}>{x.receipt_number} - {x.supplier_name} - {x.receipt_date}</option>)}</select></label>
      <label>رقم فاتورة المورد<input required value={invoiceForm.invoice_number} onChange={(e) => setInvoiceForm({ ...invoiceForm, invoice_number: e.target.value })} /></label>
      <label>تاريخ الفاتورة<input required type="date" value={invoiceForm.invoice_date} onChange={(e) => setInvoiceForm({ ...invoiceForm, invoice_date: e.target.value })} /></label>
      <label>نسبة الضريبة %<input required type="number" min="0" max="100" step="0.0001" value={invoiceForm.tax_rate} onChange={(e) => setInvoiceForm({ ...invoiceForm, tax_rate: e.target.value })} /><small>أدخل النسبة المعتمدة للمعاملة؛ تُحسب الضريبة على قيمة الأصناف غير شاملة الضريبة.</small></label>
      {invoiceForm.receipt && <div className="inventory-wide data-table-wrap"><table><thead><tr><th>الصنف</th><th>الكمية</th><th>سعر الوحدة</th><th>صافي القيمة</th></tr></thead><tbody>{receipts.find((x) => x.id === Number(invoiceForm.receipt))?.line_items.map((line) => <tr key={line.id}><td>{line.product_code} - {line.product_name}</td><td>{line.quantity}</td><td>{money(line.unit_cost)}</td><td>{money(String(Number(line.quantity) * Number(line.unit_cost)))}</td></tr>)}</tbody></table><p>سيتم احتساب الضريبة على قيمة الأصناف، ويجب أن يطابق صافي الفاتورة قيمة الاستلام حتى لا تتغير قيمة المخزون.</p></div>}
      <label className="inventory-wide">ملاحظات<textarea value={invoiceForm.notes} onChange={(e) => setInvoiceForm({ ...invoiceForm, notes: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ فاتورة المورد كمسودة"}</button></div>
    </form>}
    {showForm && tab === "payments" && <form className="inventory-form" onSubmit={(event) => void savePayment(event)}>
      <label className="inventory-wide">فاتورة المورد<select required value={paymentForm.invoice} onChange={(e) => { const invoice = invoices.find((x) => x.id === Number(e.target.value)); setPaymentForm({ ...paymentForm, invoice: e.target.value, amount: invoice?.balance_due || "" }); }}><option value="">اختر الفاتورة</option>{paymentableInvoices.map((x) => <option key={x.id} value={x.id}>{x.invoice_number} - {x.supplier_name} - المتبقي {money(x.balance_due)}</option>)}</select></label>
      <label>رقم سند الصرف<input required value={paymentForm.payment_number} onChange={(e) => setPaymentForm({ ...paymentForm, payment_number: e.target.value })} /></label>
      <label>تاريخ الصرف<input required type="date" value={paymentForm.payment_date} onChange={(e) => setPaymentForm({ ...paymentForm, payment_date: e.target.value })} /></label>
      <label>طريقة الصرف<select value={paymentForm.method} onChange={(e) => setPaymentForm({ ...paymentForm, method: e.target.value as "cash" | "bank" })}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={paymentForm.amount} onChange={(e) => setPaymentForm({ ...paymentForm, amount: e.target.value })} /></label>
      <label>مرجع التحويل/الإيصال<input value={paymentForm.reference} onChange={(e) => setPaymentForm({ ...paymentForm, reference: e.target.value })} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ سند الصرف كمسودة"}</button></div>
    </form>}
    {tab === "statement" && <div className="inventory-form">
      <label>المورد<select value={statementSupplier} onChange={(e) => { setStatementSupplier(e.target.value); setStatement(null); }}><option value="">اختر المورد</option>{suppliers.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>من تاريخ<input type="date" value={statementFrom} onChange={(e) => setStatementFrom(e.target.value)} /></label>
      <label>إلى تاريخ<input type="date" value={statementTo} onChange={(e) => setStatementTo(e.target.value)} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy || !statementSupplier} onClick={() => void loadStatement()}>{busy ? "جارٍ التحميل…" : "عرض كشف الحساب"}</button></div>
      {statement && <div className="inventory-wide">
        <div className="stock-card-summary"><div><span>المورد</span><strong>{statement.supplier_name}</strong></div><div><span>الرصيد الافتتاحي</span><strong>{money(statement.opening_balance)}</strong></div><div><span>الرصيد الختامي</span><strong>{money(statement.closing_balance)}</strong></div></div>
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>نوع المستند</th><th>رقم المستند</th><th>البيان</th><th>مدين</th><th>دائن</th><th>الرصيد</th></tr></thead><tbody>{statement.rows.map((row, index) => <tr key={row.document_type + row.document_number + index}><td>{row.date}</td><td>{row.document_type === "invoice" ? "فاتورة مورد" : "سداد"}</td><td className="code-cell">{row.document_number}</td><td>{row.description}</td><td>{money(row.debit)}</td><td>{money(row.credit)}</td><td>{money(row.balance)}</td></tr>)}</tbody></table>{statement.rows.length === 0 && <div className="table-empty">لا توجد حركة خلال الفترة المحددة.</div>}</div>
      </div>}
    </div>}
    <div className="customer-toolbar"><div className="customer-count">{tab === "receipts" ? "عدد أذون الاستلام" : tab === "invoices" ? "عدد فواتير الموردين" : "عدد سندات الصرف"} <strong>{(tab === "receipts" ? receipts : tab === "invoices" ? invoices : payments).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو المورد…" value={search} onChange={(e) => setSearch(e.target.value)} /><button className="subtle-button" type="button" onClick={() => void refresh()} disabled={busy}>{busy ? "تحديث…" : "تحديث"}</button></div>
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>المورد</th><th>المخزن</th><th>التاريخ</th><th>مرجع المورد</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map((x) => <tr key={x.id}><td className="code-cell">{x.receipt_number}</td><td>{x.supplier_name}</td><td>{x.warehouse_name}</td><td>{x.receipt_date}</td><td>{x.supplier_delivery_ref || "—"}</td><td>{x.movement_status === "posted" ? "مرحّل" : "مسودة"}</td><td>{x.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postReceipt(x.id)}>ترحيل الاستلام</button> : "تم تحديث المخزون"}</td></tr>)}</tbody></table>{!busy && filteredReceipts.length === 0 && <div className="table-empty">لا توجد أذون استلام مطابقة.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>المورد</th><th>إذن الاستلام</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map((x) => <tr key={x.id}><td className="code-cell">{x.invoice_number}</td><td>{x.supplier_name}</td><td>{x.receipt_number}</td><td>{x.invoice_date}</td><td>{money(x.net_total)}</td><td>{money(x.tax_total)}</td><td>{money(x.grand_total)}</td><td>{money(x.paid_total)}</td><td>{money(x.balance_due)}</td><td>{x.status === "posted" ? "مرحّلة" : "مسودة"}</td><td>{x.journal_number || "—"}</td><td>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postInvoice(x.id)}>ترحيل الفاتورة</button> : "تم الترحيل"}</td></tr>)}</tbody></table>{!busy && filteredInvoices.length === 0 && <div className="table-empty">لا توجد فواتير موردين مطابقة.</div>}</div>}
    {tab === "payments" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>المورد</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredPayments.map((x) => <tr key={x.id}><td className="code-cell">{x.payment_number}</td><td>{x.supplier_name}</td><td>{x.invoice_number}</td><td>{x.payment_date}</td><td>{x.method === "cash" ? "نقدي" : "تحويل بنكي"}</td><td>{money(x.amount)}</td><td>{x.status === "posted" ? "مرحّل" : "مسودة"}</td><td>{x.journal_number || "—"}</td><td>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postPayment(x.id)}>ترحيل السند</button> : "تم الترحيل"}</td></tr>)}</tbody></table>{!busy && filteredPayments.length === 0 && <div className="table-empty">لا توجد سندات صرف مطابقة.</div>}</div>}
  </section>;
}
