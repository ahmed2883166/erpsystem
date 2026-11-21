import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, inventoryApi, salesApi, type Customer, type CustomerReceipt, type Product, type SalesDelivery, type SalesInvoice, type Warehouse } from "./api";

type Props = { companyId: number | null };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

export default function SalesPanel({ companyId }: Props) {
  const [tab, setTab] = useState<"deliveries" | "invoices" | "receipts">("deliveries");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [deliveries, setDeliveries] = useState<SalesDelivery[]>([]);
  const [invoices, setInvoices] = useState<SalesInvoice[]>([]);
  const [receipts, setReceipts] = useState<CustomerReceipt[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const [deliveryForm, setDeliveryForm] = useState({ customer: "", warehouse: "", delivery_number: "", delivery_date: new Date().toISOString().slice(0, 10), product: "", quantity: "1", expiry_date: "" });
  const [invoiceForm, setInvoiceForm] = useState({ delivery: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), unit_price: "", tax_rate: "0" });
  const [receiptForm, setReceiptForm] = useState({ invoice: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", amount: "", reference: "" });

  const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [c, p, w, d, i, r] = await Promise.all([
        api.customers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId),
        salesApi.deliveries(companyId), salesApi.invoices(companyId), salesApi.receipts(companyId),
      ]);
      setCustomers(c); setProducts(p); setWarehouses(w); setDeliveries(d); setInvoices(i); setReceipts(r);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المبيعات."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  const invoiceable = useMemo(() => deliveries.filter(d => d.movement_status === "posted" && d.line_items.length > 0 && !invoices.some(i => i.delivery === d.id)), [deliveries, invoices]);
  const payable = useMemo(() => invoices.filter(i => i.status === "posted" && Number(i.balance_due) > 0), [invoices]);
  const filteredDeliveries = deliveries.filter(x => [x.delivery_number, x.customer_name, x.warehouse_name].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredInvoices = invoices.filter(x => [x.invoice_number, x.customer_name, x.delivery_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredReceipts = receipts.filter(x => [x.receipt_number, x.customer_name, x.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));

  const createDelivery = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      await salesApi.createDelivery({
        company: companyId, customer: Number(deliveryForm.customer), warehouse: Number(deliveryForm.warehouse),
        delivery_number: deliveryForm.delivery_number, delivery_date: deliveryForm.delivery_date,
        lines: [{ product: Number(deliveryForm.product), quantity: deliveryForm.quantity, expiry_date: deliveryForm.expiry_date || null }],
      });
      setShowForm(false);
      setDeliveryForm(old => ({ ...old, delivery_number: "", product: "", quantity: "1", expiry_date: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن التسليم."); }
    finally { setBusy(false); }
  };
  const createInvoice = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const delivery = deliveries.find(d => d.id === Number(invoiceForm.delivery));
    if (!delivery || !delivery.line_items.length) { setError("اختر إذن تسليم صالحًا."); return; }
    const customer = delivery.customer;
    const deliveryLine = delivery.line_items[0];
    setBusy(true); setError("");
    try {
      await salesApi.createInvoice({
        company: companyId, customer, delivery: delivery.id, invoice_number: invoiceForm.invoice_number,
        invoice_date: invoiceForm.invoice_date,
        lines: [{ delivery_line: deliveryLine.id, unit_price: invoiceForm.unit_price, tax_rate: invoiceForm.tax_rate }],
      });
      setShowForm(false); setInvoiceForm(old => ({ ...old, delivery: "", invoice_number: "", unit_price: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء فاتورة المبيعات."); }
    finally { setBusy(false); }
  };
  const createReceipt = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const invoice = invoices.find(i => i.id === Number(receiptForm.invoice));
    if (!invoice) { setError("اختر فاتورة لها رصيد مستحق."); return; }
    setBusy(true); setError("");
    try {
      await salesApi.createReceipt({ company: companyId, customer: invoice.customer, invoice: invoice.id,
        receipt_number: receiptForm.receipt_number, receipt_date: receiptForm.receipt_date,
        method: receiptForm.method, amount: receiptForm.amount, reference: receiptForm.reference });
      setShowForm(false); setReceiptForm(old => ({ ...old, invoice: "", receipt_number: "", amount: "", reference: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء سند القبض."); }
    finally { setBusy(false); }
  };
  const post = async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل المستند."); }
    finally { setBusy(false); }
  };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر الشركة لعرض دورة المبيعات.</div></section>;
  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>دورة المبيعات والتحصيل</h2><p>تسليم مخزني، فاتورة مبيعات، ثم سند قبض مرتبط بحساب العميل.</p></div><span className="coming-soon">المبيعات</span></div>
    {error && <div className="error-banner">{error}</div>}
    <div className="inventory-tabs">
      <button type="button" className={tab === "deliveries" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("deliveries"); setShowForm(false); }}>أذون التسليم <b>{deliveries.length}</b></button>
      <button type="button" className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); }}>فواتير المبيعات <b>{invoices.length}</b></button>
      <button type="button" className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); }}>سندات القبض <b>{receipts.length}</b></button>
    </div>
    <div className="customer-toolbar"><div className="customer-count">عدد المستندات <strong>{(tab === "deliveries" ? deliveries : tab === "invoices" ? invoices : receipts).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو العميل…" value={search} onChange={e => setSearch(e.target.value)} /><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button><button type="button" className="primary-button" disabled={busy} onClick={() => setShowForm(v => !v)}>{showForm ? "إغلاق النموذج" : tab === "deliveries" ? "إذن تسليم جديد" : tab === "invoices" ? "فاتورة جديدة" : "سند قبض جديد"}</button></div>
    {showForm && tab === "deliveries" && <form className="inventory-form" onSubmit={createDelivery}>
      <label>العميل<select required value={deliveryForm.customer} onChange={e => setDeliveryForm({...deliveryForm, customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(c => c.is_active).map(c => <option key={c.id} value={c.id}>{c.code} · {c.name_ar}</option>)}</select></label>
      <label>مخزن الصرف<select required value={deliveryForm.warehouse} onChange={e => setDeliveryForm({...deliveryForm, warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.filter(w => w.is_active).map(w => <option key={w.id} value={w.id}>{w.code} · {w.name_ar}</option>)}</select></label>
      <label>رقم إذن التسليم<input required maxLength={37} value={deliveryForm.delivery_number} onChange={e => setDeliveryForm({...deliveryForm, delivery_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={deliveryForm.delivery_date} onChange={e => setDeliveryForm({...deliveryForm, delivery_date:e.target.value})} /></label>
      <label>الصنف<select required value={deliveryForm.product} onChange={e => setDeliveryForm({...deliveryForm, product:e.target.value, expiry_date:""})}><option value="">اختر الصنف</option>{products.filter(p => p.is_active && p.product_type === "goods").map(p => <option key={p.id} value={p.id}>{p.code} · {p.name_ar}</option>)}</select></label>
      <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={deliveryForm.quantity} onChange={e => setDeliveryForm({...deliveryForm, quantity:e.target.value})} /></label>
      {products.find(p => p.id === Number(deliveryForm.product))?.track_expiry && <label>تاريخ الصلاحية<input required type="date" value={deliveryForm.expiry_date} onChange={e => setDeliveryForm({...deliveryForm, expiry_date:e.target.value})} /></label>}
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إذن التسليم كمسودة"}</button></div>
    </form>}
    {showForm && tab === "invoices" && <form className="inventory-form" onSubmit={createInvoice}>
      <label>إذن التسليم<select required value={invoiceForm.delivery} onChange={e => { const d=deliveries.find(x=>x.id===Number(e.target.value)); const p=d ? products.find(x=>x.id===d.line_items[0]?.product) : undefined; setInvoiceForm({...invoiceForm, delivery:e.target.value, unit_price:p?.sale_price || ""}); }}><option value="">اختر إذن التسليم</option>{invoiceable.map(d => <option key={d.id} value={d.id}>{d.delivery_number} · {d.customer_name}</option>)}</select></label>
      <label>رقم الفاتورة<input required value={invoiceForm.invoice_number} onChange={e => setInvoiceForm({...invoiceForm, invoice_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={invoiceForm.invoice_date} onChange={e => setInvoiceForm({...invoiceForm, invoice_date:e.target.value})} /></label>
      <label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={invoiceForm.unit_price} onChange={e => setInvoiceForm({...invoiceForm, unit_price:e.target.value})} /></label>
      <label>ضريبة القيمة المضافة %<input required type="number" min="0" max="100" step="0.0001" value={invoiceForm.tax_rate} onChange={e => setInvoiceForm({...invoiceForm, tax_rate:e.target.value})} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !invoiceForm.delivery}>{busy ? "جارٍ الحفظ…" : "حفظ الفاتورة كمسودة"}</button></div>
    </form>}
    {showForm && tab === "receipts" && <form className="inventory-form" onSubmit={createReceipt}>
      <label>الفاتورة<select required value={receiptForm.invoice} onChange={e => { const invoice=invoices.find(x=>x.id===Number(e.target.value)); setReceiptForm({...receiptForm, invoice:e.target.value, amount:invoice?.balance_due || ""}); }}><option value="">اختر فاتورة مستحقة</option>{payable.map(i => <option key={i.id} value={i.id}>{i.invoice_number} · {i.customer_name} · متبقي {money(i.balance_due)}</option>)}</select></label>
      <label>رقم السند<input required value={receiptForm.receipt_number} onChange={e => setReceiptForm({...receiptForm, receipt_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={receiptForm.receipt_date} onChange={e => setReceiptForm({...receiptForm, receipt_date:e.target.value})} /></label>
      <label>طريقة التحصيل<select value={receiptForm.method} onChange={e => setReceiptForm({...receiptForm, method:e.target.value as "cash" | "bank"})}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={receiptForm.amount} onChange={e => setReceiptForm({...receiptForm, amount:e.target.value})} /></label>
      <label>مرجع التحويل/الإيصال<input value={receiptForm.reference} onChange={e => setReceiptForm({...receiptForm, reference:e.target.value})} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !receiptForm.invoice}>{busy ? "جارٍ الحفظ…" : "حفظ سند القبض كمسودة"}</button></div>
    </form>}
    {tab === "deliveries" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>العميل</th><th>المخزن</th><th>التاريخ</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredDeliveries.map(d=><tr key={d.id}><td className="code-cell">{d.delivery_number}</td><td>{d.customer_name}</td><td>{d.warehouse_name}</td><td>{d.delivery_date}</td><td>{d.movement_status === "posted" ? "مرحّل" : "مسودة"}</td><td>{d.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postDelivery(d.id))}>ترحيل وصرف المخزون</button> : "تم الصرف"}</td></tr>)}</tbody></table>{!busy && !filteredDeliveries.length && <div className="table-empty">لا توجد أذون تسليم.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>إذن التسليم</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map(i=><tr key={i.id}><td className="code-cell">{i.invoice_number}</td><td>{i.customer_name}</td><td>{i.delivery_number}</td><td>{i.invoice_date}</td><td>{money(i.net_total)}</td><td>{money(i.tax_total)}</td><td>{money(i.grand_total)}</td><td>{money(i.paid_total)}</td><td>{money(i.balance_due)}</td><td>{i.journal_number || "—"}</td><td>{i.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postInvoice(i.id))}>ترحيل الفاتورة</button> : "مرحّلة"}</td></tr>)}</tbody></table>{!busy && !filteredInvoices.length && <div className="table-empty">لا توجد فواتير مبيعات.</div>}</div>}
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>العميل</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>المبلغ</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map(r=><tr key={r.id}><td className="code-cell">{r.receipt_number}</td><td>{r.customer_name}</td><td>{r.invoice_number}</td><td>{r.receipt_date}</td><td>{r.method === "cash" ? "نقدي" : "بنكي"}</td><td>{money(r.amount)}</td><td>{r.journal_number || "—"}</td><td>{r.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postReceipt(r.id))}>ترحيل سند القبض</button> : "مرحّل"}</td></tr>)}</tbody></table>{!busy && !filteredReceipts.length && <div className="table-empty">لا توجد سندات قبض.</div>}</div>}
  </section>;
}
