/*
 * دورة المشتريات: أوامر الشراء والاستلام وفواتير الموردين والمدفوعات وكشوف الموردين.
 * تربط مستندات الشراء بالمخزون وحساب البضاعة المستلمة غير المفوترة والضريبة والمورد.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, inventoryApi, purchasingApi, treasuryApi, type Product, type PurchaseInvoice, type PurchaseOrder, type PurchaseReceipt, type Supplier, type SupplierAging, type SupplierPayment, type SupplierReturn, type SupplierStatement, type TreasuryAccount, type Warehouse, type TaxCode, type Currency, type ExchangeRate, type FxSubledgerRevaluation } from "./api";

type Props = { companyId: number | null };
type ReceiptDraftLine = { product: string; quantity: string; unit_cost: string; expiry_date: string; purchase_order_line?: string };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });

// شاشة دورة المشتريات وتبويبات أوامر الشراء والاستلام والفواتير والمدفوعات.
export default function PurchasingPanel({ companyId }: Props) {
  const [tab, setTab] = useState<"orders" | "receipts" | "invoices" | "payments" | "returns" | "statement" | "aging">("receipts");
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRate[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [orders, setOrders] = useState<PurchaseOrder[]>([]);
  const [receipts, setReceipts] = useState<PurchaseReceipt[]>([]);
  const [invoices, setInvoices] = useState<PurchaseInvoice[]>([]);
  const [payments, setPayments] = useState<SupplierPayment[]>([]);
  const [returns, setReturns] = useState<SupplierReturn[]>([]);
  const [returnForm, setReturnForm] = useState({ invoice: "", return_number: "", return_date: new Date().toISOString().slice(0, 10), notes: "" });
  const [returnLines, setReturnLines] = useState<{ invoice_line: string; quantity: string }[]>([{ invoice_line: "", quantity: "1" }]);
  const [statementSupplier, setStatementSupplier] = useState("");
  const [statementFrom, setStatementFrom] = useState("");
  const [statementTo, setStatementTo] = useState("");
  const [statement, setStatement] = useState<SupplierStatement | null>(null);
  const [agingAsOf, setAgingAsOf] = useState(new Date().toISOString().slice(0, 10));
  const [aging, setAging] = useState<SupplierAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const [receiptForm, setReceiptForm] = useState({ supplier: "", warehouse: "", purchase_order: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), supplier_delivery_ref: "", notes: "" });
  const [receiptLines, setReceiptLines] = useState<ReceiptDraftLine[]>([{ product: "", quantity: "1", unit_cost: "0", expiry_date: "" }]);
  const [orderForm, setOrderForm] = useState({ supplier: "", warehouse: "", order_number: "", order_date: new Date().toISOString().slice(0, 10), expected_date: "", notes: "" });
  const [orderLines, setOrderLines] = useState<{ product: string; quantity: string; unit_price: string; tax_rate: string }[]>([{ product: "", quantity: "1", unit_price: "0", tax_rate: "0" }]);
  const [invoiceForm, setInvoiceForm] = useState({ receipt: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), tax_rate: "0", tax_code: "", transaction_currency: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), notes: "" });
  const [paymentForm, setPaymentForm] = useState({ invoice: "", payment_number: "", payment_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", payment_account: "", amount: "", transaction_currency: "", transaction_amount: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), reference: "" });
  const [fxInvoice, setFxInvoice] = useState<PurchaseInvoice | null>(null);
  const [fxHistory, setFxHistory] = useState<FxSubledgerRevaluation[]>([]);
  const [fxForm, setFxForm] = useState({revaluation_date:new Date().toISOString().slice(0,10),rate_date:new Date().toISOString().slice(0,10),rate:""});

  // تحميل بيانات الموردين والأصناف والمخازن ومستندات الشراء للشركة.
const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [s, p, w, o, r, i, pay, ret, t, ta, cur, fx] = await Promise.all([
        api.suppliers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId),
        purchasingApi.orders(companyId), purchasingApi.receipts(companyId), purchasingApi.invoices(companyId), purchasingApi.payments(companyId), purchasingApi.returns(companyId), api.taxCodes(companyId), treasuryApi.accounts(companyId), api.currencies(), api.exchangeRates(companyId),
      ]);
      setSuppliers(s.filter((x) => x.is_active)); setProducts(p.filter((x) => x.is_active && x.product_type === "goods"));
      setWarehouses(w.filter((x) => x.is_active)); setOrders(o); setReceipts(r); setInvoices(i); setPayments(pay); setReturns(ret); setTaxCodes(t.filter(x => x.is_active && x.input_tax_recoverable && x.category !== "reverse_charge")); setTreasuryAccounts(ta.filter(x => x.is_active));
      setOrderForm((old) => ({ ...old, supplier: old.supplier || String(s.find((x) => x.is_active)?.id || ""), warehouse: old.warehouse || String(w.find((x) => x.is_active)?.id || "") }));
      setOrderLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? String(p.find((x) => x.is_active && x.product_type === "goods")?.id || "") : "") })));
      setReceiptForm((old) => ({ ...old, supplier: old.supplier || String(s.find((x) => x.is_active)?.id || ""), warehouse: old.warehouse || String(w.find((x) => x.is_active)?.id || "") }));
      setPaymentForm((old) => ({ ...old, payment_account: String(ta.find(x => x.is_active && x.kind === old.method)?.account || "") }));
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
  const filteredReturns = returns.filter((row) => [row.return_number, row.supplier_name, row.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const returnableInvoices = invoices.filter((invoice) => invoice.status === "posted" && Number(invoice.balance_due) > 0 && (!invoice.transaction_currency || !payments.some((payment) => payment.invoice === invoice.id && payment.status === "posted")));

  const findRate = (currencyId: string, onDate: string) => exchangeRates
    .filter(rate => rate.currency === Number(currencyId) && rate.rate_date <= onDate)
    .sort((a, b) => b.rate_date.localeCompare(a.rate_date))[0];


  // تحميل كشف حساب المورد للفترة والمورد المختارين.
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

  // تحميل تقرير أعمار أرصدة الموردين.
const loadAging = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setAging(await purchasingApi.supplierAging(companyId, agingAsOf)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل أعمار مديونية الموردين."); }
    finally { setBusy(false); }
  };

  // إنشاء أو حفظ أمر الشراء وبنوده.
const saveOrder = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      await purchasingApi.createOrder({ company: companyId, supplier: Number(orderForm.supplier), warehouse: Number(orderForm.warehouse),
        order_number: orderForm.order_number, order_date: orderForm.order_date, expected_date: orderForm.expected_date || null,
        notes: orderForm.notes, lines: orderLines.map((line) => ({ product: Number(line.product), quantity: line.quantity, unit_price: line.unit_price, tax_rate: line.tax_rate })) });
      setShowForm(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء أمر الشراء."); }
    finally { setBusy(false); }
  };
  // تأكيد أمر الشراء المحدد قبل الانتقال إلى خطوات الاستلام.
const confirmOrder = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.confirmOrder(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر اعتماد أمر الشراء."); }
    finally { setBusy(false); }
  };

  // إلغاء أمر شراء قبل وجود أي استلام فعلي.
const cancelOrder = async (id: number) => {
    if (!window.confirm("سيتم إلغاء أمر الشراء نهائيًا ما دام لم يبدأ الاستلام. هل تريد المتابعة؟")) return;
    setBusy(true); setError("");
    try { await purchasingApi.cancelOrder(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إلغاء أمر الشراء."); }
    finally { setBusy(false); }
  };

  // حفظ مستند استلام الأصناف والكميات وتفاصيل التكلفة والصلاحية.
const saveReceipt = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      if (!receiptForm.supplier || !receiptForm.warehouse || !receiptLines.length || receiptLines.some((line) => !line.product || Number(line.quantity) <= 0 || Number(line.unit_cost) < 0)) throw new Error("راجع المورد والمخزن والأصناف والكميات وتكلفة الوحدة.");
      await purchasingApi.createReceipt({
        company: companyId, supplier: Number(receiptForm.supplier), warehouse: Number(receiptForm.warehouse),
        purchase_order: receiptForm.purchase_order ? Number(receiptForm.purchase_order) : null,
        receipt_number: receiptForm.receipt_number, receipt_date: receiptForm.receipt_date,
        supplier_delivery_ref: receiptForm.supplier_delivery_ref, notes: receiptForm.notes,
        lines: receiptLines.map((line) => ({ product: Number(line.product), quantity: line.quantity, unit_cost: line.unit_cost, expiry_date: line.expiry_date || null, ...(line.purchase_order_line ? { purchase_order_line: Number(line.purchase_order_line) } : {}) })),
      });
      setShowForm(false); setReceiptForm((old) => ({ ...old, receipt_number: "", supplier_delivery_ref: "", notes: "" }));
      setReceiptLines([{ product: String(products[0]?.id || ""), quantity: "1", unit_cost: "0", expiry_date: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن الاستلام."); }
    finally { setBusy(false); }
  };

  // حفظ فاتورة المورد وربطها بالاستلام والضرائب والمبالغ المستحقة.
const saveInvoice = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    const receipt = receipts.find((row) => row.id === Number(invoiceForm.receipt));
    if (!receipt) { setError("اختر إذن استلام مرحّلًا."); return; }
    setBusy(true); setError("");
    try {
      await purchasingApi.createInvoice({
        company: companyId, supplier: receipt.supplier, receipt: receipt.id,
        invoice_number: invoiceForm.invoice_number, invoice_date: invoiceForm.invoice_date, notes: invoiceForm.notes,
        transaction_currency: invoiceForm.transaction_currency ? Number(invoiceForm.transaction_currency) : null,
        exchange_rate: invoiceForm.transaction_currency ? invoiceForm.exchange_rate : null,
        exchange_rate_date: invoiceForm.transaction_currency ? invoiceForm.exchange_rate_date : null,
        lines: receipt.line_items.map((line) => ({ receipt_line: line.id, ...(invoiceForm.transaction_currency ? { transaction_unit_price: (Number(line.unit_cost) / Number(invoiceForm.exchange_rate)).toFixed(4) } : { unit_price: line.unit_cost }), tax_rate: invoiceForm.tax_rate, tax_code: invoiceForm.tax_code ? Number(invoiceForm.tax_code) : null })),
      });
      setShowForm(false); setInvoiceForm((old) => ({ ...old, receipt: "", invoice_number: "", notes: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء فاتورة المورد."); }
    finally { setBusy(false); }
  };

  // تسجيل دفعة المورد وربطها بحساب الخزينة أو البنك.
const savePayment = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    const invoice = invoices.find((row) => row.id === Number(paymentForm.invoice));
    if (!invoice) { setError("اختر فاتورة مورد لها رصيد مستحق."); return; }
    setBusy(true); setError("");
    try {
      const baseAmount = invoice.transaction_currency
        ? (Number(paymentForm.transaction_amount) * Number(paymentForm.exchange_rate)).toFixed(4)
        : paymentForm.amount;
      await purchasingApi.createPayment({ company: companyId, supplier: invoice.supplier, invoice: invoice.id, payment_number: paymentForm.payment_number, payment_date: paymentForm.payment_date, method: paymentForm.method, payment_account: paymentForm.payment_account ? Number(paymentForm.payment_account) : null, amount: baseAmount,
        ...(invoice.transaction_currency ? { transaction_currency: invoice.transaction_currency, transaction_amount: paymentForm.transaction_amount, exchange_rate: paymentForm.exchange_rate, exchange_rate_date: paymentForm.exchange_rate_date } : {}),
        reference: paymentForm.reference });
      setShowForm(false); setPaymentForm((old) => ({ ...old, invoice: "", payment_number: "", amount: "", reference: "" })); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء سند الصرف."); }
    finally { setBusy(false); }
  };
  // ترحيل دفعة المورد بعد التحقق من بياناتها على الخادم.
const postPayment = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postPayment(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل سند الصرف."); }
    finally { setBusy(false); }
  };

  // عكس آخر سند صرف مرحّل وتحديث رصيد المورد بعد إنشاء القيد العكسي.
  const reversePayment = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.reversePayment(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر عكس سند الصرف."); }
    finally { setBusy(false); }
  };

  // ترحيل استلام المخزون وتحديث الكميات والحسابات المرتبطة.
const postReceipt = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postReceipt(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل إذن الاستلام."); }
    finally { setBusy(false); }
  };
  // ترحيل فاتورة المورد وتثبيت الذمم والضريبة والتسوية المحاسبية.
const postInvoice = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.postInvoice(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل فاتورة المورد."); }
    finally { setBusy(false); }
  };


  // إنشاء مسودة مرتجع مرتبطة بفاتورة المورد وبنودها الأصلية.
  const saveReturn = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const invoice = invoices.find((row) => row.id === Number(returnForm.invoice));
    if (!invoice) { setError("اختر فاتورة مورد مرحلة ومسموحًا بمرتجعها."); return; }
    if (!returnForm.return_number.trim() || !returnLines.length || returnLines.some((line) => !line.invoice_line || Number(line.quantity) <= 0)) {
      setError("أدخل رقم المرتجع وبند الفاتورة وكمية صحيحة."); return;
    }
    setBusy(true); setError("");
    try {
      await purchasingApi.createReturn({
        company: companyId, supplier: invoice.supplier, invoice: invoice.id,
        return_number: returnForm.return_number.trim(), return_date: returnForm.return_date,
        notes: returnForm.notes,
        lines: returnLines.map((line) => ({ invoice_line: Number(line.invoice_line), quantity: line.quantity })),
      });
      setShowForm(false);
      setReturnForm((old) => ({ ...old, invoice: "", return_number: "", notes: "" }));
      setReturnLines([{ invoice_line: "", quantity: "1" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء مرتجع المورد."); }
    finally { setBusy(false); }
  };

  // ترحيل المرتجع بعد إعادة تحقق الخادم من الكمية والتكلفة والضريبة.
  const postReturn = async (id: number) => {
    if (!window.confirm("سيتم إخراج الكمية من مخزون الاستلام الأصلي وإنشاء قيد تخفيض المورد والضريبة. هل تريد الترحيل؟")) return;
    setBusy(true); setError("");
    try { await purchasingApi.postReturn(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل مرتجع المورد."); }
    finally { setBusy(false); }
  };

  // عكس مرتجع مرحّل بإرجاع نفس طبقات التكلفة وقيد المورد والضريبة.
  const reverseReturn = async (id: number) => {
    if (!window.confirm("سيتم إرجاع الكمية إلى المخزون بنفس طبقات التكلفة الأصلية وإنشاء قيد عكسي كامل. هل تريد المتابعة؟")) return;
    setBusy(true); setError("");
    try { await purchasingApi.reverseReturn(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر عكس مرتجع المورد."); }
    finally { setBusy(false); }
  };

  const beginFxRevaluation = async (invoice:PurchaseInvoice) => { setFxInvoice(invoice); setError(""); const today=new Date().toISOString().slice(0,10); const approved=findRate(String(invoice.transaction_currency),today); setFxForm({revaluation_date:today,rate_date:approved?.rate_date||today,rate:approved?.rate||""}); try { setFxHistory(await purchasingApi.fxRevaluations(invoice.id)); } catch(reason) { setFxHistory([]); setError(reason instanceof Error?reason.message:"تعذر تحميل سجل إعادة التقييم."); } };
  const submitFxRevaluation = async (event:FormEvent) => { event.preventDefault(); if(!fxInvoice)return; setBusy(true);setError(""); try { await purchasingApi.revalueInvoice(fxInvoice.id,fxForm);setFxHistory(await purchasingApi.fxRevaluations(fxInvoice.id));await refresh();setFxInvoice(null); } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إعادة تقييم فاتورة المورد."); } finally { setBusy(false); } };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر شركة لعرض دورة المشتريات.</div></section>;
  return <section className="work-panel inventory-panel">
    <div className="work-panel-head"><div><h2>دورة المشتريات</h2><p>استلام البضاعة أولاً، ثم فاتورة المورد وتسوية حساب البضاعة المستلمة غير المفوترة.</p></div>
      <button className="primary-button" type="button" disabled={busy || tab === "statement" || tab === "aging" || ((tab === "invoices" && invoiceableReceipts.length === 0) || (tab === "payments" && paymentableInvoices.length === 0) || (tab === "returns" && returnableInvoices.length === 0))} onClick={() => setShowForm((v) => !v)}>{showForm ? "إغلاق النموذج" : tab === "orders" ? "＋ أمر شراء جديد" : tab === "receipts" ? "＋ إذن استلام جديد" : tab === "invoices" ? "＋ فاتورة مورد جديدة" : tab === "payments" ? "＋ سند صرف جديد" : tab === "returns" ? "＋ مرتجع مورد جديد" : "كشف الحساب"}</button>
    </div>
    <div className="inventory-tabs"><button className={tab === "orders" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("orders"); setShowForm(false); setSearch(""); }}>أوامر الشراء <b>{orders.length}</b></button><button className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); setSearch(""); }}>أذون الاستلام <b>{receipts.length}</b></button><button className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); setSearch(""); }}>فواتير الموردين <b>{invoices.length}</b></button><button className={tab === "payments" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("payments"); setShowForm(false); setSearch(""); }}>سندات الصرف <b>{payments.length}</b></button><button className={tab === "returns" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("returns"); setShowForm(false); setSearch(""); }}>مرتجعات الموردين <b>{returns.length}</b></button><button className={tab === "statement" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("statement"); setShowForm(false); setStatementSupplier((old) => old || String(suppliers[0]?.id || "")); setStatement(null); }}>كشف حساب المورد</button><button className={tab === "aging" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("aging"); setShowForm(false); setAging(null); }}>أعمار المديونية</button></div>
    {error && <div className="error-banner">{error}</div>}
    {showForm && tab === "returns" && <form className="inventory-form" onSubmit={(event) => void saveReturn(event)}>
      <label className="inventory-wide">فاتورة المورد المرحّلة<select required value={returnForm.invoice} onChange={(e) => {
        const invoiceId = e.target.value;
        const invoice = invoices.find((row) => row.id === Number(invoiceId));
        setReturnForm((old) => ({ ...old, invoice: invoiceId }));
        setReturnLines([{ invoice_line: String(invoice?.invoice_lines[0]?.id || ""), quantity: "1" }]);
      }}><option value="">اختر فاتورة مورد</option>{returnableInvoices.map((invoice) => <option key={invoice.id} value={invoice.id}>{invoice.invoice_number} · {invoice.supplier_name} · {invoice.transaction_currency ? `${money(invoice.transaction_grand_total || "0")} ${currencies.find((currency) => currency.id === invoice.transaction_currency)?.code || "عملة أجنبية"}` : money(invoice.grand_total)} · متبقي {money(invoice.balance_due)}</option>)}</select></label>
      <label>رقم المرتجع<input required maxLength={37} value={returnForm.return_number} onChange={(e) => setReturnForm({ ...returnForm, return_number: e.target.value })} /></label>
      <label>تاريخ المرتجع<input required type="date" value={returnForm.return_date} onChange={(e) => setReturnForm({ ...returnForm, return_date: e.target.value })} /></label>
      {returnForm.invoice && <div className="inventory-wide"><div className="work-panel-head"><div><h3>الأصناف المرتجعة</h3><p>يُربط كل سطر ببند فاتورة المورد الأصلية ويُخصم من تكلفة الاستلام نفسه.</p></div><button className="subtle-button" type="button" onClick={() => setReturnLines((old) => [...old, { invoice_line: "", quantity: "1" }])}>＋ إضافة صنف</button></div></div>}
      {returnLines.map((line, index) => <div className="inventory-form-row inventory-wide" key={index}>
        <label>بند الفاتورة<select required value={line.invoice_line} onChange={(e) => setReturnLines((old) => old.map((item, i) => i === index ? { ...item, invoice_line: e.target.value } : item))}><option value="">اختر الصنف</option>{invoices.find((invoice) => invoice.id === Number(returnForm.invoice))?.invoice_lines.map((item) => <option key={item.id} value={item.id}>{item.product_code} · {item.product_name} · الكمية {item.quantity}</option>)}</select></label>
        <label>كمية المرتجع<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(e) => setReturnLines((old) => old.map((item, i) => i === index ? { ...item, quantity: e.target.value } : item))} /></label>
        <button className="subtle-button" type="button" disabled={returnLines.length === 1} onClick={() => setReturnLines((old) => old.filter((_, i) => i !== index))}>حذف السطر</button>
      </div>)}
      <label className="inventory-wide">ملاحظات<textarea value={returnForm.notes} onChange={(e) => setReturnForm({ ...returnForm, notes: e.target.value })} /></label>
      <div className="inventory-wide"><p className="inventory-report-note">تُرحّل المرتجعات الأجنبية بسعر الصرف التاريخي للفاتورة فقط إذا لم توجد دفعات أو إعادة تقييم سابقة عليها. عند وجود تسوية عملة سابقة يرفض الخادم الترحيل حمايةً من فروق غير محسوبة.</p></div>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy || !returnableInvoices.length}>{busy ? "جارٍ الحفظ…" : "حفظ مرتجع كمسودة"}</button></div>
    </form>}
    {showForm && tab === "orders" && <form className="inventory-form" onSubmit={(event) => void saveOrder(event)}>
      <label>المورد<select required value={orderForm.supplier} onChange={(e) => setOrderForm({ ...orderForm, supplier: e.target.value })}><option value="">اختر المورد</option>{suppliers.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>مخزن الاستلام<select required value={orderForm.warehouse} onChange={(e) => setOrderForm({ ...orderForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>رقم أمر الشراء<input required value={orderForm.order_number} onChange={(e) => setOrderForm({ ...orderForm, order_number: e.target.value })} /></label>
      <label>تاريخ الأمر<input required type="date" value={orderForm.order_date} onChange={(e) => setOrderForm({ ...orderForm, order_date: e.target.value })} /></label>
      <label>تاريخ التوريد المتوقع<input type="date" value={orderForm.expected_date} onChange={(e) => setOrderForm({ ...orderForm, expected_date: e.target.value })} /></label>
      <div className="inventory-wide"><div className="work-panel-head"><div><h3>بنود أمر الشراء</h3><p>يمكن استلام الكميات على دفعات بعد اعتماد الأمر.</p></div><button className="subtle-button" type="button" onClick={() => setOrderLines((old) => [...old, { product: "", quantity: "1", unit_price: "0", tax_rate: "0" }])}>＋ إضافة صنف</button></div></div>
      {orderLines.map((line, index) => <div className="inventory-form-row inventory-wide" key={index}>
        <label>الصنف<select required value={line.product} onChange={(e) => setOrderLines((old) => old.map((x, i) => i === index ? { ...x, product: e.target.value } : x))}><option value="">اختر الصنف</option>{products.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>الكمية المطلوبة<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(e) => setOrderLines((old) => old.map((x, i) => i === index ? { ...x, quantity: e.target.value } : x))} /></label>
        <label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={(e) => setOrderLines((old) => old.map((x, i) => i === index ? { ...x, unit_price: e.target.value } : x))} /></label>
        <label>الضريبة %<input type="number" min="0" max="100" step="0.0001" value={line.tax_rate} onChange={(e) => setOrderLines((old) => old.map((x, i) => i === index ? { ...x, tax_rate: e.target.value } : x))} /></label>
        <button className="subtle-button" type="button" disabled={orderLines.length === 1} onClick={() => setOrderLines((old) => old.filter((_, i) => i !== index))}>حذف السطر</button>
      </div>)}
      <label className="inventory-wide">ملاحظات<textarea value={orderForm.notes} onChange={(e) => setOrderForm({ ...orderForm, notes: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ أمر الشراء كمسودة"}</button></div>
    </form>}
    {showForm && tab === "receipts" && <form className="inventory-form" onSubmit={(event) => void saveReceipt(event)}>
      <label>المورد<select required value={receiptForm.supplier} onChange={(e) => setReceiptForm({ ...receiptForm, supplier: e.target.value })}><option value="">اختر المورد</option>{suppliers.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>مخزن الاستلام<select required value={receiptForm.warehouse} onChange={(e) => setReceiptForm({ ...receiptForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>رقم إذن الاستلام<input required maxLength={32} value={receiptForm.receipt_number} onChange={(e) => setReceiptForm({ ...receiptForm, receipt_number: e.target.value })} placeholder="GR-0001" /></label>
      <label>تاريخ الاستلام<input required type="date" value={receiptForm.receipt_date} onChange={(e) => setReceiptForm({ ...receiptForm, receipt_date: e.target.value })} /></label>
      {receiptForm.purchase_order && <div className="inventory-wide"><strong>أمر الشراء المرتبط: {orders.find((x) => x.id === Number(receiptForm.purchase_order))?.order_number || "—"}</strong></div>}
      <label>مرجع مستند المورد<input value={receiptForm.supplier_delivery_ref} onChange={(e) => setReceiptForm({ ...receiptForm, supplier_delivery_ref: e.target.value })} /></label>
      <div className="inventory-wide"><div className="work-panel-head"><div><h3>أصناف الاستلام</h3><p>أضف كل الأصناف والكميات وتكلفة الوحدة قبل الحفظ.</p></div>{!receiptForm.purchase_order && <button className="subtle-button" type="button" onClick={() => setReceiptLines((old) => [...old, { product: "", quantity: "1", unit_cost: "0", expiry_date: "" }])}>＋ إضافة صنف</button>}</div></div>
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
      <label>عملة فاتورة المورد<select value={invoiceForm.transaction_currency} onChange={e => { const rate=e.target.value ? findRate(e.target.value, invoiceForm.invoice_date) : null; setInvoiceForm({...invoiceForm, transaction_currency:e.target.value, exchange_rate:rate?.rate || "", exchange_rate_date:rate?.rate_date || invoiceForm.invoice_date}); }}><option value="">العملة الأساسية للشركة</option>{currencies.map(currency=><option key={currency.id} value={currency.id}>{currency.code} · {currency.name_ar}</option>)}</select></label>
      {invoiceForm.transaction_currency && <>
        <label>تاريخ سعر الصرف<input required type="date" value={invoiceForm.exchange_rate_date} onChange={e => { const rate=findRate(invoiceForm.transaction_currency,e.target.value); setInvoiceForm({...invoiceForm, exchange_rate_date:e.target.value, exchange_rate:rate?.rate || invoiceForm.exchange_rate}); }} /></label>
        <label>سعر الصرف مقابل العملة الأساسية<input required type="number" min="0.0000000001" step="0.0000000001" value={invoiceForm.exchange_rate} onChange={e => setInvoiceForm({...invoiceForm, exchange_rate:e.target.value})} /></label>
        <p className="inventory-wide">تُحوّل أسعار الوحدات الأجنبية إلى العملة الأساسية، ويجب أن تطابق قيمة صافي الفاتورة قيمة الاستلام المخزني.</p>
      </>}
      <label>كود ضريبة المدخلات<select value={invoiceForm.tax_code} onChange={(e) => { const selected=taxCodes.find(code=>code.id===Number(e.target.value)); setInvoiceForm({ ...invoiceForm, tax_code:e.target.value, tax_rate:selected?.rate || "0" }); }}><option value="">بدون كود — إدخال يدوي</option>{taxCodes.map(code=><option key={code.id} value={code.id}>{code.code} · {code.name_ar} · {code.rate}%</option>)}</select><small>تظهر هنا الأكواد القابلة للخصم فقط. الضريبة غير القابلة للخصم تحتاج معالجة مستقلة لتكلفة المخزون قبل تفعيلها.</small></label>
      {!invoiceForm.tax_code && <label>نسبة الضريبة %<input required type="number" min="0" max="100" step="0.0001" value={invoiceForm.tax_rate} onChange={(e) => setInvoiceForm({ ...invoiceForm, tax_rate: e.target.value })} /><small>أدخل النسبة المعتمدة للمعاملة؛ تُحسب الضريبة على قيمة الأصناف غير شاملة الضريبة.</small></label>}
      {invoiceForm.receipt && <div className="inventory-wide data-table-wrap"><table><thead><tr><th>الصنف</th><th>الكمية</th><th>سعر الوحدة</th><th>صافي القيمة</th></tr></thead><tbody>{receipts.find((x) => x.id === Number(invoiceForm.receipt))?.line_items.map((line) => <tr key={line.id}><td>{line.product_code} - {line.product_name}</td><td>{line.quantity}</td><td>{money(line.unit_cost)}</td><td>{money(String(Number(line.quantity) * Number(line.unit_cost)))}</td></tr>)}</tbody></table><p>سيتم احتساب الضريبة على قيمة الأصناف، ويجب أن يطابق صافي الفاتورة قيمة الاستلام حتى لا تتغير قيمة المخزون.</p></div>}
      <label className="inventory-wide">ملاحظات<textarea value={invoiceForm.notes} onChange={(e) => setInvoiceForm({ ...invoiceForm, notes: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ فاتورة المورد كمسودة"}</button></div>
    </form>}
    {showForm && tab === "payments" && <form className="inventory-form" onSubmit={(event) => void savePayment(event)}>
      <label className="inventory-wide">فاتورة المورد<select required value={paymentForm.invoice} onChange={(e) => { const invoice = invoices.find((x) => x.id === Number(e.target.value)); const rate=invoice?.transaction_currency ? findRate(String(invoice.transaction_currency), paymentForm.payment_date) : null; const paidForeign=invoice?.transaction_currency ? payments.filter(row=>row.invoice===invoice.id && row.status==="posted").reduce((sum,row)=>sum+Number(row.transaction_amount || 0),0) : 0; setPaymentForm({ ...paymentForm, invoice: e.target.value, amount: invoice?.balance_due || "", transaction_currency: invoice?.transaction_currency ? String(invoice.transaction_currency) : "", transaction_amount: invoice?.transaction_currency ? String(Math.max(Number(invoice.transaction_grand_total || 0)-paidForeign,0)) : "", exchange_rate: rate?.rate || "", exchange_rate_date: rate?.rate_date || paymentForm.payment_date }); }}><option value="">اختر الفاتورة</option>{paymentableInvoices.map((x) => <option key={x.id} value={x.id}>{x.invoice_number} - {x.supplier_name} - المتبقي {money(x.balance_due)}</option>)}</select></label>
      <label>رقم سند الصرف<input required value={paymentForm.payment_number} onChange={(e) => setPaymentForm({ ...paymentForm, payment_number: e.target.value })} /></label>
      <label>تاريخ الصرف<input required type="date" value={paymentForm.payment_date} onChange={(e) => setPaymentForm({ ...paymentForm, payment_date: e.target.value })} /></label>
      {paymentForm.transaction_currency && <>
        <label>المبلغ المسدد بالعملة الأجنبية<input required type="number" min="0.0001" step="0.0001" value={paymentForm.transaction_amount} onChange={e => setPaymentForm({...paymentForm, transaction_amount:e.target.value})} /></label>
        <label>تاريخ سعر الصرف<input required type="date" value={paymentForm.exchange_rate_date} onChange={e => { const rate=findRate(paymentForm.transaction_currency,e.target.value); setPaymentForm({...paymentForm, exchange_rate_date:e.target.value, exchange_rate:rate?.rate || paymentForm.exchange_rate}); }} /></label>
        <label>سعر الصرف<input required type="number" min="0.0000000001" step="0.0000000001" value={paymentForm.exchange_rate} onChange={e => setPaymentForm({...paymentForm, exchange_rate:e.target.value})} /></label>
        <label>المقابل بالعملة الأساسية<input readOnly value={(Number(paymentForm.transaction_amount || 0)*Number(paymentForm.exchange_rate || 0)).toFixed(4)} /></label>
      </>}
      <label>طريقة الصرف<select value={paymentForm.method} onChange={(e) => setPaymentForm({ ...paymentForm, method: e.target.value as "cash" | "bank", payment_account: String(treasuryAccounts.find(x => x.is_active && x.kind === (e.target.value as "cash" | "bank"))?.account || "") })}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label className="inventory-wide">حساب الخزينة / البنك<select value={paymentForm.payment_account} onChange={(e) => setPaymentForm({ ...paymentForm, payment_account: e.target.value })}><option value="">الحساب الافتراضي حسب طريقة الصرف</option>{treasuryAccounts.filter(account => account.is_active && account.kind === paymentForm.method).map((account) => <option key={account.id} value={account.account}>{account.code} - {account.name_ar} ({account.account_code})</option>)}</select></label>
      {!paymentForm.transaction_currency && <label>المبلغ بالعملة الأساسية<input required type="number" min="0.0001" step="0.0001" value={paymentForm.amount} onChange={(e) => setPaymentForm({ ...paymentForm, amount: e.target.value })} /></label>}
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
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>نوع المستند</th><th>رقم المستند</th><th>البيان</th><th>مدين</th><th>دائن</th><th>الرصيد</th></tr></thead><tbody>{statement.rows.map((row, index) => <tr key={row.document_type + row.document_number + index}><td>{row.date}</td><td>{row.document_type === "invoice" ? "فاتورة مورد" : row.document_type === "supplier_return" ? "مرتجع مورد" : row.document_type === "supplier_return_reversal" ? "عكس مرتجع" : "سداد"}</td><td className="code-cell">{row.document_number}</td><td>{row.description}</td><td>{money(row.debit)}</td><td>{money(row.credit)}</td><td>{money(row.balance)}</td></tr>)}</tbody></table>{statement.rows.length === 0 && <div className="table-empty">لا توجد حركة خلال الفترة المحددة.</div>}</div>
      </div>}
    </div>}
    {tab === "aging" && <div className="inventory-form">
      <label>تاريخ التقرير<input type="date" value={agingAsOf} onChange={(e) => { setAgingAsOf(e.target.value); setAging(null); }} /></label>
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
        <div className="data-table-wrap"><table><thead><tr><th>كود المورد</th><th>المورد</th><th>غير مستحق</th><th>1–30</th><th>31–60</th><th>61–90</th><th>أكثر من 90</th><th>الإجمالي</th></tr></thead>
          <tbody>{aging.suppliers.map((row) => <tr key={row.supplier}><td className="code-cell">{row.supplier_code}</td><td>{row.supplier_name}</td><td>{money(row.not_due)}</td><td>{money(row["1_30"])}</td><td>{money(row["31_60"])}</td><td>{money(row["61_90"])}</td><td>{money(row.over_90)}</td><td><strong>{money(row.total_due)}</strong></td></tr>)}</tbody>
        </table>{aging.suppliers.length === 0 && <div className="table-empty">لا توجد أرصدة مستحقة على الموردين في تاريخ التقرير.</div>}</div>
        <p className="inventory-report-note">يُحسب تاريخ الاستحقاق من تاريخ الفاتورة مضافًا إليه أجل السداد المسجل للمورد، وتُخصم السدادات المرحلة فقط.</p>
      </div>}
    </div>}
    <div className="customer-toolbar"><div className="customer-count">{tab === "orders" ? "عدد أوامر الشراء" : tab === "receipts" ? "عدد أذون الاستلام" : tab === "invoices" ? "عدد فواتير الموردين" : tab === "returns" ? "عدد مرتجعات الموردين" : "عدد سندات الصرف"} <strong>{(tab === "orders" ? orders : tab === "receipts" ? receipts : tab === "invoices" ? invoices : tab === "returns" ? returns : payments).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو المورد…" value={search} onChange={(e) => setSearch(e.target.value)} /><button className="subtle-button" type="button" onClick={() => void refresh()} disabled={busy}>{busy ? "تحديث…" : "تحديث"}</button></div>
    {tab === "orders" && <div className="data-table-wrap"><table><thead><tr><th>رقم الأمر</th><th>المورد</th><th>المخزن</th><th>تاريخ الأمر</th><th>التوريد المتوقع</th><th>البنود المستلمة/المطلوبة</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{orders.filter((row) => [row.order_number, row.supplier_name, row.warehouse_name].join(" ").toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.id}><td className="code-cell">{row.order_number}</td><td>{row.supplier_name}</td><td>{row.warehouse_name}</td><td>{row.order_date}</td><td>{row.expected_date || "—"}</td><td>{row.line_items.map((line) => <div key={line.id}>{line.product_code}: {line.received_quantity}/{line.quantity}</div>)}</td><td>{row.status === "draft" ? "مسودة" : row.status === "confirmed" ? "معتمد" : row.status === "partially_received" ? "استلام جزئي" : row.status === "received" ? "مستلم بالكامل" : "ملغي"}</td><td><div className="form-actions">{row.status === "draft" && <button className="primary-button" type="button" disabled={busy} onClick={() => void confirmOrder(row.id)}>اعتماد الأمر</button>}{(row.status === "confirmed" || row.status === "partially_received") && <button className="subtle-button" type="button" onClick={() => { setTab("receipts"); setShowForm(true); setReceiptForm((old) => ({ ...old, supplier: String(row.supplier), warehouse: String(row.warehouse), purchase_order: String(row.id), receipt_number: "" })); setReceiptLines(row.line_items.filter((line) => Number(line.remaining_quantity) > 0).map((line) => ({ product: String(line.product), quantity: line.remaining_quantity, unit_cost: line.unit_price, expiry_date: "", purchase_order_line: String(line.id) }))); }}>إنشاء إذن استلام</button>}{(row.status === "draft" || row.status === "confirmed") && <button className="subtle-button" type="button" disabled={busy} onClick={() => void cancelOrder(row.id)}>إلغاء الأمر</button>}{(row.status === "received" || row.status === "cancelled") && "—"}</div></td></tr>)}</tbody></table>{!busy && orders.length === 0 && <div className="table-empty">لا توجد أوامر شراء.</div>}</div>}
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>المورد</th><th>المخزن</th><th>التاريخ</th><th>مرجع المورد</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map((x) => <tr key={x.id}><td className="code-cell">{x.receipt_number}</td><td>{x.supplier_name}</td><td>{x.warehouse_name}</td><td>{x.receipt_date}</td><td>{x.supplier_delivery_ref || "—"}</td><td>{x.movement_status === "posted" ? "مرحّل" : "مسودة"}</td><td>{x.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postReceipt(x.id)}>ترحيل الاستلام</button> : "تم تحديث المخزون"}</td></tr>)}</tbody></table>{!busy && filteredReceipts.length === 0 && <div className="table-empty">لا توجد أذون استلام مطابقة.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>المورد</th><th>إذن الاستلام</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map((x) => <tr key={x.id}><td className="code-cell">{x.invoice_number}</td><td>{x.supplier_name}</td><td>{x.receipt_number}</td><td>{x.invoice_date}</td><td>{money(x.net_total)}</td><td>{money(x.tax_total)}</td><td>{money(x.grand_total)}</td><td>{money(x.paid_total)}</td><td>{money(x.balance_due)}</td><td>{x.status === "posted" ? "مرحّلة" : "مسودة"}</td><td>{x.journal_number || "—"}</td><td>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postInvoice(x.id)}>ترحيل الفاتورة</button> : <div className="table-actions"><span>تم الترحيل</span>{x.transaction_currency && Number(x.balance_due)>0 && <button type="button" className="subtle-button" disabled={busy} onClick={()=>void beginFxRevaluation(x)}>إعادة تقييم العملة</button>}</div>}</td></tr>)}</tbody></table>{!busy && filteredInvoices.length === 0 && <div className="table-empty">لا توجد فواتير موردين مطابقة.</div>}</div>}
    {tab === "invoices" && fxInvoice && <form className="inventory-form" onSubmit={submitFxRevaluation}>
      <div className="inventory-wide"><strong>إعادة تقييم رصيد المورد: {fxInvoice.invoice_number}</strong><p>إجمالي الفاتورة بالعملة الأجنبية: {money(fxInvoice.transaction_grand_total || "0")} · المتبقي بالعملة الأساسية: {money(fxInvoice.balance_due)}</p></div>
      <label>تاريخ إعادة التقييم<input required type="date" value={fxForm.revaluation_date} onChange={e=>setFxForm({...fxForm,revaluation_date:e.target.value})}/></label>
      <label>تاريخ سعر الصرف المعتمد<input required type="date" max={fxForm.revaluation_date} value={fxForm.rate_date} onChange={e=>{const rate=findRate(String(fxInvoice.transaction_currency),e.target.value);setFxForm({...fxForm,rate_date:e.target.value,rate:rate?.rate||fxForm.rate});}}/></label>
      <label>سعر الصرف مقابل عملة الشركة<input required type="number" min="0.0000000001" step="0.0000000001" value={fxForm.rate} onChange={e=>setFxForm({...fxForm,rate:e.target.value})}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!fxForm.rate}>{busy?"جارٍ التقييم…":"حفظ وإثبات فرق العملة"}</button><button className="subtle-button" type="button" onClick={()=>setFxInvoice(null)}>إغلاق</button></div>
      {fxHistory.length>0&&<div className="data-table-wrap inventory-wide"><table><thead><tr><th>تاريخ التقييم</th><th>سعر الصرف</th><th>الرصيد الأجنبي</th><th>الفرق</th><th>القيد</th><th>الحالة</th></tr></thead><tbody>{fxHistory.map(row=><tr key={row.id}><td>{row.revaluation_date}</td><td>{row.rate}</td><td>{money(row.foreign_balance)}</td><td>{money(row.difference)}</td><td>{row.journal_number||"—"}</td><td>{row.status==="reversed"?"معكوس":row.status==="no_difference"?"دون فرق":"مرحّل"}</td></tr>)}</tbody></table></div>}
    </form>}
    {tab === "returns" && <div className="data-table-wrap"><table><thead><tr><th>رقم المرتجع</th><th>المورد</th><th>فاتورة المورد</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>بالعملة الأجنبية</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReturns.map((row) => <tr key={row.id}><td className="code-cell">{row.return_number}</td><td>{row.supplier_name}</td><td>{row.invoice_number}</td><td>{row.return_date}</td><td>{money(row.net_amount)}</td><td>{money(row.tax_amount)}</td><td>{money(row.grand_total)}</td><td>{row.transaction_currency_code ? `${money(row.transaction_grand_total || "0")} ${row.transaction_currency_code}` : "—"}</td><td>{row.status === "posted" ? "مرحّل" : row.status === "reversed" ? "معكوس" : "مسودة"}</td><td>{row.status === "reversed" ? row.reversal_journal_number || "—" : row.journal_number || "—"}</td><td>{row.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postReturn(row.id)}>ترحيل المرتجع</button> : row.status === "posted" ? <button className="subtle-button" type="button" disabled={busy} onClick={() => void reverseReturn(row.id)}>عكس المرتجع</button> : "تم العكس"}</td></tr>)}</tbody></table>{!busy && filteredReturns.length === 0 && <div className="table-empty">لا توجد مرتجعات موردين مطابقة.</div>}</div>}
    {tab === "payments" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>المورد</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>حساب الصرف</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredPayments.map((x) => <tr key={x.id}><td className="code-cell">{x.payment_number}</td><td>{x.supplier_name}</td><td>{x.invoice_number}</td><td>{x.payment_date}</td><td>{x.method === "cash" ? "نقدي" : "تحويل بنكي"}</td><td>{x.payment_account_name || "الحساب الافتراضي"}</td><td>{money(x.amount)}</td><td>{x.status === "posted" ? "مرحّل" : x.status === "reversed" ? "معكوس" : "مسودة"}</td><td>{x.journal_number || "—"}</td><td>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postPayment(x.id)}>ترحيل السند</button> : x.status === "reversed" ? "تم العكس" : <button className="subtle-button" type="button" disabled={busy} onClick={() => { if (window.confirm("سيتم إنشاء قيد عكسي وإلغاء أثر سند الصرف على رصيد المورد. يجب عكس أحدث سند أولًا. هل تريد المتابعة؟")) void reversePayment(x.id); }}>عكس السند</button>}</td></tr>)}</tbody></table>{!busy && filteredPayments.length === 0 && <div className="table-empty">لا توجد سندات صرف مطابقة.</div>}</div>}
  </section>;
}
