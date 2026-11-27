/*
 * دورة المبيعات: تسليمات العملاء وفواتير البيع والتحصيل ومتابعة أعمار الديون.
 * تربط مستندات البيع بالمخزون والإيراد والضريبة والذمم والتحصيلات.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import DocumentTemplatesPanel from "./DocumentTemplatesPanel";
import SalesDocumentPrint from "./SalesDocumentPrint";
import SalesReportsPanel from "./SalesReportsPanel";
import CustomerReceiptBatchesPanel from "./CustomerReceiptBatchesPanel";
import { formatMoney, multiplyDecimalToFixed } from "./formatters";
import { api, inventoryApi, salesApi, treasuryApi, type Customer, type CustomerReceipt, type Product, type SalesDelivery, type SalesInvoice, type SalesReturn, type TreasuryAccount, type Warehouse, type CustomerAging, type CustomerReceiptBatch, type TaxCode, type Currency, type ExchangeRate, type FxSubledgerRevaluation, type SalesQuotation, type SalesOrder, type Company } from "./api";

type Props = { companyId: number | null };
type DeliveryDraftLine = { product: string; quantity: string; expiry_date: string; order_line?: string };
type InvoiceDraftLine = { delivery_line?: number; product?: number; product_name: string; quantity: string; unit_price: string; tax_rate: string; tax_code: string; discount_percent: string };
type QuotationDraftLine = { product:string; quantity:string; unit_price:string; tax_rate:string; tax_code:string; discount_percent:string };
const money = (value: string) => formatMoney(value);
const statusPill = (status: string) => `status-pill ${{ draft: "draft", posted: "posted", confirmed: "posted", delivered: "posted", cancelled: "reversed", partially_delivered: "partial", reversed: "reversed", sent: "sent", accepted: "accepted", rejected: "rejected", expired: "expired", partial: "partial" } as Record<string, string>}[status] || "unknown"}`;

// شاشة دورة المبيعات وتبويبات التسليمات والفواتير والتحصيل وأعمار الديون.
export default function SalesPanel({ companyId }: Props) {
  const [tab, setTab] = useState<"quotations" | "orders" | "deliveries" | "invoices" | "receipts" | "returns" | "aging" | "reports" | "receipt-batches">("deliveries");
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRate[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [branches, setBranches] = useState<import("./api").Branch[]>([]);
  const [deliveries, setDeliveries] = useState<SalesDelivery[]>([]);
  const [orders, setOrders] = useState<SalesOrder[]>([]);
  const [invoices, setInvoices] = useState<SalesInvoice[]>([]);
  const [receipts, setReceipts] = useState<CustomerReceipt[]>([]);
  const [receiptBatches, setReceiptBatches] = useState<CustomerReceiptBatch[]>([]);
  const [returns, setReturns] = useState<SalesReturn[]>([]);
  const [quotations, setQuotations] = useState<SalesQuotation[]>([]);
  const [editingQuotationId,setEditingQuotationId]=useState<number|null>(null);
  const [companyProfile, setCompanyProfile] = useState<Company|null>(null);
  const [printTarget, setPrintTarget] = useState<{kind:"invoice"|"quotation"|"receipt"|"return"|"receipt_batch";id:number}|null>(null);
  const [quoteLines, setQuoteLines] = useState<QuotationDraftLine[]>([{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]);
  const [quoteForm, setQuoteForm] = useState({customer:"",quotation_number:"",quotation_date:new Date().toISOString().slice(0,10),valid_until:new Date(Date.now()+14*86400000).toISOString().slice(0,10),notes:""});
  const [convertingQuoteId, setConvertingQuoteId] = useState<number|null>(null);
  const [quoteDeliveryForm, setQuoteDeliveryForm] = useState<{warehouse:string;delivery_number:string;delivery_date:string;expiry_dates:Record<string,string>}>({warehouse:"",delivery_number:"",delivery_date:new Date().toISOString().slice(0,10),expiry_dates:{}});
  const [agingAsOf, setAgingAsOf] = useState(new Date().toISOString().slice(0, 10));
  const [aging, setAging] = useState<CustomerAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [showTemplatesPanel, setShowTemplatesPanel] = useState(false);
  const [search, setSearch] = useState("");
  const [deliveryForm, setDeliveryForm] = useState({ customer: "", warehouse: "", order: "", delivery_number: "", delivery_date: new Date().toISOString().slice(0, 10) });
  const [orderForm, setOrderForm] = useState({customer:"",warehouse:"",order_number:"",order_date:new Date().toISOString().slice(0,10),requested_delivery_date:"",notes:""});
  const [orderLines, setOrderLines] = useState<QuotationDraftLine[]>([{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]);
  const [deliveryLines, setDeliveryLines] = useState<DeliveryDraftLine[]>([{ product: "", quantity: "1", expiry_date: "" }]);
  const [invoiceForm, setInvoiceForm] = useState({ mode: "delivery" as "delivery" | "service", delivery: "", branch: "", customer: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), transaction_currency: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10) });
  const [invoiceLines, setInvoiceLines] = useState<InvoiceDraftLine[]>([]);
  const [editingInvoiceId, setEditingInvoiceId] = useState<number | null>(null);
  const [fxInvoice, setFxInvoice] = useState<SalesInvoice | null>(null);
  const [fxHistory, setFxHistory] = useState<FxSubledgerRevaluation[]>([]);
  const [fxForm, setFxForm] = useState({ revaluation_date: new Date().toISOString().slice(0, 10), rate_date: new Date().toISOString().slice(0, 10), rate: "" });
  const [receiptForm, setReceiptForm] = useState({ invoice: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", payment_account: "", amount: "", transaction_currency: "", transaction_amount: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), reference: "" });
  const [returnForm, setReturnForm] = useState({invoice:"",invoice_line:"",return_number:"",return_date:new Date().toISOString().slice(0,10),quantity:"1",notes:""});

  // تحديث قوائم العملاء والأصناف والمخازن ومستندات البيع للشركة الحالية.
const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [c, p, w, b, d, i, r, sr, t, ta, cur, fx, q, so, companyRows, rb] = await Promise.all([
        api.customers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId), api.allBranches(companyId),
        salesApi.deliveries(companyId), salesApi.invoices(companyId), salesApi.receipts(companyId), salesApi.returns(companyId), api.taxCodes(companyId), treasuryApi.accounts(companyId), api.currencies(), api.exchangeRates(companyId), salesApi.quotations(companyId), salesApi.orders(companyId), api.companies(), salesApi.receiptBatches(companyId),
      ]);
      setCustomers(c); setProducts(p); setWarehouses(w); setBranches(b); setDeliveries(d); setOrders(so); setInvoices(i); setReceipts(r); setReceiptBatches(rb); setReturns(sr); setQuotations(q); setCompanyProfile(companyRows.find(x=>x.id===companyId)||null); setTaxCodes(t.filter(x => x.is_active && x.category !== "reverse_charge")); setTreasuryAccounts(ta.filter(x => x.is_active)); setCurrencies(cur.filter(x => x.is_active)); setExchangeRates(fx);
      setReceiptForm((old) => ({ ...old, payment_account: String(ta.find(x => x.is_active && x.kind === old.method)?.account || "") }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المبيعات."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  const invoicedQuantityByDeliveryLine = useMemo(() => {
    const totals = new Map<number, number>();
    invoices.forEach(invoice => invoice.invoice_lines.forEach(line => {
      if (line.delivery_line !== null) {
        totals.set(line.delivery_line, (totals.get(line.delivery_line) || 0) + Number(line.quantity));
      }
    }));
    return totals;
  }, [invoices]);
  const remainingDeliveryQuantity = (line: { id: number; quantity: string }) =>
    Math.max(Number(line.quantity) - (invoicedQuantityByDeliveryLine.get(line.id) || 0), 0);
  const invoiceable = useMemo(() => deliveries.filter(d =>
    d.movement_status === "posted" && d.line_items.some(line => Number(line.quantity) > (invoicedQuantityByDeliveryLine.get(line.id) || 0))
  ), [deliveries, invoicedQuantityByDeliveryLine]);
  const payable = useMemo(() => invoices.filter(i => i.status === "posted" && Number(i.balance_due) > 0), [invoices]);
  const filteredDeliveries = deliveries.filter(x => [x.delivery_number, x.customer_name, x.warehouse_name].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredInvoices = invoices.filter(x => [x.invoice_number, x.customer_name, x.delivery_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredReceipts = receipts.filter(x => [x.receipt_number, x.customer_name, x.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredReturns = returns.filter(x => [x.return_number,x.customer_name,x.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));

  const findRate = (currencyId: string, onDate: string) => exchangeRates
    .filter(rate => rate.currency === Number(currencyId) && rate.rate_date <= onDate)
    .sort((a, b) => b.rate_date.localeCompare(a.rate_date))[0];



  const createQuotation = async (event: FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      const payload = {
        company:companyId, customer:Number(quoteForm.customer),
        quotation_number:quoteForm.quotation_number, quotation_date:quoteForm.quotation_date,
        valid_until:quoteForm.valid_until, notes:quoteForm.notes,
        lines:quoteLines.map(line=>({
          product:Number(line.product),quantity:line.quantity,unit_price:line.unit_price,
          tax_rate:line.tax_rate,tax_code:line.tax_code?Number(line.tax_code):null,
          ...(line.discount_percent!==""?{discount_percent:line.discount_percent}:{})
        })),
      };
      if(editingQuotationId) await salesApi.updateQuotation(editingQuotationId,payload);
      else await salesApi.createQuotation(payload);
      setEditingQuotationId(null); setShowForm(false); setQuoteForm(old=>({...old,quotation_number:"",notes:""}));
      setQuoteLines([{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]);
      await refresh();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء عرض السعر."); }
    finally { setBusy(false); }
  };

  const beginEditQuotation = (quote:SalesQuotation) => {
    setEditingQuotationId(quote.id);
    setQuoteForm({customer:String(quote.customer),quotation_number:quote.quotation_number,quotation_date:quote.quotation_date,valid_until:quote.valid_until,notes:quote.notes||""});
    setQuoteLines(quote.quotation_lines.map(line=>({product:String(line.product),quantity:line.quantity,unit_price:line.unit_price,tax_rate:line.tax_rate,tax_code:line.tax_code?String(line.tax_code):"",discount_percent:line.discount_percent})));
    setTab("quotations");setShowForm(true);
  };
  const deleteQuotation = async (quote:SalesQuotation) => {
    if(!window.confirm("سيتم حذف مسودة عرض السعر فقط. هل تريد المتابعة؟"))return;
    setBusy(true);setError("");
    try{await salesApi.deleteQuotation(quote.id);await refresh();}
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر حذف مسودة عرض السعر.");}
    finally{setBusy(false);}
  };

  const quoteAction = async (action:"send"|"accept"|"reject", id:number) => {
    setBusy(true); setError("");
    try {
      if(action==="send") await salesApi.sendQuotation(id);
      else if(action==="accept") await salesApi.acceptQuotation(id);
      else await salesApi.rejectQuotation(id);
      await refresh();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحديث عرض السعر."); }
    finally { setBusy(false); }
  };
  const convertQuotation = async (event:FormEvent) => {
    event.preventDefault(); if(!convertingQuoteId) return;
    setBusy(true); setError("");
    try {
      await salesApi.convertQuotation(convertingQuoteId,{
        warehouse:Number(quoteDeliveryForm.warehouse),
        delivery_number:quoteDeliveryForm.delivery_number,
        delivery_date:quoteDeliveryForm.delivery_date,
        expiry_dates:quoteDeliveryForm.expiry_dates,
      });
      setConvertingQuoteId(null); setQuoteDeliveryForm(old=>({...old,delivery_number:""}));
      await refresh(); setTab("deliveries");
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحويل عرض السعر إلى إذن تسليم."); }
    finally { setBusy(false); }
  };

  const createOrder = async (event: FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    setBusy(true); setError("");
    try {
      await salesApi.createOrder({
        company:companyId, customer:Number(orderForm.customer), warehouse:Number(orderForm.warehouse),
        order_number:orderForm.order_number, order_date:orderForm.order_date,
        requested_delivery_date:orderForm.requested_delivery_date || null, notes:orderForm.notes,
        lines:orderLines.map(line=>({product:Number(line.product),quantity:line.quantity,unit_price:line.unit_price,
          tax_rate:line.tax_rate, tax_code:line.tax_code ? Number(line.tax_code) : null,
          discount_percent:line.discount_percent || "0"})),
      });
      setShowForm(false); setOrderForm(old=>({...old,order_number:"",notes:""}));
      setOrderLines([{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]);
      await refresh();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء أمر البيع."); }
    finally { setBusy(false); }
  };

  // إنشاء مستند تسليم بالأصناف والكميات والمخزن وتفاصيل الصلاحية.
const createDelivery = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      await salesApi.createDelivery({
        company: companyId, customer: Number(orders.find(o=>o.id===Number(deliveryForm.order))?.customer || deliveryForm.customer), warehouse: Number(orders.find(o=>o.id===Number(deliveryForm.order))?.warehouse || deliveryForm.warehouse),
        order: deliveryForm.order ? Number(deliveryForm.order) : null,
        delivery_number: deliveryForm.delivery_number, delivery_date: deliveryForm.delivery_date,
        lines: deliveryLines.map(line => ({ product: Number(line.product), quantity: line.quantity, expiry_date: line.expiry_date || null, ...(line.order_line ? {order_line:Number(line.order_line)} : {}) })),
      });
      setShowForm(false);
      setDeliveryForm(old => ({ ...old, order:"", delivery_number: "" }));
      setDeliveryLines([{ product: "", quantity: "1", expiry_date: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن التسليم."); }
    finally { setBusy(false); }
  };
  // إنشاء فاتورة بضاعة من إذن تسليم أو فاتورة خدمة مباشرة على فرع الشركة.
const createInvoice = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    const delivery = invoiceForm.mode === "delivery" ? deliveries.find(d => d.id === Number(invoiceForm.delivery)) : null;
    if (invoiceForm.mode === "delivery" && (!delivery || !delivery.line_items.length)) { setError("اختر إذن تسليم صالحًا."); return; }
    const customer = delivery ? delivery.customer : Number(invoiceForm.customer);
    if (!customer || (invoiceForm.mode === "service" && (!invoiceForm.branch || !invoiceLines.every(line => line.product && Number(line.quantity) > 0)))) {
      setError("حدد العميل والفرع وأدخل خدمة وكمية صحيحة."); return;
    }
    setBusy(true); setError("");
    try {
      const payload = {
        company: companyId, customer, delivery: delivery?.id ?? null,
        branch: invoiceForm.mode === "service" ? Number(invoiceForm.branch) : null,
        invoice_number: invoiceForm.invoice_number, invoice_date: invoiceForm.invoice_date,
        transaction_currency: invoiceForm.transaction_currency ? Number(invoiceForm.transaction_currency) : null,
        exchange_rate: invoiceForm.transaction_currency ? invoiceForm.exchange_rate : null,
        exchange_rate_date: invoiceForm.transaction_currency ? invoiceForm.exchange_rate_date : null,
        lines: invoiceLines.map(line => ({
          ...(invoiceForm.mode === "delivery" ? { delivery_line: line.delivery_line, quantity: line.quantity } : { product: line.product, quantity: line.quantity }),
          ...(invoiceForm.transaction_currency ? { transaction_unit_price: line.unit_price } : { unit_price: line.unit_price }),
          tax_rate: line.tax_rate, tax_code: line.tax_code ? Number(line.tax_code) : null,
          ...(line.discount_percent !== "" ? { discount_percent: line.discount_percent } : {}),
        })),
      };
      if (editingInvoiceId) await salesApi.updateInvoice(editingInvoiceId, { lines: payload.lines });
      else await salesApi.createInvoice(payload);
      setShowForm(false); setEditingInvoiceId(null);
      setInvoiceForm(old => ({ ...old, delivery: "", customer: "", branch: "", invoice_number: "" }));
      setInvoiceLines([]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء فاتورة المبيعات."); }
    finally { setBusy(false); }
  };
  // تحميل بيانات الفاتورة المختارة داخل نموذج التعديل دون إرسال تغييرات للخادم.
const beginEditInvoice = (invoice: SalesInvoice) => {
    setTab("invoices"); setShowForm(true); setError(""); setEditingInvoiceId(invoice.id);
    setInvoiceForm({ mode: invoice.delivery ? "delivery" : "service", delivery: invoice.delivery ? String(invoice.delivery) : "", branch: invoice.branch ? String(invoice.branch) : "", customer: String(invoice.customer), invoice_number: invoice.invoice_number, invoice_date: invoice.invoice_date, transaction_currency: invoice.transaction_currency ? String(invoice.transaction_currency) : "", exchange_rate: invoice.exchange_rate || "", exchange_rate_date: invoice.exchange_rate_date || invoice.invoice_date });
    setInvoiceLines(invoice.invoice_lines.map(line => ({
      delivery_line: line.delivery_line ?? undefined, product: line.product, product_name: line.product_name, quantity: line.quantity,
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
        ? multiplyDecimalToFixed(receiptForm.transaction_amount, receiptForm.exchange_rate, 4)
        : receiptForm.amount;
      await salesApi.createReceipt({ company: companyId, customer: invoice.customer, invoice: invoice.id,
        receipt_number: receiptForm.receipt_number, receipt_date: receiptForm.receipt_date,
        method: receiptForm.method, payment_account: receiptForm.payment_account ? Number(receiptForm.payment_account) : null, amount: baseAmount,
        ...(invoice.transaction_currency ? { transaction_currency: invoice.transaction_currency, transaction_amount: receiptForm.transaction_amount, exchange_rate: receiptForm.exchange_rate, exchange_rate_date: receiptForm.exchange_rate_date } : {}),
        reference: receiptForm.reference });
      setShowForm(false); setReceiptForm(old => ({ ...old, invoice: "", receipt_number: "", amount: "", reference: "" }));
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء سند القبض."); }
    finally { setBusy(false); }
  };
  const createReturn = async (event:FormEvent) => {
    event.preventDefault();
    if(!companyId)return;
    const invoice=invoices.find(row=>row.id===Number(returnForm.invoice));
    if(!invoice || invoice.status!=="posted"){setError("اختر فاتورة مبيعات مرحلة.");return;}
    if(!returnForm.invoice_line){setError("اختر بند الفاتورة المراد إرجاعه.");return;}
    setBusy(true);setError("");
    try {
      await salesApi.createReturn({
        company:companyId, invoice:invoice.id, return_number:returnForm.return_number,
        return_date:returnForm.return_date, notes:returnForm.notes,
        lines:[{invoice_line:Number(returnForm.invoice_line),quantity:returnForm.quantity}],
      });
      setShowForm(false);setReturnForm(old=>({...old,invoice:"",invoice_line:"",return_number:"",quantity:"1",notes:""}));
      await refresh();
    } catch(reason){setError(reason instanceof Error?reason.message:"تعذر إنشاء مردود المبيعات.");}
    finally{setBusy(false);}
  };

  // تحميل تقرير أعمار مديونيات العملاء للشركة.
const loadAging = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setAging(await salesApi.customerAging(companyId, agingAsOf)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل أعمار مديونية العملاء."); }
    finally { setBusy(false); }
  };

  const beginFxRevaluation = async (invoice:SalesInvoice) => { setFxInvoice(invoice); setError(""); const today=new Date().toISOString().slice(0,10); const approved=findRate(String(invoice.transaction_currency),today); setFxForm({revaluation_date:today,rate_date:approved?.rate_date||today,rate:approved?.rate||""}); try { setFxHistory(await salesApi.fxRevaluations(invoice.id)); } catch(reason) { setFxHistory([]); setError(reason instanceof Error?reason.message:"تعذر تحميل سجل إعادة التقييم."); } };
  const submitFxRevaluation = async (event:FormEvent) => { event.preventDefault(); if(!fxInvoice)return; setBusy(true); setError(""); try { await salesApi.revalueInvoice(fxInvoice.id,fxForm); setFxHistory(await salesApi.fxRevaluations(fxInvoice.id)); await refresh(); setFxInvoice(null); } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إعادة تقييم فاتورة العميل."); } finally { setBusy(false); } };

  // تنفيذ إجراء ترحيل مستند بيع مع معالجة حالة الانتظار والأخطاء وتحديث القوائم.
const post = async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل المستند."); }
    finally { setBusy(false); }
  };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر الشركة لعرض دورة المبيعات.</div></section>;
  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>دورة المبيعات والتحصيل</h2><p>عروض أسعار، تسليم مخزني، فواتير مبيعات، ثم سند قبض مرتبط بحساب العميل.</p></div><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>setShowTemplatesPanel(value=>!value)}>{showTemplatesPanel?"إغلاق قوالب الطباعة":"قوالب الطباعة"}</button><span className="coming-soon">المبيعات</span></div></div>
     {showTemplatesPanel && <DocumentTemplatesPanel companyId={companyId} />}
    {error && <div className="error-banner">{error}</div>}
    <div className="inventory-tabs">
      <button type="button" className={tab === "quotations" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("quotations"); setShowForm(false); setEditingInvoiceId(null); }}>عروض الأسعار 
       <button type="button" className={tab === "orders" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("orders"); setShowForm(false); setEditingInvoiceId(null); }}>أوامر البيع <b>{orders.length}</b></button> <b>{quotations.length}</b></button>
      <button type="button" className={tab === "deliveries" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("deliveries"); setShowForm(false); setEditingInvoiceId(null); }}>أذون التسليم <b>{deliveries.length}</b></button>
      <button type="button" className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); setEditingInvoiceId(null); }}>فواتير المبيعات <b>{invoices.length}</b></button>
      <button type="button" className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); setEditingInvoiceId(null); }}>سندات القبض <b>{receipts.length}</b></button>
      <button type="button" className={tab === "returns" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("returns"); setShowForm(false); setEditingInvoiceId(null); }}>مردودات المبيعات <b>{returns.length}</b></button>
      <button type="button" className={tab === "aging" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("aging"); setShowForm(false); setEditingInvoiceId(null); setAging(null); }}>أعمار مديونية العملاء</button>
      <button type="button" className={tab === "reports" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("reports"); setShowForm(false); setEditingInvoiceId(null); }}>تقارير المبيعات</button>
      <button type="button" className={tab === "receipt-batches" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipt-batches"); setShowForm(false); setEditingInvoiceId(null); }}>تحصيل مجمع / دفعات مقدمة</button>
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
          <div><span>دفعات مقدمة غير مخصصة</span><strong>{money(aging.totals.unallocated_advances)}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>كود العميل</th><th>العميل</th><th>غير مستحق</th><th>1–30</th><th>31–60</th><th>61–90</th><th>أكثر من 90</th><th>الإجمالي</th><th>دفعات مقدمة</th></tr></thead>
          <tbody>{aging.customers.map(row => <tr key={row.customer}><td className="code-cell">{row.customer_code}</td><td>{row.customer_name}<details className="aging-invoice-details"><summary>تفاصيل الفواتير ({row.invoices.length})</summary><div className="aging-invoice-list">{row.invoices.map(invoice => <div key={invoice.invoice}><span>{invoice.invoice_number} · استحقاق {invoice.due_date}</span><strong>{money(invoice.balance_due)}</strong></div>)}</div></details></td><td>{money(row.not_due)}</td><td>{money(row["1_30"])}</td><td>{money(row["31_60"])}</td><td>{money(row["61_90"])}</td><td>{money(row.over_90)}</td><td><strong>{money(row.total_due)}</strong></td><td>{money(row.unallocated_advances)}</td></tr>)}</tbody>
        </table>{aging.customers.length === 0 && <div className="table-empty">لا توجد أرصدة مستحقة على العملاء في تاريخ التقرير.</div>}</div>
        <p className="inventory-report-note">يعتمد التقرير على الفواتير المرحلة فقط، ويخصم سندات القبض المرحلة حتى تاريخ التقرير. تاريخ الاستحقاق = تاريخ الفاتورة + أجل السداد المسجل للعميل.</p>
      </div>}
    </div>}
    {tab === "reports" && <SalesReportsPanel companyId={companyId} customers={customers} products={products} branches={branches} />}
    {tab === "receipt-batches" && <CustomerReceiptBatchesPanel companyId={companyId} customers={customers} invoices={invoices} treasuryAccounts={treasuryAccounts} onRefresh={refresh} onPrint={batch=>setPrintTarget({kind:"receipt_batch",id:batch.id})} />}
    <div className="customer-toolbar" style={{display:tab === "reports" || tab === "receipt-batches" ? "none" : undefined}}><div className="customer-count">عدد المستندات <strong>{(tab === "orders" ? orders : tab === "deliveries" ? deliveries : tab === "invoices" ? invoices : tab === "receipts" ? receipts : tab === "returns" ? returns : tab === "quotations" ? quotations : []).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو العميل…" value={search} onChange={e => setSearch(e.target.value)} /><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button><button type="button" className="primary-button" disabled={busy || tab === "aging" || tab === "reports" || tab === "receipt-batches"} onClick={() => { if (showForm) { setEditingInvoiceId(null); setInvoiceLines([]); setInvoiceForm(old => ({...old, delivery:"", invoice_number:""})); } if (tab === "quotations") { setEditingQuotationId(null); setQuoteForm(old => ({...old, customer:"", quotation_number:"", notes:""})); setQuoteLines([{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]); } setShowForm(v => !v); }}>{showForm ? "إغلاق النموذج" : tab === "orders" ? "أمر بيع جديد" : tab === "deliveries" ? "إذن تسليم جديد" : tab === "invoices" ? "فاتورة جديدة" : tab === "receipts" ? "سند قبض جديد" : tab === "returns" ? "مردود مبيعات جديد" : tab === "quotations" ? "عرض سعر جديد" : "التقرير"}</button></div>

    {showForm && tab === "orders" && <form className="inventory-form" onSubmit={createOrder}>
      <label>العميل<select required value={orderForm.customer} onChange={e=>setOrderForm({...orderForm,customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
      <label>مخزن التسليم<select required value={orderForm.warehouse} onChange={e=>setOrderForm({...orderForm,warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
      <label>رقم أمر البيع<input required maxLength={80} value={orderForm.order_number} onChange={e=>setOrderForm({...orderForm,order_number:e.target.value})}/></label>
      <label>تاريخ الأمر<input required type="date" value={orderForm.order_date} onChange={e=>setOrderForm({...orderForm,order_date:e.target.value})}/></label>
      <label>تاريخ التسليم المطلوب<input type="date" min={orderForm.order_date} value={orderForm.requested_delivery_date} onChange={e=>setOrderForm({...orderForm,requested_delivery_date:e.target.value})}/></label>
      <div className="inventory-line-editor inventory-wide"><div className="inventory-line-heading"><strong>بنود أمر البيع</strong><button type="button" className="subtle-button" onClick={()=>setOrderLines(old=>[...old,{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}])}>إضافة بند</button></div>
        {orderLines.map((line,index)=><div className="inventory-form-row" key={index}>
          <label>الصنف<select required value={line.product} onChange={e=>{const product=products.find(x=>x.id===Number(e.target.value));setOrderLines(old=>old.map((x,i)=>i===index?{...x,product:e.target.value,unit_price:product?.sale_price||"0"}:x));}}><option value="">اختر الصنف</option>{products.filter(x=>x.is_active&&x.product_type==="goods").map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
          <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setOrderLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/></label>
          <label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={e=>setOrderLines(old=>old.map((x,i)=>i===index?{...x,unit_price:e.target.value}:x))}/></label>
          <label>الضريبة %<input type="number" min="0" max="100" step="0.0001" value={line.tax_rate} onChange={e=>setOrderLines(old=>old.map((x,i)=>i===index?{...x,tax_rate:e.target.value}:x))}/></label>
          <label>كود الضريبة<select value={line.tax_code} onChange={e=>{const code=taxCodes.find(x=>x.id===Number(e.target.value));setOrderLines(old=>old.map((x,i)=>i===index?{...x,tax_code:e.target.value,tax_rate:code?.rate||"0"}:x));}}><option value="">بدون كود</option>{taxCodes.map(code=><option key={code.id} value={code.id}>{code.code} · {code.name_ar}</option>)}</select></label>
          <label>خصم %<input type="number" min="0" max="100" step="0.0001" value={line.discount_percent} onChange={e=>setOrderLines(old=>old.map((x,i)=>i===index?{...x,discount_percent:e.target.value}:x))}/></label>
          <button type="button" className="subtle-button" disabled={orderLines.length===1} onClick={()=>setOrderLines(old=>old.filter((_,i)=>i!==index))}>حذف</button>
        </div>)}
      </div>
      <label className="inventory-wide">ملاحظات<textarea rows={2} value={orderForm.notes} onChange={e=>setOrderForm({...orderForm,notes:e.target.value})}/></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy?"جارٍ الحفظ…":"حفظ أمر البيع كمسودة"}</button></div>
    </form>}
    {showForm && tab === "quotations" && <form className="inventory-form" onSubmit={createQuotation}>
      <label>العميل<select required value={quoteForm.customer} onChange={e=>setQuoteForm({...quoteForm,customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
      <label>رقم عرض السعر<input required maxLength={80} value={quoteForm.quotation_number} onChange={e=>setQuoteForm({...quoteForm,quotation_number:e.target.value})}/></label>
      <label>تاريخ العرض<input required type="date" value={quoteForm.quotation_date} onChange={e=>setQuoteForm({...quoteForm,quotation_date:e.target.value})}/></label>
      <label>صالح حتى<input required type="date" min={quoteForm.quotation_date} value={quoteForm.valid_until} onChange={e=>setQuoteForm({...quoteForm,valid_until:e.target.value})}/></label>
      <div className="inventory-line-editor inventory-wide"><div className="inventory-line-heading"><strong>بنود عرض السعر</strong><button type="button" className="subtle-button" onClick={()=>setQuoteLines(old=>[...old,{product:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}])}>إضافة بند</button></div>
        {quoteLines.map((line,index)=><div className="inventory-form-row" key={index}>
          <label>الصنف<select required value={line.product} onChange={e=>{const product=products.find(x=>x.id===Number(e.target.value));setQuoteLines(old=>old.map((x,i)=>i===index?{...x,product:e.target.value,unit_price:product?.sale_price||"0"}:x));}}><option value="">اختر الصنف</option>{products.filter(x=>x.is_active && x.product_type === "goods").map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
          <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setQuoteLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/></label>
          <label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={e=>setQuoteLines(old=>old.map((x,i)=>i===index?{...x,unit_price:e.target.value}:x))}/></label>
          <label>الضريبة %<input type="number" min="0" max="100" step="0.0001" value={line.tax_rate} onChange={e=>setQuoteLines(old=>old.map((x,i)=>i===index?{...x,tax_rate:e.target.value}:x))}/></label>
          <label>خصم %<input type="number" min="0" max="100" step="0.0001" placeholder="تلقائي حسب القواعد" value={line.discount_percent} onChange={e=>setQuoteLines(old=>old.map((x,i)=>i===index?{...x,discount_percent:e.target.value}:x))}/></label>
          <button type="button" className="subtle-button" disabled={quoteLines.length===1} onClick={()=>setQuoteLines(old=>old.filter((_,i)=>i!==index))}>حذف البند</button>
        </div>)}
      </div>
      <label className="inventory-wide">ملاحظات<textarea rows={2} value={quoteForm.notes} onChange={e=>setQuoteForm({...quoteForm,notes:e.target.value})}/></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy?"جارٍ الحفظ…":editingQuotationId?"حفظ تعديلات العرض":"حفظ عرض السعر كمسودة"}</button></div>
    </form>}
    {convertingQuoteId && <form className="inventory-form" onSubmit={convertQuotation}>
      <strong className="inventory-wide">تحويل عرض السعر إلى إذن تسليم مسودة</strong>
      <label>المخزن<select required value={quoteDeliveryForm.warehouse} onChange={e=>setQuoteDeliveryForm({...quoteDeliveryForm,warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
      <label>رقم إذن التسليم<input required maxLength={37} value={quoteDeliveryForm.delivery_number} onChange={e=>setQuoteDeliveryForm({...quoteDeliveryForm,delivery_number:e.target.value})}/></label>
      <label>تاريخ التسليم<input required type="date" value={quoteDeliveryForm.delivery_date} onChange={e=>setQuoteDeliveryForm({...quoteDeliveryForm,delivery_date:e.target.value})}/></label>
      {quotations.find(q=>q.id===convertingQuoteId)?.quotation_lines.filter(line=>products.find(p=>p.id===line.product)?.track_expiry).map(line=><label key={line.id}>تاريخ صلاحية {line.product_name}<input required type="date" value={quoteDeliveryForm.expiry_dates[String(line.id)]||""} onChange={e=>setQuoteDeliveryForm(old=>({...old,expiry_dates:{...old.expiry_dates,[String(line.id)]:e.target.value}}))}/></label>)}
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>إنشاء إذن تسليم مسودة</button><button type="button" className="subtle-button" onClick={()=>setConvertingQuoteId(null)}>إلغاء</button></div>
    </form>}

    {showForm && tab === "deliveries" && <form className="inventory-form" onSubmit={createDelivery}>
      <label>أمر البيع (اختياري)<select value={deliveryForm.order} onChange={e=>{const order=orders.find(o=>o.id===Number(e.target.value));setDeliveryForm({...deliveryForm,order:e.target.value,customer:order?String(order.customer):"",warehouse:order?String(order.warehouse):""});setDeliveryLines([{product:"",quantity:"1",expiry_date:""}]);}}><option value="">إذن تسليم عادي بدون أمر بيع</option>{orders.filter(o=>o.status==="confirmed"||o.status==="partially_delivered").map(o=><option key={o.id} value={o.id}>{o.order_number} · {o.customer_name}</option>)}</select></label>
      <label>العميل<select required disabled={!!deliveryForm.order} value={deliveryForm.customer} onChange={e => setDeliveryForm({...deliveryForm, customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(c => c.is_active).map(c => <option key={c.id} value={c.id}>{c.code} · {c.name_ar}</option>)}</select></label>
      <label>مخزن الصرف<select required disabled={!!deliveryForm.order} value={deliveryForm.warehouse} onChange={e => setDeliveryForm({...deliveryForm, warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.filter(w => w.is_active).map(w => <option key={w.id} value={w.id}>{w.code} · {w.name_ar}</option>)}</select></label>
      <label>رقم إذن التسليم<input required maxLength={37} value={deliveryForm.delivery_number} onChange={e => setDeliveryForm({...deliveryForm, delivery_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={deliveryForm.delivery_date} onChange={e => setDeliveryForm({...deliveryForm, delivery_date:e.target.value})} /></label>
      <div className="inventory-line-editor inventory-wide"><div className="inventory-line-heading"><strong>أصناف التسليم</strong><button type="button" className="subtle-button" onClick={() => setDeliveryLines(old => [...old, { product: "", quantity: "1", expiry_date: "" }])}>إضافة صنف</button></div>
        {deliveryLines.map((line, index) => { const selected = products.find(p => p.id === Number(line.product)); const selectedOrder = orders.find(o=>o.id===Number(deliveryForm.order)); const selectedOrderLine = selectedOrder?.order_lines.find(x=>x.id===Number(line.order_line)); return <div className="inventory-form-row" key={index}>
          {selectedOrder ? <label>بند أمر البيع<select required value={line.order_line||""} onChange={e=>{const orderLine=selectedOrder.order_lines.find(x=>x.id===Number(e.target.value));setDeliveryLines(old=>old.map((x,i)=>i===index?{...x,order_line:e.target.value,product:orderLine?String(orderLine.product):"",quantity:orderLine?.remaining_quantity||"1",expiry_date:""}:x));}}><option value="">اختر بندًا متبقيًا</option>{selectedOrder.order_lines.filter(x=>Number(x.remaining_quantity)>0).map(x=><option key={x.id} value={x.id}>{x.product_code} · {x.product_name} — متبقي {x.remaining_quantity}</option>)}</select></label> : <label>الصنف<select required value={line.product} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, product:e.target.value, expiry_date:""} : x))}><option value="">اختر الصنف</option>{products.filter(p => p.is_active && p.product_type === "goods").map(p => <option key={p.id} value={p.id}>{p.code} · {p.name_ar}</option>)}</select></label>}
          <label>الكمية<input required type="number" min="0.0001" max={selectedOrderLine?.remaining_quantity} step="0.0001" value={line.quantity} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, quantity:e.target.value} : x))} /></label>
          {selected?.track_expiry && <label>تاريخ الصلاحية<input required type="date" value={line.expiry_date} onChange={e => setDeliveryLines(old => old.map((x,i) => i===index ? {...x, expiry_date:e.target.value} : x))} /></label>}
          <button type="button" className="subtle-button" disabled={deliveryLines.length===1} onClick={() => setDeliveryLines(old => old.filter((_,i) => i!==index))}>حذف</button>
        </div>; })}
      </div>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إذن التسليم كمسودة"}</button></div>
    </form>}
    {showForm && tab === "invoices" && <form className="inventory-form" onSubmit={createInvoice}>
      <label>نوع الفاتورة<select disabled={!!editingInvoiceId} value={invoiceForm.mode} onChange={e=>{const mode=e.target.value as "delivery"|"service";setInvoiceForm({...invoiceForm,mode,delivery:"",customer:"",branch:""});setInvoiceLines(mode==="service"?[{product:undefined,product_name:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}]:[]);}}><option value="delivery">فاتورة بضاعة من إذن تسليم</option><option value="service">فاتورة خدمة مباشرة</option></select></label>
      {invoiceForm.mode === "delivery" ? <label>إذن التسليم<select required disabled={!!editingInvoiceId} value={invoiceForm.delivery} onChange={e => { const d=deliveries.find(x=>x.id===Number(e.target.value)); setInvoiceForm({...invoiceForm, delivery:e.target.value}); setInvoiceLines(d ? d.line_items.filter(line => remainingDeliveryQuantity(line) > 0).map(line => { const index=d.line_items.findIndex(item=>item.id===line.id); const quoteLine=d.order_snapshot?.lines[index] || d.quotation_snapshot?.lines[index]; return {delivery_line:line.id, product:line.product, product_name:line.product_name, quantity:String(remainingDeliveryQuantity(line)), unit_price:quoteLine?.unit_price || products.find(p=>p.id===line.product)?.sale_price || "0", tax_rate:quoteLine?.tax_rate || "0", tax_code:quoteLine?.tax_code ? String(quoteLine.tax_code) : "", discount_percent:quoteLine?.discount_percent || ""}; }) : []); }}><option value="">اختر إذن التسليم</option>{invoiceable.map(d => <option key={d.id} value={d.id}>{d.delivery_number} · {d.customer_name}</option>)}</select></label> : <>
        <label>العميل<select required disabled={!!editingInvoiceId} value={invoiceForm.customer} onChange={e=>setInvoiceForm({...invoiceForm,customer:e.target.value})}><option value="">اختر العميل</option>{customers.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
        <label>الفرع<select required disabled={!!editingInvoiceId} value={invoiceForm.branch} onChange={e=>setInvoiceForm({...invoiceForm,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.filter(x=>x.is_active).map(x=><option key={x.id} value={x.id}>{x.code} · {x.name_ar}</option>)}</select></label>
      </>}
      <label>رقم الفاتورة<input required disabled={!!editingInvoiceId} value={invoiceForm.invoice_number} onChange={e => setInvoiceForm({...invoiceForm, invoice_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" disabled={!!editingInvoiceId} value={invoiceForm.invoice_date} onChange={e => setInvoiceForm({...invoiceForm, invoice_date:e.target.value})} /></label>
      <label>عملة الفاتورة<select disabled={!!editingInvoiceId} value={invoiceForm.transaction_currency} onChange={e => { const rate=e.target.value ? findRate(e.target.value, invoiceForm.invoice_date) : null; setInvoiceForm({...invoiceForm, transaction_currency:e.target.value, exchange_rate:rate?.rate || "", exchange_rate_date:rate?.rate_date || invoiceForm.invoice_date}); }}><option value="">العملة الأساسية للشركة</option>{currencies.map(currency=><option key={currency.id} value={currency.id}>{currency.code} · {currency.name_ar}</option>)}</select></label>
      {invoiceForm.transaction_currency && <>
        <label>تاريخ سعر الصرف<input required type="date" value={invoiceForm.exchange_rate_date} onChange={e => { const rate=findRate(invoiceForm.transaction_currency,e.target.value); setInvoiceForm({...invoiceForm, exchange_rate_date:e.target.value, exchange_rate:rate?.rate || invoiceForm.exchange_rate}); }} /></label>
        <label>سعر الصرف مقابل العملة الأساسية<input required type="number" min="0.0000000001" step="0.0000000001" value={invoiceForm.exchange_rate} onChange={e => setInvoiceForm({...invoiceForm, exchange_rate:e.target.value})} /></label>
        <p className="inventory-wide">أدخل أسعار الوحدات بالعملة الأجنبية؛ سيُحفظ المقابل بالعملة الأساسية في الأستاذ العام.</p>
      </>}
      {invoiceLines.map((line,index) => <div className="inventory-form-row" key={line.delivery_line ?? `service-${index}`}>
        {invoiceForm.mode === "service" ? <>
          <label>الخدمة<select required value={line.product ? String(line.product) : ""} onChange={e=>{const product=products.find(p=>p.id===Number(e.target.value));setInvoiceLines(old=>old.map((x,i)=>i===index?{...x,product:product?.id,product_name:product?.name_ar||"",unit_price:product?.sale_price||"0"}:x));}}><option value="">اختر الخدمة</option>{products.filter(p=>p.product_type==="service"&&p.is_active).map(p=><option key={p.id} value={p.id}>{p.code} · {p.name_ar}</option>)}</select></label>
          <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setInvoiceLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/></label>
          <button type="button" className="subtle-button" disabled={invoiceLines.length===1} onClick={()=>setInvoiceLines(old=>old.filter((_,i)=>i!==index))}>حذف الخدمة</button>
        </> : <>
          <label>الصنف<input readOnly value={line.product_name} /></label><label>الكمية المطلوب فوترتها<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setInvoiceLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/><small>المتاح للفوترة: {line.quantity}</small></label>
        </>}
        <label>{invoiceForm.transaction_currency ? "سعر الوحدة بالعملة الأجنبية" : "سعر الوحدة بالعملة الأساسية"}<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,unit_price:e.target.value}:x))} /></label>
        <label>كود الضريبة<select value={line.tax_code} onChange={e => { const selected=taxCodes.find(code=>code.id===Number(e.target.value)); setInvoiceLines(old=>old.map((x,i)=>i===index?{...x,tax_code:e.target.value,tax_rate:selected?.rate || "0"}:x)); }}><option value="">بدون كود — إدخال يدوي</option>{taxCodes.map(code=><option key={code.id} value={code.id}>{code.code} · {code.name_ar} · {code.rate}%</option>)}</select></label>
        {!line.tax_code && <label>الضريبة %<input required type="number" min="0" max="100" step="0.0001" value={line.tax_rate} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,tax_rate:e.target.value}:x))} /></label>}
        <label>خصم %<input type="number" min="0" max="100" step="0.0001" placeholder="تلقائي حسب قواعد العميل" value={line.discount_percent} onChange={e => setInvoiceLines(old => old.map((x,i)=>i===index?{...x,discount_percent:e.target.value}:x))} /></label>
      </div>)}
      {invoiceForm.mode==="service" && <button type="button" className="subtle-button" onClick={()=>setInvoiceLines(old=>[...old,{product:undefined,product_name:"",quantity:"1",unit_price:"0",tax_rate:"0",tax_code:"",discount_percent:""}])}>إضافة خدمة أخرى</button>}
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !invoiceLines.length || (invoiceForm.mode==="delivery" ? !invoiceForm.delivery : !invoiceForm.customer || !invoiceForm.branch)}>{busy ? "جارٍ الحفظ…" : editingInvoiceId ? "حفظ تعديلات المسودة" : "حفظ الفاتورة كمسودة"}</button>{editingInvoiceId && <button type="button" className="subtle-button" onClick={() => { setEditingInvoiceId(null); setShowForm(false); setInvoiceLines([]); setInvoiceForm(old => ({...old, delivery:"", customer:"", branch:"", invoice_number:""})); }}>إلغاء التعديل</button>}</div>
    </form>}
    {showForm && tab === "returns" && <form className="inventory-form" onSubmit={createReturn}>
      <label>فاتورة المبيعات<select required value={returnForm.invoice} onChange={e=>setReturnForm({...returnForm,invoice:e.target.value,invoice_line:""})}><option value="">اختر فاتورة مرحلة</option>{invoices.filter(row=>row.status==="posted").map(row=><option key={row.id} value={row.id}>{row.invoice_number} · {row.customer_name}</option>)}</select></label>
      <label>بند الفاتورة<select required value={returnForm.invoice_line} onChange={e=>setReturnForm({...returnForm,invoice_line:e.target.value})}><option value="">اختر الصنف</option>{(invoices.find(row=>row.id===Number(returnForm.invoice))?.invoice_lines || []).map(line=><option key={line.id} value={line.id}>{line.product_code} · {line.product_name} · الكمية {line.quantity}</option>)}</select></label>
      <label>رقم المردود<input required maxLength={37} value={returnForm.return_number} onChange={e=>setReturnForm({...returnForm,return_number:e.target.value})}/></label>
      <label>تاريخ المردود<input required type="date" value={returnForm.return_date} onChange={e=>setReturnForm({...returnForm,return_date:e.target.value})}/></label>
      <label>كمية المرتجع<input required type="number" min="0.0001" step="0.0001" value={returnForm.quantity} onChange={e=>setReturnForm({...returnForm,quantity:e.target.value})}/></label>
      <label>ملاحظات<input value={returnForm.notes} onChange={e=>setReturnForm({...returnForm,notes:e.target.value})}/></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy||!returnForm.invoice_line}>{busy?"جارٍ الحفظ…":"حفظ المردود كمسودة"}</button></div>
    </form>}
    {showForm && tab === "receipts" && <form className="inventory-form" onSubmit={createReceipt}>
      <label>الفاتورة<select required value={receiptForm.invoice} onChange={e => { const invoice=invoices.find(x=>x.id===Number(e.target.value)); const rate=invoice?.transaction_currency ? findRate(String(invoice.transaction_currency), receiptForm.receipt_date) : null; const paidForeign=invoice?.transaction_currency ? receipts.filter(row=>row.invoice===invoice.id && row.status==="posted").reduce((sum,row)=>sum+Number(row.transaction_amount || 0),0) : 0; setReceiptForm({...receiptForm, invoice:e.target.value, amount:invoice?.balance_due || "", transaction_currency:invoice?.transaction_currency ? String(invoice.transaction_currency) : "", transaction_amount:invoice?.transaction_currency ? String(Math.max(Number(invoice.transaction_grand_total || 0)-paidForeign,0)) : "", exchange_rate:rate?.rate || "", exchange_rate_date:rate?.rate_date || receiptForm.receipt_date}); }}><option value="">اختر فاتورة مستحقة</option>{payable.map(i => <option key={i.id} value={i.id}>{i.invoice_number} · {i.customer_name} · متبقي {money(i.balance_due)}</option>)}</select></label>
      <label>رقم السند<input required value={receiptForm.receipt_number} onChange={e => setReceiptForm({...receiptForm, receipt_number:e.target.value})} /></label>
      <label>التاريخ<input required type="date" value={receiptForm.receipt_date} onChange={e => setReceiptForm({...receiptForm, receipt_date:e.target.value})} /></label>
      {receiptForm.transaction_currency && <>
        <label>المبلغ المحصل بالعملة الأجنبية<input required type="number" min="0.0001" step="0.0001" value={receiptForm.transaction_amount} onChange={e => setReceiptForm({...receiptForm, transaction_amount:e.target.value})} /></label>
        <label>تاريخ سعر الصرف<input required type="date" value={receiptForm.exchange_rate_date} onChange={e => { const rate=findRate(receiptForm.transaction_currency,e.target.value); setReceiptForm({...receiptForm, exchange_rate_date:e.target.value, exchange_rate:rate?.rate || receiptForm.exchange_rate}); }} /></label>
        <label>سعر الصرف<input required type="number" min="0.0000000001" step="0.0000000001" value={receiptForm.exchange_rate} onChange={e => setReceiptForm({...receiptForm, exchange_rate:e.target.value})} /></label>
        <label>المقابل بالعملة الأساسية<input readOnly value={formatMoney(multiplyDecimalToFixed(receiptForm.transaction_amount || "0", receiptForm.exchange_rate || "0", 4))} /></label>
      </>}
      <label>طريقة التحصيل<select value={receiptForm.method} onChange={e => setReceiptForm({...receiptForm, method:e.target.value as "cash" | "bank", payment_account:String(treasuryAccounts.find(x => x.is_active && x.kind === (e.target.value as "cash" | "bank"))?.account || "")})}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label className="inventory-wide">حساب الخزينة / البنك<select value={receiptForm.payment_account} onChange={e => setReceiptForm({...receiptForm, payment_account:e.target.value})}><option value="">الحساب الافتراضي حسب طريقة التحصيل</option>{treasuryAccounts.filter(account => account.is_active && account.kind === receiptForm.method).map(account => <option key={account.id} value={account.account}>{account.code} - {account.name_ar} ({account.account_code})</option>)}</select></label>
      {!receiptForm.transaction_currency && <label>المبلغ بالعملة الأساسية<input required type="number" min="0.0001" step="0.0001" value={receiptForm.amount} onChange={e => setReceiptForm({...receiptForm, amount:e.target.value})} /></label>}
      <label>مرجع التحويل/الإيصال<input value={receiptForm.reference} onChange={e => setReceiptForm({...receiptForm, reference:e.target.value})} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !receiptForm.invoice}>{busy ? "جارٍ الحفظ…" : "حفظ سند القبض كمسودة"}</button></div>
    </form>}

    {tab === "quotations" && <div className="data-table-wrap"><table><thead><tr><th>رقم العرض</th><th>العميل</th><th>التاريخ</th><th>صالح حتى</th><th>الخصم</th><th>الإجمالي</th><th>الحالة</th><th>إجراءات</th></tr></thead><tbody>
      {quotations.filter(q=>[q.quotation_number,q.customer_name].join(" ").toLowerCase().includes(search.toLowerCase())).map(q=><tr key={q.id}>
        <td className="code-cell">{q.quotation_number}</td><td>{q.customer_name}</td><td>{q.quotation_date}</td><td>{q.valid_until}</td><td>{money(q.discount_total)}</td><td>{money(q.grand_total)}</td>
        <td><span className={statusPill(q.status)}>{q.status==="draft"?"مسودة":q.status==="sent"?"مرسل":q.status==="accepted"?"مقبول":q.status==="rejected"?"مرفوض":q.status==="expired"?"منتهي":"تم التحويل"}</span></td>
        <td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>setPrintTarget({kind:"quotation",id:q.id})}>معاينة / طباعة</button>
          {q.status==="draft" && <><button className="subtle-button" type="button" disabled={busy} onClick={()=>beginEditQuotation(q)}>تعديل</button><button className="subtle-button" type="button" disabled={busy} onClick={()=>void deleteQuotation(q)}>حذف</button><button className="primary-button" type="button" disabled={busy} onClick={()=>void quoteAction("send",q.id)}>إرسال</button></>}
          {q.status==="sent" && <><button className="primary-button" type="button" disabled={busy} onClick={()=>void quoteAction("accept",q.id)}>قبول</button><button className="subtle-button" type="button" disabled={busy} onClick={()=>void quoteAction("reject",q.id)}>رفض</button></>}
          {q.status==="accepted" && <button className="primary-button" type="button" disabled={busy} onClick={()=>{setConvertingQuoteId(q.id);setQuoteDeliveryForm(old=>({...old,delivery_number:("SD-"+q.quotation_number).slice(0,37),expiry_dates:{}}));}}>تحويل لإذن تسليم</button>}
          {q.converted_delivery_number && <span>{q.converted_delivery_number}</span>}
        </div></td>
      </tr>)}
    </tbody></table>{!busy && quotations.length===0 && <div className="table-empty">لا توجد عروض أسعار.</div>}</div>}

    {tab === "orders" && <div className="data-table-wrap"><table><thead><tr><th>رقم الأمر</th><th>العميل</th><th>تاريخ الأمر</th><th>التسليم المطلوب</th><th>الإجمالي</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
      {orders.filter(o=>[o.order_number,o.customer_name].join(" ").toLowerCase().includes(search.toLowerCase())).map(o=><tr key={o.id}>
        <td className="code-cell">{o.order_number}<details className="aging-invoice-details"><summary>البنود ({o.order_lines.length})</summary><div className="aging-invoice-list">{o.order_lines.map(line=><div key={line.id}><span>{line.product_code} · {line.product_name} — متبقي {line.remaining_quantity}</span><strong>{line.quantity}</strong></div>)}</div></details></td>
        <td>{o.customer_name}</td><td>{o.order_date}</td><td>{o.requested_delivery_date||"—"}</td><td>{money(o.grand_total)}</td>
        <td><span className={statusPill(o.status)}>{o.status==="draft"?"مسودة":o.status==="confirmed"?"معتمد":o.status==="partially_delivered"?"تسليم جزئي":o.status==="delivered"?"مكتمل":"ملغي"}</span></td>
        <td><div className="table-actions">{o.status==="draft"&&<button type="button" className="primary-button" disabled={busy} onClick={()=>void post(()=>salesApi.confirmOrder(o.id))}>اعتماد</button>}{(o.status==="confirmed"||o.status==="partially_delivered")&&<><button type="button" className="primary-button" disabled={busy} onClick={()=>{setTab("deliveries");setShowForm(true);setDeliveryForm(old=>({...old,order:String(o.id),customer:String(o.customer),warehouse:String(o.warehouse),delivery_number:""}));setDeliveryLines([{product:"",quantity:"1",expiry_date:""}]);}}>تسليم كمية</button><button type="button" className="subtle-button" disabled={busy} onClick={()=>void post(()=>salesApi.cancelOrder(o.id))}>إلغاء المتبقي</button></>}</div></td>
      </tr>)}
    </tbody></table>{!busy&&!orders.length&&<div className="table-empty">لا توجد أوامر بيع.</div>}</div>}
    {tab === "deliveries" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>العميل</th><th>المخزن</th><th>التاريخ</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredDeliveries.map(d=><tr key={d.id}><td className="code-cell">{d.delivery_number}</td><td>{d.customer_name}</td><td>{d.warehouse_name}</td><td>{d.delivery_date}</td><td><span className={statusPill(d.movement_status)}>{d.movement_status === "posted" ? "مرحّل" : "مسودة"}</span></td><td>{d.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postDelivery(d.id))}>ترحيل وصرف المخزون</button> : "تم الصرف"}</td></tr>)}</tbody></table>{!busy && !filteredDeliveries.length && <div className="table-empty">لا توجد أذون تسليم.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>إذن التسليم</th><th>التاريخ</th><th>الخصم</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map(i=><tr key={i.id}><td className="code-cell">{i.invoice_number}</td><td>{i.customer_name}</td><td>{i.delivery_number || "خدمة مباشرة"}</td><td>{i.invoice_date}</td><td>{money(i.discount_total)}</td><td>{money(i.net_total)}</td><td>{money(i.tax_total)}</td><td>{money(i.grand_total)}</td><td>{money(i.paid_total)}</td><td>{money(i.balance_due)}</td><td><span className={statusPill(i.status === "posted" && Number(i.balance_due) > 0 && Number(i.paid_total) > 0 ? "partial" : i.status)}>{i.status === "posted" ? (Number(i.balance_due) <= 0 ? "مدفوعة" : Number(i.paid_total) > 0 ? "مدفوعة جزئيًا" : "مرحّلة") : "مسودة"}</span></td><td>{i.journal_number || "—"}</td><td>{i.status === "draft" ? <div className="table-actions"><button type="button" className="subtle-button" disabled={busy} onClick={()=>setPrintTarget({kind:"invoice",id:i.id})}>معاينة / طباعة</button>{(i.approval_status === "pending" || i.approval_status === "rejected" || i.approval_status === "superseded") && <span>{i.approval_status === "pending" ? "بانتظار اعتماد المدير" : i.approval_status === "rejected" ? "الخصم مرفوض — يلزم تعديل" : "طلب سابق — راجع المسودة"}</span>}<button type="button" className="subtle-button" disabled={busy} onClick={() => beginEditInvoice(i)}>تعديل المسودة</button>{(i.approval_status === "not_required" || i.approval_status === "approved") && <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postInvoice(i.id))}>ترحيل الفاتورة</button>}</div> : <div className="table-actions"><button type="button" className="subtle-button" disabled={busy} onClick={()=>setPrintTarget({kind:"invoice",id:i.id})}>معاينة / طباعة</button><span>مرحّلة</span>{i.transaction_currency && Number(i.balance_due)>0 && <button type="button" className="subtle-button" disabled={busy} onClick={()=>void beginFxRevaluation(i)}>إعادة تقييم العملة</button>}</div>}</td></tr>)}</tbody></table>{!busy && !filteredInvoices.length && <div className="table-empty">لا توجد فواتير مبيعات.</div>}</div>}
    {tab === "invoices" && fxInvoice && <form className="inventory-form" onSubmit={submitFxRevaluation}>
      <div className="inventory-wide"><strong>إعادة تقييم رصيد العميل: {fxInvoice.invoice_number}</strong><p>إجمالي الفاتورة بالعملة الأجنبية: {money(fxInvoice.transaction_grand_total || "0")} · المتبقي بالعملة الأساسية: {money(fxInvoice.balance_due)}</p></div>
      <label>تاريخ إعادة التقييم<input required type="date" value={fxForm.revaluation_date} onChange={e=>setFxForm({...fxForm,revaluation_date:e.target.value})}/></label>
      <label>تاريخ سعر الصرف المعتمد<input required type="date" max={fxForm.revaluation_date} value={fxForm.rate_date} onChange={e=>{const rate=findRate(String(fxInvoice.transaction_currency),e.target.value);setFxForm({...fxForm,rate_date:e.target.value,rate:rate?.rate||fxForm.rate});}}/></label>
      <label>سعر الصرف مقابل عملة الشركة<input required type="number" min="0.0000000001" step="0.0000000001" value={fxForm.rate} onChange={e=>setFxForm({...fxForm,rate:e.target.value})}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!fxForm.rate}>{busy?"جارٍ التقييم…":"حفظ وإثبات فرق العملة"}</button><button className="subtle-button" type="button" onClick={()=>setFxInvoice(null)}>إغلاق</button></div>
      {fxHistory.length>0&&<div className="data-table-wrap inventory-wide"><table><thead><tr><th>تاريخ التقييم</th><th>سعر الصرف</th><th>الرصيد الأجنبي</th><th>الفرق</th><th>القيد</th><th>الحالة</th></tr></thead><tbody>{fxHistory.map(row=><tr key={row.id}><td>{row.revaluation_date}</td><td>{row.rate}</td><td>{money(row.foreign_balance)}</td><td>{money(row.difference)}</td><td>{row.journal_number||"—"}</td><td>{row.status==="reversed"?"معكوس":row.status==="no_difference"?"دون فرق":"مرحّل"}</td></tr>)}</tbody></table></div>}
    </form>}
    {tab === "returns" && <div className="data-table-wrap"><table><thead><tr><th>رقم المردود</th><th>العميل</th><th>الفاتورة</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReturns.map(row=><tr key={row.id}><td className="code-cell">{row.return_number}</td><td>{row.customer_name}</td><td>{row.invoice_number}</td><td>{row.return_date}</td><td>{money(row.transaction_currency_code ? row.transaction_net_total || "0" : row.net_total)}{row.transaction_currency_code ? <small>{row.transaction_currency_code} · أساسي: {money(row.net_total)}</small> : null}</td><td>{money(row.transaction_currency_code ? row.transaction_tax_total || "0" : row.tax_total)}{row.transaction_currency_code ? <small>{row.transaction_currency_code} · أساسي: {money(row.tax_total)}</small> : null}</td><td>{money(row.transaction_currency_code ? row.transaction_grand_total || "0" : row.grand_total)}{row.transaction_currency_code ? <small>{row.transaction_currency_code} · أساسي: {money(row.grand_total)}</small> : null}</td><td><span className={statusPill(row.status)}>{row.status === "reversed" ? "معكوس" : row.status === "posted" ? "مرحّل" : "مسودة"}</span></td><td>{row.status==="reversed"?row.reversal_journal_number||"—":row.journal_number||"—"}</td><td><div className="table-actions"><button className="subtle-button" type="button" disabled={busy} onClick={()=>setPrintTarget({kind:"return",id:row.id})}>معاينة / طباعة</button>{row.status==="draft"?<button className="primary-button" type="button" disabled={busy} onClick={()=>void post(()=>salesApi.postReturn(row.id))}>ترحيل المردود</button>:row.status==="reversed"?<span>معكوس</span>:<button className="subtle-button" type="button" disabled={busy} onClick={()=>{if(window.confirm("سيتم صرف الأصناف المرتجعة من نفس طبقات التكلفة الأصلية وإنشاء قيد عكسي للمردود. إذا تم بيع أو نقل الكمية قد يرفض النظام العكس. هل تريد المتابعة؟")) void post(()=>salesApi.reverseReturn(row.id));}}>عكس المردود</button>}</div></td></tr>)}</tbody></table>{!busy&&!filteredReturns.length&&<div className="table-empty">لا توجد مردودات مبيعات.</div>}</div>}
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>العميل</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>حساب التحصيل</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map(r=><tr key={r.id}><td className="code-cell">{r.receipt_number}</td><td>{r.customer_name}</td><td>{r.invoice_number}</td><td>{r.receipt_date}</td><td>{r.method === "cash" ? "نقدي" : "بنكي"}</td><td>{r.payment_account_name || "الحساب الافتراضي"}</td><td>{r.transaction_currency_code ? <>{money(r.transaction_amount || "0")} {r.transaction_currency_code}<small>أساسي: {money(r.amount)}</small></> : money(r.amount)}</td><td><span className={statusPill(r.status)}>{r.status === "reversed" ? "معكوس" : r.status === "posted" ? "مرحّل" : "مسودة"}</span></td><td>{r.journal_number || "—"}</td><td><button className="subtle-button" type="button" disabled={busy} onClick={()=>setPrintTarget({kind:"receipt",id:r.id})}>معاينة / طباعة</button> {r.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void post(() => salesApi.postReceipt(r.id))}>ترحيل سند القبض</button> : r.status === "reversed" ? <span>معكوس</span> : <div className="table-actions"><span>مرحّل</span><button className="subtle-button" type="button" disabled={busy} onClick={() => { if (window.confirm("سيتم إنشاء قيد عكسي وإلغاء أثر سند القبض على رصيد العميل. يجب عكس أحدث سند أولًا. هل تريد المتابعة؟")) void post(() => salesApi.reverseReceipt(r.id)); }}>عكس السند</button></div>}</td></tr>)}</tbody></table>{!busy && !filteredReceipts.length && <div className="table-empty">لا توجد سندات قبض.</div>}</div>}
    {printTarget && <SalesDocumentPrint
      company={companyProfile}
      invoice={printTarget.kind === "invoice" ? invoices.find(row => row.id === printTarget.id) || null : null}
      quotation={printTarget.kind === "quotation" ? quotations.find(row => row.id === printTarget.id) || null : null}
      receipt={printTarget.kind === "receipt" ? receipts.find(row => row.id === printTarget.id) || null : null}
      receiptBatch={printTarget.kind === "receipt_batch" ? receiptBatches.find(row => row.id === printTarget.id) || null : null}
      salesReturn={printTarget.kind === "return" ? returns.find(row => row.id === printTarget.id) || null : null}
      onClose={() => setPrintTarget(null)}
    />}
  </section>;
}
