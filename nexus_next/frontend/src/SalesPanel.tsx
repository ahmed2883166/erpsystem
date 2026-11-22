/*
 * دورة المبيعات: تسليمات العملاء وفواتير البيع والتحصيل ومتابعة أعمار الديون.
 * تربط مستندات البيع بالمخزون والإيراد والضريبة والذمم والتحصيلات.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, inventoryApi, salesApi, treasuryApi, type Customer, type CustomerReceipt, type Product, type SalesDelivery, type SalesInvoice, type TreasuryAccount, type Warehouse, type CustomerAging, type TaxCode, type Currency, type ExchangeRate } from "./api";

type Props = { companyId: number | null };
type DeliveryDraftLine = { product: string; quantity: string; expiry_date: string };
type InvoiceDraftLine = { delivery_line: number; product_name: string; quantity: string; unit_price: string; tax_rate: string; tax_code: string; discount_percent: string };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

// شاشة دورة المبيعات وتبويبات التسليمات والفواتير والتحصيل وأعمار الديون.
export default function SalesPanel({ companyId }: Props) {
  const [tab, setTab] = useState<"deliveries" | "invoices" | "receipts" | "aging">("deliveries");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRate[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [deliveries, setDeliveries] = useState<SalesDelivery[]>([]);
  const [invoices, setInvoices] = useState<SalesInvoice[]>([]);
  const [receipts, setReceipts] = useState<CustomerReceipt[]>([]);
  const [agingAsOf, setAgingAsOf] = useState(new Date().toISOString().slice(0, 10));
  const [aging, setAging] = useState<CustomerAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const [deliveryForm, setDeliveryForm] = useState({ customer: "", warehouse: "", delivery_number: "", delivery_date: new Date().toISOString().slice(0, 10) });
  const [deliveryLines, setDeliveryLines] = useState<DeliveryDraftLine[]>([{ product: "", quantity: "1", expiry_date: "" }]);
  const [invoiceForm, setInvoiceForm] = useState({ delivery: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), transaction_currency: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10) });
  const [invoiceLines, setInvoiceLines] = useState<InvoiceDraftLine[]>([]);
  const [editingInvoiceId, setEditingInvoiceId] = useState<number | null>(null);
  const [receiptForm, setReceiptForm] = useState({ invoice: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", payment_account: "", amount: "", transaction_currency: "", transaction_amount: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), reference: "" });

  // تحديث قوائم العملاء والأصناف والمخازن ومستندات البيع للشركة الحالية.
const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [c, p, w, d, i, r, t, ta, cur, fx] = await Promise.all([
        api.customers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId),
        salesApi.deliveries(companyId), salesApi.invoices(companyId), salesApi.receipts(companyId), api.taxCodes(companyId), treasuryApi.accounts(companyId), api.currencies(), api.exchangeRates(companyId),
      ]);
      setCustomers(c); setProducts(p); setWarehouses(w); setDeliveries(d); setInvoices(i); setReceipts(r); setTaxCodes(t.filter(x => x.is_active && x.category !== "reverse_charge")); setTreasuryAccounts(ta.filter(x => x.is_active)); setCurrencies(cur.filter(x => x.is_active)); setExchangeRates(fx);
      setReceiptForm((old) => ({ ...old, payment_account: String(ta.find(x => x.is_active && x.kind === old.method)?.account || "") }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المبيعات."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  const invoiceable = useMemo(() => deliveries.filter(d => d.movement_status === "posted" && d.line_items.length > 0 && !invoices.some(i => i.delivery === d.id)), [deliveries, invoices]);
  const payable = useMemo(() => invoices.filter(i => i.status === "posted" && Number(i.balance_due) > 0), [invoices]);
  const filteredDeliveries = deliveries.filter(x => [x.delivery_number, x.customer_name, x.warehouse_name].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredInvoices = invoices.filter(x => [x.invoice_number, x.customer_name, x.delivery_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredReceipts = receipts.filter(x => [x.receipt_number, x.customer_name, x.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));

  const findRate = (currencyId: string, onDate: string) => exchangeRates
    .filter(rate => rate.currency === Number(currencyId) && rate.rate_date <= onDate)
    .sort((a, b) => b.rate_date.localeCompare(a.rate_date))[0];


  // إنشاء مستند تسليم بالأصناف والكميات والمخزن وتفاصيل الصلاحية.
const createDelivery = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      await salesApi.createDelivery({
        company: companyId, customer: Number(deliveryForm.customer), warehouse: Number(deliveryForm.warehouse),
        delivery_number: deliveryForm.delivery_number, delivery_date: deliveryForm.delivery_date,
        lines: deliveryLines.map(line => ({ product: Number(line.product), quantity: line.quantity, expiry_date: line.expiry_date || null })),
      });
      setShowForm(false);
      setDeliveryForm(old => ({ ...old, delivery_number: "" }));
      setDeliveryLines([{ product: "", quantity: "1", expiry_date: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن التسليم."); }
    finally { setBusy(false); }
  };
  // إنشاء فاتورة البيع وربطها ببنود التسليم والأسعار والخصومات والضرائب.
const createInvoice = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const delivery = deliveries.find(d => d.id === Number(invoiceForm.delivery));
    if (!delivery || !delivery.line_items.length) { setError("اختر إذن تسليم صالحًا."); return; }
    const customer = delivery.customer;
    const deliveryLine = delivery.line_items[0];
    setBusy(true); setError("");
    try {
      const payload = {
        company: companyId, customer, delivery: delivery.id, invoice_number: invoiceForm.invoice_number,
        invoice_date: invoiceForm.invoice_date,
        transaction_currency: invoiceForm.transaction_currency ? Number(invoiceForm.transaction_currency) : null,
        exchange_rate: invoiceForm.transaction_currency ? invoiceForm.exchange_rate : null,
        exchange_rate_date: invoiceForm.transaction_currency ? invoiceForm.exchange_rate_date : null,
        lines: invoiceLines.map(line => ({ delivery_line: line.delivery_line, ...(invoiceForm.transaction_currency ? { transaction_unit_price: line.unit_price } : { unit_price: line.unit_price }), tax_rate: line.tax_rate, tax_code: line.tax_code ? Number(line.tax_code) : null, discount_percent: line.discount_percent })),
      };
      if (editingInvoiceId) await salesApi.updateInvoice(editingInvoiceId, { lines: payload.lines });
      else await salesApi.createInvoice(payload);
      setShowForm(false); setEditingInvoiceId(null); setInvoiceForm(old => ({ ...old, delivery: "", invoice_number: "" })); setInvoiceLines([]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء فاتورة المبيعات."); }
    finally { setBusy(false); }
  };
  // تحميل بيانات الفاتورة المختارة داخل نموذج التعديل دون إرسال تغييرات للخادم.
const beginEditInvoice = (invoice: SalesInvoice) => {
    setTab("invoices"); setShowForm(true); setError(""); setEditingInvoiceId(invoice.id);
    setInvoiceForm({ delivery: String(invoice.delivery), invoice_number: invoice.invoice_number, invoice_date: invoice.invoice_date, transaction_currency: invoice.transaction_currency ? String(invoice.transaction_currency) : "", exchange_rate: invoice.exchange_rate || "", exchange_rate_date: invoice.exchange_rate_date || invoice.invoice_date });
    setInvoiceLines(invoice.invoice_lines.map(line => ({
      delivery_line: line.delivery_line, product_name: line.product_name, quantity: line.quantity,
      unit_price: line.transaction_unit_price || line.unit_price, tax_rate: line.tax_rate,
      tax_code: line.tax_code ? String(line.tax_code) : "",
      discount_percent: line.discount_percent,
    })));
  };
  const createReceipt = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const invoice = invoices.find(i => i.id === Number(receiptForm.invoice));
    if (!invoice) { setError("اختر فاتورة لها رصيد مستحق."); return; }
    setBusy(true); setError("");
    try {
      const foreignAmount = invoice.transaction_currency ? Number(receiptForm.transaction_amount) : null;
      const baseAmount = invoice.transaction_currency
        ? (foreignAmount! * Number(receiptForm.exchange_rate)).toFixed(4)
        : receiptForm.amount;
      await salesApi.createReceipt({ company: companyId, customer: invoice.customer, invoice: invoice.id,
        receipt_number: receiptForm.receipt_number, receipt_date: receiptForm.receipt_date,
        method: receiptForm.method, payment_account: Number(receiptForm.payment_account), amount: baseAmount,
        ...(invoice.transaction_currency ? { transaction_currency: invoice.transaction_currency, transaction_amount: receiptForm.transaction_amount, exchange_rate: receiptForm.exchange_rate, exchange_rate_date: receiptForm.exchange_rate_date } : {}),
        reference: receiptForm.reference });
      setShowForm(false); setReceiptForm(old => ({ ...old, invoice: "", receipt_number: "", amount: "", reference: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء سند القبض."); }
    finally { setBusy(false); }
  };
  // تحميل تقرير أعمار مديونيات العملاء للشركة.
const loadAging = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setAging(await salesApi.customerAging(companyId, agingAsOf)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل أعمار مديونية العملاء."); }
    finally { setBusy(false); }
  };

  // تنفيذ إجراء ترحيل مستند بيع مع معالجة حالة الانتظار والأخطاء وتحديث القوائم.
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
      <button type="button" className={tab === "deliveries" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("deliveries"); setShowForm(false); setEditingInvoiceId(null); }}>أذون التسليم <b>{deliveries.length}</b></button>
      <button type="button" className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); setEditingInvoiceId(null); }}>فواتير المبيعات <b>{invoices.length}</b></button>
      <button type="button" className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); setEditingInvoiceId(null); }}>سندات القبض <b>{receipts.length}</b></button>
      <button type="button" className={tab === "aging" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("aging"); setShowForm(false); setEditingInvoiceId(null); setAging(null); }}>أعمار مديونية العملاء</button>
    </div>
    {tab === "aging" && <div className="inventory-form">
      <label>تاريخ التقرير<input type="date" value={agingAsOf} onChange={e => { setAgingAsOf(e.target.value); setAging(null); }} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy} onClick={() => void loadAging()}>{busy ? "جارٍ التحميل…" : "عرض أعمار المديونية"}</button></div>
      {aging && <div className="inventory-wide">
        <div className="stock-card-summary">
          <div><span>غير مستحق</span><strong>{money(aging.totals.not_due)}</strong></div>
          <div><span>1–30 يوم</span><strong>{money(aging.totals["1_30"])}</strong></div>
          <div><span>31–60 يوم</span><strong>{money(aging.totals["31_60"])}</strong></div>
          <div><span>61–90 يوم</span><strong>{money(aging.totals["61_90"])}</strong></div>
          <div><span>أكثر من 90 يوم</span><strong>{money(aging.totals.over_90)}</strong></div>
          <div><span>إجمالي المستحق</span><strong>{money(aging.totals.total_due)}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>كود العميل</th><th>العميل</th><th>غير مستحق</th><th>1–30</th><th>31–60</th><th>61–90</th><th>أكثر من 90</th><th>الإجمالي</th></tr></thead>
          <tbody>{aging.customers.map(row => <tr key={row.customer}><td className="code-cell">{row.customer_code}</td><td>{row.customer_name}<details className="aging-invoice-details"><summary>تفاصيل الفواتير ({row.invoices.length})</summary><div className="aging-invoice-list">{row.invoices.map(invoice => <div key={invoice.invoice}><span>{invoice.invoice_number} · استحقاق {invoice.due_date}</span><strong>{money(invoice.balance_due)}</strong></div>)}</div></details></td><td>{money(row.not_due)}</td><td>{money(row["1_30"])}</td><td>{money(row["31_60"])}</td><td>{money(row["61_90"])}</td><td>{money(row.over_90)}</td><td><strong>{money(row.total_due)}</strong></td></tr>)}</tbody>
        </table>{aging.customers.length === 0 && <div className="table-empty">لا توجد أرصدة مستحقة على العملاء في تاريخ التقرير.</div>}</div>
        <p className="inventory-report-note">يعتمد التقرير على الفواتير المرحلة فقط، ويخصم سندات القبض المرحلة حتى تاريخ التقرير. تاريخ الاستحقاق = تاريخ الفاتورة + أجل السداد المسجل للعميل.</p>
      </div>}
    </div>}
    <div className="customer-toolbar"><div className="customer-count">عدد المستندات <strong>{(tab === "deliveries" ? deliveries : tab === "invoices" ? invoices : receipts).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو العميل…" value={search} onChange={e => setSearch(e.target.value)} /><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button><button type="button" className="primary-button" disabled={busy || tab === "aging"} onClick={() => { if (showForm) { setEditingInvoiceId(null); setInvoiceLines([]); setInvoiceForm(old => ({...old, delivery:"", invoice_number:""})); } setShowForm(v => !v); }}>{showForm ? "إغلاق النموذج" : tab === "deliveries" ? "إذن تسليم جديد" : tab === "invoices" ? "فاتورة جديدة" : tab === "receipts" ? "سند قبض جديد" : "التقرير"}</button></div>
    {showForm && tab === "deliveries" && <form className="inventory-form" onSubmit={createDelivery}>
      <label>العميل<select required value={deliveryForm.customer} onChange={e => setDeliveryForm({...deliveryForm, customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(c => c.is_active).map(c => <option key={c.id} value={c.id}>{c.code} · {c.name_ar}</option>)}</select></label>
      <label>مخزن الصرف<select required value={deliveryForm.warehouse} onChange={e => setDeliveryForm({...deliveryForm, warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.filter(w => w.is_active).map(w => <option key={w.id} value={w.id}>{w.code} · {w.name_ar}</option>)}</select></label>
      <label>رقم إذن التسليم<input required maxLength={37} value={deliveryForm.delivery_number} onChange={e => setDeliveryForm({...deliveryForm, delivery_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={deliveryForm.delivery_date} onChange={e => setDeliveryForm({...deliveryForm, delivery_date:e.target.value})} /></label>
      <div className="inventory-line-editor inventory-wide"><div className="inventory-line-heading"><strong>أصناف التسليم</strong><button type="button" className="subtle-button" onClick={() => setDeliveryLines(old => [...old, { product: "", quantity: "1", expiry_date: "" }])}>إضافة صنف</button></div>
        {deliveryLines.map((line, index) => { const selected = products.find(p => p.id === Number(line.product)); return <div className="inventory-form-row" key={index}>
          <label>الصنف<select required value={line.product} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, product:e.target.value, expiry_date:""} : x))}><option value="">اختر الصنف</option>{products.filter(p => p.is_active && p.product_type === "goods").map(p => <option key={p.id} value={p.id}>{p.code} · {p.name_ar}</option>)}</select></label>
          <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, quantity:e.target.value} : x))} /></label>
          {selected?.track_expiry && <label>تاريخ الصلاحية<input required type="date" value={line.expiry_date} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, expiry_date:e.target.value} : x))} /></label>}
          <button type="button" className="subtle-button" disabled={deliveryLines.length===1} onClick={() => setDeliveryLines(old => old.filter((_,i) => i!==index))}>حذف</button>
        </div>; })}
      </div>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إذن التسليم كمسودة"}</button></div>
    </form>}
    {showForm && tab === "invoices" && <form className="inventory-form" onSubmit={createInvoice}>
      <label>إذن التسليم<select required disabled={!!editingInvoiceId} value={invoiceForm.delivery} onChange={e => { const d=deliveries.find(x=>x.id===Number(e.target.value)); setInvoiceForm({...invoiceForm, delivery:e.target.value}); setInvoiceLines(d ? d.line_items.map(line => ({delivery_line:line.id, product_name:line.product_name, quantity:line.quantity, unit_price:products.find(p=>p.id===line.product)?.sale_price || "0", tax_rate:"0", tax_code:"", discount_percent:"0"})) : []); }}><option value="">اختر إذن التسليم</option>{invoiceable.map(d => <option key={d.id} value={d.id}>{d.delivery_number} · {d.customer_name}</option>)}</select></label>
      <label>رقم الفاتورة<input required disabled={!!editingInvoiceId} value={invoiceForm.invoice_number} onChange={e => setInvoiceForm({...invoiceForm, invoice_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" disabled={!!editingInvoiceId} value={invoiceForm.invoice_date} onChange={e => setInvoiceForm({...invoiceForm, invoice_date:e.target.value})} /></label>
      {invoiceLines.map((line,index) => <div className="inventory-form-row" key={line.delivery_line}>
        <label>الصنف<input readOnly value={line.product_name} /></label><label>الكمية<input readOnly value={line.quantity} /></label>
        <label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,unit_price:e.target.value}:x))} /></label>
        <label>كود الضريبة<select value={line.tax_code} onChange={e => { const selected=taxCodes.find(code=>code.id===Number(e.target.value)); setInvoiceLines(old=>old.map((x,i)=>i===index?{...x,tax_code:e.target.value,tax_rate:selected?.rate || "0"}:x)); }}><option value="">بدون كود — إدخال يدوي</option>{taxCodes.map(code=><option key={code.id} value={code.id}>{code.code} · {code.name_ar} · {code.rate}%</option>)}</select></label>
        {!line.tax_code && <label>الضريبة %<input required type="number" min="0" max="100" step="0.0001" value={line.tax_rate} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,tax_rate:e.target.value}:x))} /></label>}
        <label>خصم %<input required type="number" min="0" max="100" step="0.0001" value={line.discount_percent} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,discount_percent:e.target.value}:x))} /></label>
      </div>)}
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !invoiceForm.delivery || !invoiceLines.length}>{busy ? "جارٍ الحفظ…" : editingInvoiceId ? "حفظ تعديلات المسودة" : "حفظ الفاتورة كمسودة"}</button>{editingInvoiceId && <button type="button" className="subtle-button" onClick={() => { setEditingInvoiceId(null); setShowForm(false); setInvoiceLines([]); setInvoiceForm(old => ({...old, delivery:"", invoice_number:""})); }}>إلغاء التعديل</button>}</div>
    </form>}
    {showForm && tab === "receipts" && <form className="inventory-form" onSubmit={createReceipt}>
      <label>الفاتورة<select required value={receiptForm.invoice} onChange={e => { const invoice=invoices.find(x=>x.id===Number(e.target.value)); setReceiptForm({...receiptForm, invoice:e.target.value, amount:invoice?.balance_due || ""}); }}><option value="">اختر فاتورة مستحقة</option>{payable.map(i => <option key={i.id} value={i.id}>{i.invoice_number} · {i.customer_name} · متبقي {money(i.balance_due)}</option>)}</select></label>
      <label>رقم السند<input required value={receiptForm.receipt_number} onChange={e => setReceiptForm({...receiptForm, receipt_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={receiptForm.receipt_date} onChange={e => setReceiptForm({...receiptForm, receipt_date:e.target.value})} /></label>
      <label>طريقة التحصيل<select value={receiptForm.method} onChange={e => setReceiptForm({...receiptForm, method:e.target.value as "cash" | "bank", payment_account:String(treasuryAccounts.find(x => x.is_active && x.kind === (e.target.value as "cash" | "bank"))?.account || "")})}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label className="inventory-wide">حساب الخزينة / البنك<select required value={receiptForm.payment_account} onChange={e => setReceiptForm({...receiptForm, payment_account:e.target.value})}><option value="">اختر الحساب</option>{treasuryAccounts.filter(account => account.is_active && account.kind === receiptForm.method).map(account => <option key={account.id} value={account.account}>{account.code} - {account.name_ar} ({account.account_code})</option>)}</select></label>
      <label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={receiptForm.amount} onChange={e => setReceiptForm({...receiptForm, amount:e.target.value})} /></label>
      <label>مرجع التحويل/الإيصال<input value={receiptForm.reference} onChange={e => setReceiptForm({...receiptForm, reference:e.target.value})} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !receiptForm.invoice}>{busy ? "جارٍ الحفظ…" : "حفظ سند القبض كمسودة"}</button></div>
    </form>}
    {tab === "deliveries" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>العميل</th><th>المخزن</th><th>التاريخ</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredDeliveries.map(d=><tr key={d.id}><td className="code-cell">{d.delivery_number}</td><td>{d.customer_name}</td><td>{d.warehouse_name}</td><td>{d.delivery_date}</td><td>{d.movement_status === "posted" ? "مرحّل" : "مسودة"}</td><td>{d.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postDelivery(d.id))}>ترحيل وصرف المخزون</button> : "تم الصرف"}</td></tr>)}</tbody></table>{!busy && !filteredDeliveries.length && <div className="table-empty">لا توجد أذون تسليم.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>إذن التسليم</th><th>التاريخ</th><th>الخصم</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map(i=><tr key={i.id}><td className="code-cell">{i.invoice_number}</td><td>{i.customer_name}</td><td>{i.delivery_number}</td><td>{i.invoice_date}</td><td>{money(i.discount_total)}</td><td>{money(i.net_total)}</td><td>{money(i.tax_total)}</td><td>{money(i.grand_total)}</td><td>{money(i.paid_total)}</td><td>{money(i.balance_due)}</td><td>{i.journal_number || "—"}</td><td>{i.status === "draft" ? <div className="table-actions">{(i.approval_status === "pending" || i.approval_status === "rejected" || i.approval_status === "superseded") && <span>{i.approval_status === "pending" ? "بانتظار اعتماد المدير" : i.approval_status === "rejected" ? "الخصم مرفوض — يلزم تعديل" : "طلب سابق — راجع المسودة"}</span>}<button type="button" className="subtle-button" disabled={busy} onClick={() => beginEditInvoice(i)}>تعديل المسودة</button>{(i.approval_status === "not_required" || i.approval_status === "approved") && <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postInvoice(i.id))}>ترحيل الفاتورة</button>}</div> : "مرحّلة"}</td></tr>)}</tbody></table>{!busy && !filteredInvoices.length && <div className="table-empty">لا توجد فواتير مبيعات.</div>}</div>}
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>العميل</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>حساب التحصيل</th><th>المبلغ</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map(r=><tr key={r.id}><td className="code-cell">{r.receipt_number}</td><td>{r.customer_name}</td><td>{r.invoice_number}</td><td>{r.receipt_date}</td><td>{r.method === "cash" ? "نقدي" : "بنكي"}</td><td>{r.payment_account_name || "الحساب الافتراضي"}</td><td>{money(r.amount)}</td><td>{r.journal_number || "—"}</td><td>{r.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postReceipt(r.id))}>ترحيل سند القبض</button> : "مرحّل"}</td></tr>)}</tbody></table>{!busy && !filteredReceipts.length && <div className="table-empty">لا توجد سندات قبض.</div>}</div>}
  </section>;
}
