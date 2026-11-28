/*
 * دورة المشتريات: أوامر الشراء والاستلام وفواتير الموردين والمدفوعات وكشوف الموردين.
 * تربط مستندات الشراء بالمخزون وحساب البضاعة المستلمة غير المفوترة والضريبة والمورد.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import PurchaseDocumentPrint, { type PurchasePrintDocument } from "./PurchaseDocumentPrint";
import DocumentTemplatesPanel from "./DocumentTemplatesPanel";
import { divideDecimalToFixed, formatMoney, multiplyDecimalToFixed } from "./formatters";
import { api, inventoryApi, purchasingApi, treasuryApi, type Product, type PurchaseInvoice, type PurchaseOrder, type PurchaseReceipt, type Supplier, type Account, type Branch, type CostCenter, type Company, type SupplierAging, type SupplierPayment, type SupplierReturn, type SupplierRefund, type SupplierStatement, type TreasuryAccount, type Warehouse, type TaxCode, type Currency, type ExchangeRate, type FxSubledgerRevaluation, type PurchaseRequisition, type PurchaseRFQ, type PurchaseServiceAcceptance, type SupplierQuotation, type GRNIAgingReport, type LandedCostDocument, type LandedCostReport, type PurchaseMatchingReport, type PurchaseAnalysisReport, type BranchPurchasePolicy } from "./api";

type Props = { companyId: number | null; company?: Company | null; activityAssignments?: NonNullable<Company["activity_assignments"]>; primaryActivity?: Company["activity"] };
type ReceiptDraftLine = { product: string; quantity: string; unit_cost: string; expiry_date: string; purchase_order_line?: string };
const money = (value: string) => formatMoney(value);
const decimalToUnits = (value: string): bigint | null => {
  const normalized = value.trim();
  if (!/^\d+(?:\.\d+)?$/.test(normalized)) return null;
  const [whole, fraction = ""] = normalized.split(".");
  if (fraction.length > 4 && /[^0]/.test(fraction.slice(4))) return null;
  return BigInt(whole) * 10000n + BigInt((fraction.slice(0, 4) + "0000").slice(0, 4));
};
const unitsToDecimal = (value: bigint): string =>
  `${value / 10000n}.${(value % 10000n).toString().padStart(4, "0")}`;
const roundedShareUnits = (amount: bigint, weight: bigint, totalWeight: bigint): bigint => {
  const numerator = amount * weight;
  const quotient = numerator / totalWeight;
  const remainder = numerator % totalWeight;
  const doubled = remainder * 2n;
  return doubled > totalWeight || (doubled === totalWeight && quotient % 2n === 1n)
    ? quotient + 1n : quotient;
};

const statusPill = (status: string) => `status-pill ${{ draft: "draft", submitted: "sent", approved: "accepted", rejected: "cancelled", converted: "posted", open: "sent", closed: "cancelled", awarded: "accepted", posted: "posted", reversed: "reversed", confirmed: "sent", partially_received: "partial", received: "accepted", cancelled: "cancelled", partial: "partial" } as Record<string, string>}[status] || "unknown"}`;

// شاشة دورة المشتريات وتبويبات أوامر الشراء والاستلام والفواتير والمدفوعات.
export default function PurchasingPanel({ companyId, company, activityAssignments, primaryActivity }: Props) {
  const activeActivityAssignments = (activityAssignments || []).filter((item) => item.is_active);
  const [tab, setTab] = useState<"requisitions" | "sourcing" | "orders" | "service-acceptances" | "receipts" | "invoices" | "payments" | "returns" | "refunds" | "statement" | "aging" | "landed-cost" | "purchase-policies">("receipts");
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [costCenters, setCostCenters] = useState<CostCenter[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [branchPurchasePolicies, setBranchPurchasePolicies] = useState<BranchPurchasePolicy[]>([]);
  const [policyForm, setPolicyForm] = useState({ branch: "", purchase_enabled: true, restrict_suppliers: false, allowed_supplier_ids: [] as string[] });
  const [taxCodes, setTaxCodes] = useState<TaxCode[]>([]);
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRate[]>([]);
  const [treasuryAccounts, setTreasuryAccounts] = useState<TreasuryAccount[]>([]);
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [orders, setOrders] = useState<PurchaseOrder[]>([]);
  const [serviceAcceptances, setServiceAcceptances] = useState<PurchaseServiceAcceptance[]>([]);
  const [requisitions, setRequisitions] = useState<PurchaseRequisition[]>([]);
  const [rfqs, setRfqs] = useState<PurchaseRFQ[]>([]);
  const [sourcingView, setSourcingView] = useState<"rfqs" | "quotations">("rfqs");
  const [quotations, setQuotations] = useState<SupplierQuotation[]>([]);
  const [rfqForm, setRfqForm] = useState({rfq_number:"",issue_date:new Date().toISOString().slice(0,10),closing_date:new Date(Date.now()+7*86400000).toISOString().slice(0,10),terms:""});
  const [rfqSuppliers, setRfqSuppliers] = useState<string[]>([]);
  const [rfqLines, setRfqLines] = useState<{product:string;quantity:string;notes:string}[]>([{product:"",quantity:"1",notes:""}]);
  const [quotationRfqId, setQuotationRfqId] = useState("");
  const [quotationForm, setQuotationForm] = useState({supplier:"",quotation_number:"",quotation_date:new Date().toISOString().slice(0,10),valid_until:new Date(Date.now()+30*86400000).toISOString().slice(0,10),delivery_days:"0",payment_terms:"",notes:""});
  const [quotationPrices, setQuotationPrices] = useState<Record<number,{unit_price:string;discount_percent:string;tax_rate:string}>>({});
  const [convertingRFQId, setConvertingRFQId] = useState<number | null>(null);
  const [awardOrderForm, setAwardOrderForm] = useState({warehouse:"",order_number:"",order_date:new Date().toISOString().slice(0,10),expected_date:""});
  const [receipts, setReceipts] = useState<PurchaseReceipt[]>([]);
  const [invoices, setInvoices] = useState<PurchaseInvoice[]>([]);
  const [payments, setPayments] = useState<SupplierPayment[]>([]);
  const [returns, setReturns] = useState<SupplierReturn[]>([]);
  const [refunds, setRefunds] = useState<SupplierRefund[]>([]);
  const [landedCosts, setLandedCosts] = useState<LandedCostDocument[]>([]);
  const [landedCostReport, setLandedCostReport] = useState<LandedCostReport | null>(null);
  const [landedCostForm, setLandedCostForm] = useState({source_invoice:"",document_number:"",document_date:new Date().toISOString().slice(0,10),notes:""});
  const [landedCostLines, setLandedCostLines] = useState<{source_invoice_line:string;receipt_line:string;receipt_lines:string[];amount:string;basis:"manual"|"quantity"|"value"|"weight"|"volume"}[]>([{source_invoice_line:"",receipt_line:"",receipt_lines:[],amount:"",basis:"manual"}]);
  const [landedCostReportFrom, setLandedCostReportFrom] = useState("");
  const [landedCostReportTo, setLandedCostReportTo] = useState("");
  const [purchaseMatchingReport, setPurchaseMatchingReport] = useState<PurchaseMatchingReport | null>(null);
  const [matchingReportFrom, setMatchingReportFrom] = useState("");
  const [matchingReportTo, setMatchingReportTo] = useState("");
  const [purchaseAnalysisReport, setPurchaseAnalysisReport] = useState<PurchaseAnalysisReport | null>(null);
  const [purchaseAnalysisFrom, setPurchaseAnalysisFrom] = useState("");
  const [purchaseAnalysisTo, setPurchaseAnalysisTo] = useState("");
  const [refundForm, setRefundForm] = useState({ return_document: "", refund_number: "", refund_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", payment_account: "", amount: "", reference: "", notes: "" });
  const [returnForm, setReturnForm] = useState({ invoice: "", return_number: "", return_date: new Date().toISOString().slice(0, 10), notes: "" });
  const [returnLines, setReturnLines] = useState<{ invoice_line: string; quantity: string }[]>([{ invoice_line: "", quantity: "1" }]);
  const [statementSupplier, setStatementSupplier] = useState("");
  const [statementFrom, setStatementFrom] = useState("");
  const [statementTo, setStatementTo] = useState("");
  const [statement, setStatement] = useState<SupplierStatement | null>(null);
  const [agingAsOf, setAgingAsOf] = useState(new Date().toISOString().slice(0, 10));
  const [agingView, setAgingView] = useState<"supplier" | "grni" | "landed-cost" | "matching" | "analysis">("supplier");
  const [grniReport, setGrniReport] = useState<GRNIAgingReport | null>(null);
  const [aging, setAging] = useState<SupplierAging | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const [receiptForm, setReceiptForm] = useState({ supplier: "", warehouse: "", purchase_order: "", receipt_number: "", receipt_date: new Date().toISOString().slice(0, 10), supplier_delivery_ref: "", notes: "" });
  const [receiptLines, setReceiptLines] = useState<ReceiptDraftLine[]>([{ product: "", quantity: "1", unit_cost: "0", expiry_date: "" }]);
  const [orderForm, setOrderForm] = useState({ supplier: "", warehouse: "", order_number: "", order_date: new Date().toISOString().slice(0, 10), expected_date: "", notes: "" });
  const [orderLines, setOrderLines] = useState<{ product: string; quantity: string; unit_price: string; tax_rate: string }[]>([{ product: "", quantity: "1", unit_price: "0", tax_rate: "0" }]);
  const [requisitionForm, setRequisitionForm] = useState({ request_number: "", request_date: new Date().toISOString().slice(0, 10), required_date: "", department: "", purpose: "" });
  const [requisitionLines, setRequisitionLines] = useState<{ product: string; requested_quantity: string; estimated_unit_price: string }[]>([{ product: "", requested_quantity: "1", estimated_unit_price: "" }]);
  const [editingReqId, setEditingReqId] = useState<number | null>(null);
  const [convertingReqId, setConvertingReqId] = useState<number | null>(null);
  const [conversionForm, setConversionForm] = useState({ supplier: "", warehouse: "", order_number: "", order_date: new Date().toISOString().slice(0, 10) });
  const [invoiceForm, setInvoiceForm] = useState({ invoice_type: "inventory" as "inventory" | "direct_expense", receipt: "", supplier: "", service_acceptance: "", branch: "", activity: "", invoice_number: "", invoice_date: new Date().toISOString().slice(0, 10), tax_rate: "0", tax_code: "", transaction_currency: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), notes: "" });
  const [invoiceQuantities, setInvoiceQuantities] = useState<Record<number, string>>({});
  const [directInvoiceLines, setDirectInvoiceLines] = useState<{description:string;expense_account:string;cost_center:string;quantity:string;unit_price:string}[]>([{description:"",expense_account:"",cost_center:"",quantity:"1",unit_price:"0"}]);
  const [paymentForm, setPaymentForm] = useState({ invoice: "", payment_number: "", payment_date: new Date().toISOString().slice(0, 10), method: "bank" as "cash" | "bank", payment_account: "", amount: "", transaction_currency: "", transaction_amount: "", exchange_rate: "", exchange_rate_date: new Date().toISOString().slice(0, 10), reference: "" });
  const [fxInvoice, setFxInvoice] = useState<PurchaseInvoice | null>(null);
  const [fxHistory, setFxHistory] = useState<FxSubledgerRevaluation[]>([]);
  const [fxForm, setFxForm] = useState({revaluation_date:new Date().toISOString().slice(0,10),rate_date:new Date().toISOString().slice(0,10),rate:""});
  const [printDocument,setPrintDocument] = useState<PurchasePrintDocument|null>(null);
  const [showPurchaseTemplates,setShowPurchaseTemplates] = useState(false);
  const openPrint=(documentType:PurchasePrintDocument["documentType"],title:string,number:string,date:string,party:string,lines:PurchasePrintDocument["lines"],summary:PurchasePrintDocument["summary"],notes="",related="")=>setPrintDocument({documentType,title,number,date,party,lines,summary,notes,related});

  // تحميل بيانات الموردين والأصناف والمخازن ومستندات الشراء للشركة.
const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [s, p, w, o, serviceAcceptanceRows, r, i, pay, ret, ref, t, ta, cur, fx, rq, rfqData, quoteData, accountRows, costCenterRows, branchRows, landedCostRows, policyRows] = await Promise.all([
        api.suppliers(companyId), inventoryApi.products(companyId), inventoryApi.warehouses(companyId),
        purchasingApi.orders(companyId), purchasingApi.serviceAcceptances(companyId), purchasingApi.receipts(companyId), purchasingApi.invoices(companyId), purchasingApi.payments(companyId), purchasingApi.returns(companyId), purchasingApi.refunds(companyId), api.taxCodes(companyId), treasuryApi.accounts(companyId), api.currencies(), api.exchangeRates(companyId), purchasingApi.requisitions(companyId), purchasingApi.rfqs(companyId), purchasingApi.quotations(companyId),
        api.accounts(companyId), api.allCostCenters(companyId), api.branches(companyId), purchasingApi.landedCosts(companyId), purchasingApi.branchPurchasePolicies(companyId),
      ]);
      setSuppliers(s.filter((x) => x.is_active)); setProducts(p.filter((x) => x.is_active));
      setAccounts(accountRows); setCostCenters(costCenterRows.filter(x=>x.is_active)); setBranches(branchRows.filter(x=>x.is_active));
      setBranchPurchasePolicies(policyRows); setWarehouses(w.filter((x) => x.is_active)); setOrders(o); setServiceAcceptances(serviceAcceptanceRows); setRequisitions(rq); setRfqs(rfqData); setQuotations(quoteData); setReceipts(r); setInvoices(i); setPayments(pay); setReturns(ret); setRefunds(ref); setLandedCosts(landedCostRows); setTaxCodes(t.filter(x => x.is_active && x.category !== "reverse_charge")); setTreasuryAccounts(ta.filter(x => x.is_active));
      setOrderForm((old) => ({ ...old, supplier: old.supplier || String(s.find((x) => x.is_active)?.id || ""), warehouse: old.warehouse || String(w.find((x) => x.is_active)?.id || "") }));
      setOrderLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? String(p.find((x) => x.is_active && x.product_type === "goods")?.id || "") : "") })));
      setReceiptForm((old) => ({ ...old, supplier: old.supplier || String(s.find((x) => x.is_active)?.id || ""), warehouse: old.warehouse || String(w.find((x) => x.is_active)?.id || "") }));
      setPaymentForm((old) => ({ ...old, payment_account: String(ta.find(x => x.is_active && x.kind === old.method)?.account || "") }));
      const primaryAssignment = activityAssignments?.find((item) => item.is_active && item.activity === primaryActivity)
        || activityAssignments?.find((item) => item.is_active);
      setInvoiceForm((old) => ({
        ...old,
        supplier: s.some(x=>x.id===Number(old.supplier) && x.is_active) ? old.supplier : String(s.find(x=>x.is_active)?.id || ""),
        branch: branchRows.some(x=>x.id===Number(old.branch) && x.is_active) ? old.branch : String(branchRows.find(x=>x.is_active)?.id || ""),
        activity: activityAssignments?.some((item) => item.is_active && String(item.id) === old.activity)
          ? old.activity : (primaryAssignment ? String(primaryAssignment.id) : ""),
      }));
      setDirectInvoiceLines((old) => old.map(line=>({...line,expense_account:accountRows.some(x=>x.id===Number(line.expense_account) && x.is_active && !x.is_group && x.allow_posting && x.nature==="expense") ? line.expense_account : String(accountRows.find(x=>x.is_active && !x.is_group && x.allow_posting && x.nature==="expense")?.id || "")})));
      setReceiptLines((old) => old.map((line, index) => ({ ...line, product: line.product || (index === 0 ? String(p.find((x) => x.is_active && x.product_type === "goods")?.id || "") : "") })));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل بيانات المشتريات."); }
    finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);
  useEffect(() => {
    const preferred = activityAssignments?.find((item) => item.is_active && item.activity === primaryActivity)
      || activityAssignments?.find((item) => item.is_active);
    setInvoiceForm((old) => ({
      ...old,
      activity: activityAssignments?.some((item) => item.is_active && String(item.id) === old.activity)
        ? old.activity : (preferred ? String(preferred.id) : ""),
    }));
  }, [companyId, activityAssignments, primaryActivity]);

  const saveBranchPurchasePolicy = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !policyForm.branch) { setError("حدد الفرع أولاً."); return; }
    setBusy(true); setError(""); setNotice("");
    const payload = {
      branch: Number(policyForm.branch),
      purchase_enabled: policyForm.purchase_enabled,
      restrict_suppliers: policyForm.restrict_suppliers,
      allowed_supplier_ids: policyForm.allowed_supplier_ids.map(Number),
    };
    try {
      const existing = branchPurchasePolicies.find(row => row.branch === Number(policyForm.branch));
      if (existing) await purchasingApi.updateBranchPurchasePolicy(existing.id, payload);
      else await purchasingApi.createBranchPurchasePolicy(payload);
      setNotice("تم حفظ سياسة الشراء للفرع وتسجيل التغيير في سجل التدقيق.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ سياسة شراء الفرع.");
    } finally { setBusy(false); }
  };
  const editBranchPurchasePolicy = (row: BranchPurchasePolicy) => {
    setPolicyForm({
      branch: String(row.branch), purchase_enabled: row.purchase_enabled,
      restrict_suppliers: row.restrict_suppliers,
      allowed_supplier_ids: row.allowed_supplier_ids.map(String),
    });
    setTab("purchase-policies"); setShowForm(false);
  };

  const receiptLineQuantityForStatus = (receiptId: number, receiptLineId: number, status: "draft" | "posted") => invoices
    .filter((invoice) => invoice.receipt === receiptId && invoice.status === status)
    .reduce((total, invoice) => total + invoice.invoice_lines
      .filter((line) => line.receipt_line === receiptLineId)
      .reduce((lineTotal, line) => lineTotal + Number(line.quantity), 0), 0);
  const remainingReceiptLineQuantity = (receipt: PurchaseReceipt, line: PurchaseReceipt["line_items"][number]) =>
    Math.max(Number(line.quantity)
      - receiptLineQuantityForStatus(receipt.id, line.id, "posted")
      - receiptLineQuantityForStatus(receipt.id, line.id, "draft"), 0);
  const paymentableInvoices = useMemo(() => invoices.filter((invoice) => invoice.status === "posted" && Number(invoice.balance_due) > 0), [invoices]);
  const expenseAccounts = accounts.filter(account=>account.is_active && !account.is_group && account.allow_posting && account.nature==="expense");
  const invoiceableReceipts = receipts.filter((receipt) =>
    receipt.movement_status === "posted" && receipt.line_items.some((line) => remainingReceiptLineQuantity(receipt, line) > 0)
  );
  const invoiceServiceAcceptances = serviceAcceptances.filter((acceptance) =>
    acceptance.status === "accepted"
    && acceptance.supplier === Number(invoiceForm.supplier)
    && (!acceptance.branch || !invoiceForm.branch || acceptance.branch === Number(invoiceForm.branch))
    && !invoices.some((invoice) => invoice.service_acceptance === acceptance.id)
  );
  const filteredReceipts = receipts.filter((row) => [row.receipt_number, row.supplier_name, row.warehouse_name, row.supplier_delivery_ref].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredInvoices = invoices.filter((row) => [row.invoice_number, row.supplier_name, row.receipt_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredPayments = payments.filter((row) => [row.payment_number, row.supplier_name, row.invoice_number, row.reference].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredReturns = returns.filter((row) => [row.return_number, row.supplier_name, row.invoice_number].join(" ").toLowerCase().includes(search.toLowerCase()));
  const filteredRefunds = refunds.filter((row) => [row.refund_number, row.supplier_name, row.return_number, row.reference].join(" ").toLowerCase().includes(search.toLowerCase()));
  const refundableReturns = returns.filter((row) => row.status === "posted");
  const returnableInvoices = invoices.filter((invoice) => invoice.invoice_type === "inventory" && invoice.status === "posted" && Number(invoice.balance_due) > 0 && (!invoice.transaction_currency || !payments.some((payment) => payment.invoice === invoice.id && payment.status === "posted")));

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

const loadGrni = async () => {
    if(!companyId)return;
    setBusy(true);setError("");
    try{setGrniReport(await purchasingApi.grniAging(companyId,agingAsOf));}
    catch(e){setError(e instanceof Error?e.message:"تعذر تحميل تقرير GRNI.");}
    finally{setBusy(false);}
  };

const loadAging = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try { setAging(await purchasingApi.supplierAging(companyId, agingAsOf)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل أعمار مديونية الموردين."); }
    finally { setBusy(false); }
  };

  const eligibleLandedCostInvoices = invoices.filter(row =>
    row.invoice_type === "direct_expense" && row.status === "posted" && row.invoice_lines.some(line => !!line.expense_account),
  );
  const selectedLandedCostInvoice = eligibleLandedCostInvoices.find(row => row.id === Number(landedCostForm.source_invoice)) || null;
  const landedCostPreviews = landedCostLines.map((line) => {
    const amount = decimalToUnits(line.amount);
    const selectedIds = line.basis === "manual" ? (line.receipt_line ? [line.receipt_line] : []) : line.receipt_lines;
    const selected = selectedIds.map((id) => {
      for (const receipt of receipts) {
        const item = receipt.line_items.find((candidate) => String(candidate.id) === id);
        if (item) return { receipt, item };
      }
      return null;
    }).filter((entry): entry is { receipt: PurchaseReceipt; item: PurchaseReceipt["line_items"][number] } => entry !== null);
    if (amount === null || amount <= 0n || selected.length !== selectedIds.length || selected.length === 0) {
      return { valid: false, total: "0.0000", rows: [] as { id: number; label: string; share: string }[] };
    }
    if (line.basis === "manual") {
      const entry = selected[0];
      return {
        valid: entry.receipt.movement_status === "posted",
        total: unitsToDecimal(amount),
        rows: [{ id: entry.item.id, label: `${entry.receipt.receipt_number} · ${entry.item.product_name}`, share: unitsToDecimal(amount) }],
      };
    }
    const weights = selected.map(({ item }) => decimalToUnits(
      line.basis === "quantity" ? item.quantity
        : line.basis === "value" ? item.cost_amount
          : line.basis === "weight" ? (Number(item.product_unit_weight) * Number(item.quantity)).toFixed(4)
            : (Number(item.product_unit_volume) * Number(item.quantity)).toFixed(4),
    ));
    if (weights.some((weight) => weight === null || weight <= 0n) || selected.some(({ receipt }) => receipt.movement_status !== "posted")) {
      return { valid: false, total: unitsToDecimal(amount), rows: [] as { id: number; label: string; share: string }[] };
    }
    const safeWeights = weights as bigint[];
    const totalWeight = safeWeights.reduce((sum, weight) => sum + weight, 0n);
    const shares = safeWeights.map((weight) => roundedShareUnits(amount, weight, totalWeight));
    const residual = amount - shares.reduce((sum, share) => sum + share, 0n);
    if (residual !== 0n) {
      let largest = 0;
      for (let index = 1; index < safeWeights.length; index += 1) {
        if (safeWeights[index] > safeWeights[largest]) largest = index;
      }
      shares[largest] += residual;
    }
    return {
      valid: shares.some((share) => share > 0n),
      total: unitsToDecimal(shares.reduce((sum, share) => sum + share, 0n)),
      rows: selected.map(({ receipt, item }, index) => ({
        id: item.id,
        label: `${receipt.receipt_number} · ${item.product_name}`,
        share: unitsToDecimal(shares[index]),
      })).filter((row) => row.share !== "0.0000"),
    };
  });

  const saveLandedCost = async (event:FormEvent) => {
    event.preventDefault();
    if (!companyId || !selectedLandedCostInvoice || !landedCostForm.document_number.trim()
      || !landedCostLines.length || landedCostLines.some(line =>
        !line.source_invoice_line || Number(line.amount) <= 0
        || (line.basis === "manual" ? !line.receipt_line : line.receipt_lines.length < 1))) {
      setError("حدد فاتورة مصروف مرحلة وبند المصروف وطريقة التوزيع وبنود الاستلام ومبلغًا صحيحًا."); return;
    }
    setBusy(true); setError("");
    try {
      await purchasingApi.createLandedCost({
        company:companyId, source_invoice:selectedLandedCostInvoice.id,
        document_number:landedCostForm.document_number.trim(),
        document_date:landedCostForm.document_date, notes:landedCostForm.notes,
        allocations:landedCostLines.map(line => ({
          source_invoice_line:Number(line.source_invoice_line),
          ...(line.basis === "manual"
            ? {receipt_line:Number(line.receipt_line)}
            : {receipt_lines:line.receipt_lines.map(Number)}),
          amount:line.amount, basis:line.basis,
        })),
      });
      setShowForm(false);
      setLandedCostForm({source_invoice:"",document_number:"",document_date:new Date().toISOString().slice(0,10),notes:""});
      setLandedCostLines([{source_invoice_line:"",receipt_line:"",receipt_lines:[],amount:"",basis:"manual"}]);
      setNotice("تم حفظ مستند رسملة التكلفة كمسودة. لم يؤثر على الحسابات أو المخزون قبل الترحيل.");
      await refresh();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر حفظ مستند الرسملة."); }
    finally { setBusy(false); }
  };
  const postLandedCost = async (id:number) => {
    setBusy(true);setError("");setNotice("");
    try { await purchasingApi.postLandedCost(id);setNotice("تم ترحيل رسملة التكلفة وتحديث المخزون والقيود.");await refresh(); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر ترحيل رسملة التكلفة."); }
    finally { setBusy(false); }
  };
  const reverseLandedCost = async (id:number) => {
    const entryDate = window.prompt("أدخل تاريخ عكس الرسملة (YYYY-MM-DD):", new Date().toISOString().slice(0,10));
    if(!entryDate)return;
    setBusy(true);setError("");setNotice("");
    try { await purchasingApi.reverseLandedCost(id,entryDate);setNotice("تم عكس الرسملة والقيود المرتبطة بها.");await refresh(); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر عكس الرسملة."); }
    finally { setBusy(false); }
  };
  const loadLandedCostReport = async () => {
    if(!companyId)return;
    if(landedCostReportFrom && landedCostReportTo && landedCostReportFrom>landedCostReportTo){setError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.");return;}
    setBusy(true);setError("");
    try { setLandedCostReport(await purchasingApi.landedCostReport(companyId,{date_from:landedCostReportFrom||undefined,date_to:landedCostReportTo||undefined})); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحميل تقرير رسملة التكلفة."); }
    finally { setBusy(false); }
  };
  const loadPurchaseMatchingReport = async () => {
    if(!companyId)return;
    if(matchingReportFrom && matchingReportTo && matchingReportFrom>matchingReportTo){setError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.");return;}
    setBusy(true);setError("");
    try { setPurchaseMatchingReport(await purchasingApi.purchaseMatchingReport(companyId,{date_from:matchingReportFrom||undefined,date_to:matchingReportTo||undefined})); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحميل تقرير مطابقة أوامر الشراء."); }
    finally { setBusy(false); }
  };
  const loadPurchaseAnalysisReport = async () => {
    if(!companyId)return;
    if(purchaseAnalysisFrom && purchaseAnalysisTo && purchaseAnalysisFrom>purchaseAnalysisTo){setError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.");return;}
    setBusy(true);setError("");
    try { setPurchaseAnalysisReport(await purchasingApi.purchaseAnalysisReport(companyId,{date_from:purchaseAnalysisFrom||undefined,date_to:purchaseAnalysisTo||undefined})); }
    catch(reason) { setError(reason instanceof Error?reason.message:"تعذر تحميل تحليل المشتريات."); }
    finally { setBusy(false); }
  };



  const saveRFQ = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !rfqForm.rfq_number.trim() || !rfqSuppliers.length || !rfqLines.length || rfqLines.some(x=>!x.product || Number(x.quantity)<=0)) { setError("أدخل رقم الطلب وموردًا واحدًا على الأقل وبنودًا صحيحة."); return; }
    setBusy(true); setError("");
    try {
      await purchasingApi.createRFQ({company:companyId,rfq_number:rfqForm.rfq_number.trim(),issue_date:rfqForm.issue_date,closing_date:rfqForm.closing_date,terms:rfqForm.terms,invited_suppliers:rfqSuppliers.map(Number),lines:rfqLines.map(x=>({product:Number(x.product),quantity:x.quantity,notes:x.notes}))});
      setShowForm(false); setRfqForm(x=>({...x,rfq_number:"",terms:""})); setRfqSuppliers([]); await refresh();
    } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء طلب العروض."); } finally { setBusy(false); }
  };
  const openRFQ = async (id:number) => { setBusy(true);setError("");try{await purchasingApi.openRFQ(id);await refresh();}catch(e){setError(e instanceof Error?e.message:"تعذر فتح الطلب.");}finally{setBusy(false);} };
  const closeRFQ = async (id:number) => { setBusy(true);setError("");try{await purchasingApi.closeRFQ(id);await refresh();}catch(e){setError(e instanceof Error?e.message:"تعذر إغلاق الطلب.");}finally{setBusy(false);} };
  const selectedRFQ = rfqs.find(x=>x.id===Number(quotationRfqId)) || null;
  const saveQuotation = async (event:FormEvent) => {
    event.preventDefault();
    if(!selectedRFQ || !quotationForm.supplier || !quotationForm.quotation_number.trim() || selectedRFQ.line_items.some(x=>!quotationPrices[x.id]?.unit_price)){setError("حدد طلبًا وموردًا وأدخل سعر كل بند.");return;}
    setBusy(true);setError("");
    try {
      await purchasingApi.createQuotation({rfq:selectedRFQ.id,supplier:Number(quotationForm.supplier),quotation_number:quotationForm.quotation_number.trim(),quotation_date:quotationForm.quotation_date,valid_until:quotationForm.valid_until,delivery_days:Number(quotationForm.delivery_days),payment_terms:quotationForm.payment_terms,notes:quotationForm.notes,lines:selectedRFQ.line_items.map(x=>({rfq_line:x.id,unit_price:quotationPrices[x.id].unit_price,discount_percent:quotationPrices[x.id].discount_percent||"0",tax_rate:quotationPrices[x.id].tax_rate||"0"}))});
      setShowForm(false);setQuotationForm(x=>({...x,quotation_number:"",notes:""}));await refresh();
    }catch(e){setError(e instanceof Error?e.message:"تعذر تسجيل العرض.");}finally{setBusy(false);}
  };

  const beginAwardConversion = (rfq:PurchaseRFQ) => {
    setConvertingRFQId(rfq.id);
    setAwardOrderForm({warehouse:String(warehouses[0]?.id||""),order_number:"PO-"+rfq.rfq_number,order_date:new Date().toISOString().slice(0,10),expected_date:""});
    setShowForm(false);
  };
  const submitAwardConversion = async (event:FormEvent) => {
    event.preventDefault();
    if(convertingRFQId===null || !awardOrderForm.warehouse || !awardOrderForm.order_number.trim()){setError("حدد مخزن الاستلام ورقم أمر الشراء.");return;}
    setBusy(true);setError("");
    try{
      await purchasingApi.convertAward(convertingRFQId,{warehouse:Number(awardOrderForm.warehouse),order_number:awardOrderForm.order_number.trim(),order_date:awardOrderForm.order_date,expected_date:awardOrderForm.expected_date||null});
      setConvertingRFQId(null);setTab("orders");await refresh();
    }catch(e){setError(e instanceof Error?e.message:"تعذر إنشاء أمر الشراء من العرض الفائز.");}
    finally{setBusy(false);}
  };

  const awardQuotation = async (rfqId:number, quotationId:number) => {
    const reason=window.prompt("اكتب مبرر الترسية مع مراعاة السعر والتوريد وشروط السداد:");
    if(!reason?.trim())return;
    setBusy(true);setError("");
    try{await purchasingApi.awardRFQ(rfqId,quotationId,reason.trim());await refresh();}
    catch(e){setError(e instanceof Error?e.message:"تعذرت الترسية.");}finally{setBusy(false);}
  };

  const createServiceAcceptanceForOrder = async (row: PurchaseOrder) => {
    if (!companyId) return;
    const serviceLines = row.line_items.filter(line => line.product_type === "service");
    const acceptedByLine = new Map<number, number>();
    serviceAcceptances.filter(doc => doc.order === row.id && doc.status === "accepted").forEach(doc => {
      doc.line_items.forEach(line => acceptedByLine.set(line.order_line, (acceptedByLine.get(line.order_line) || 0) + Number(line.quantity)));
    });
    const lines = serviceLines.map(line => ({ order_line: line.id, quantity: String(Math.max(0, Number(line.quantity) - (acceptedByLine.get(line.id) || 0))) })).filter(line => Number(line.quantity) > 0);
    if (!lines.length) { setError("تم اعتماد كامل كميات الخدمات في هذا الأمر بالفعل."); return; }
    const acceptanceNumber = window.prompt("رقم محضر استلام/إنجاز الخدمة:");
    if (!acceptanceNumber?.trim()) return;
    setBusy(true); setError("");
    try {
      await purchasingApi.createServiceAcceptance({
        company: companyId, order: row.id, supplier: row.supplier,
        acceptance_number: acceptanceNumber.trim(), acceptance_date: new Date().toISOString().slice(0, 10),
        notes: "محضر إنجاز خدمة مرتبط بأمر الشراء " + row.order_number, lines,
      });
      setTab("service-acceptances"); setShowForm(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء محضر الخدمة."); }
    finally { setBusy(false); }
  };

  const acceptServiceAcceptance = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.acceptServiceAcceptance(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر اعتماد محضر الخدمة."); }
    finally { setBusy(false); }
  };

  const saveRequisition = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !requisitionForm.request_number.trim() || !requisitionLines.length || requisitionLines.some(line => !line.product || Number(line.requested_quantity) <= 0)) {
      setError("أكمل رقم الطلب والأصناف والكميات المطلوبة."); return;
    }
    setBusy(true); setError("");
    try {
      const payload = {
        company: companyId, request_number: requisitionForm.request_number.trim(),
        request_date: requisitionForm.request_date, required_date: requisitionForm.required_date || null,
        department: requisitionForm.department, purpose: requisitionForm.purpose,
        lines: requisitionLines.map(line => ({ product: Number(line.product), requested_quantity: line.requested_quantity, estimated_unit_price: line.estimated_unit_price || null })),
      };
      if (editingReqId) await purchasingApi.updateRequisition(editingReqId, payload);
      else await purchasingApi.createRequisition(payload);
      setNotice(editingReqId ? "تم حفظ التعديلات وإعادة الطلب إلى مسودة." : "تم حفظ طلب الاحتياج كمسودة.");
      setEditingReqId(null);
      setShowForm(false);
      setRequisitionForm(old => ({ ...old, request_number: "", department: "", purpose: "", required_date: "" }));
      setRequisitionLines([{ product: String(products[0]?.id || ""), requested_quantity: "1", estimated_unit_price: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ طلب الاحتياج."); }
    finally { setBusy(false); }
  };
  const openRequisitionEdit = (row: PurchaseRequisition) => {
    setEditingReqId(row.id);
    setRequisitionForm({
      request_number: row.request_number, request_date: row.request_date,
      required_date: row.required_date || "", department: row.department || "", purpose: row.purpose || "",
    });
    setRequisitionLines(row.line_items.map(line => ({
      product: String(line.product), requested_quantity: line.requested_quantity,
      estimated_unit_price: line.estimated_unit_price || "",
    })));
    setTab("requisitions"); setShowForm(true); setError(""); setNotice("");
  };
  const cancelRequisition = async (id: number) => {
    if (!window.confirm("هل تريد إلغاء طلب الاحتياج؟ لن يمكن إرساله للاعتماد بعد الإلغاء.")) return;
    setBusy(true); setError("");
    try { await purchasingApi.cancelRequisition(id); setNotice("تم إلغاء طلب الاحتياج."); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إلغاء طلب الاحتياج."); }
    finally { setBusy(false); }
  };
  const submitRequisition = async (id: number) => {
    setBusy(true); setError("");
    try { await purchasingApi.submitRequisition(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إرسال طلب الاحتياج."); }
    finally { setBusy(false); }
  };
  const reviewRequisition = async (id: number, decision: "approve" | "reject") => {
    const note = window.prompt(decision === "approve" ? "ملاحظة الاعتماد (اختياري)" : "سبب الرفض (مطلوب)") || "";
    if (decision === "reject" && !note.trim()) return;
    setBusy(true); setError("");
    try { await purchasingApi.reviewRequisition(id, decision, note); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذرت مراجعة طلب الاحتياج."); }
    finally { setBusy(false); }
  };
  const openRequisitionConversion = (row: PurchaseRequisition) => {
    setConvertingReqId(row.id);
    setConversionForm({
      supplier: String(suppliers[0]?.id || ""),
      warehouse: String(warehouses[0]?.id || ""),
      order_number: `PO-${row.request_number}`,
      order_date: new Date().toISOString().slice(0, 10),
    });
    setError("");
  };
  const convertRequisition = async (event: FormEvent) => {
    event.preventDefault();
    const row = requisitions.find(item => item.id === convertingReqId);
    if (!row || !conversionForm.supplier || !conversionForm.warehouse || !conversionForm.order_number.trim()) {
      setError("حدد المورد والمخزن ورقم أمر الشراء."); return;
    }
    setBusy(true); setError("");
    try {
      await purchasingApi.convertRequisition(row.id, {
        supplier: Number(conversionForm.supplier), warehouse: Number(conversionForm.warehouse),
        order_number: conversionForm.order_number.trim(), order_date: conversionForm.order_date,
        expected_date: row.required_date,
      });
      setConvertingReqId(null); setTab("orders"); setShowForm(false); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحويل الطلب إلى أمر شراء."); }
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
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await purchasingApi.confirmOrder(id);
      if ("status" in result && result.status === "pending_approval") setNotice(result.detail);
      else setNotice("تم تأكيد أمر الشراء.");
      await refresh();
    }
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
      setShowForm(false); setReceiptForm((old) => ({ ...old, purchase_order: "", receipt_number: "", supplier_delivery_ref: "", notes: "" }));
      setReceiptLines([{ product: String(products[0]?.id || ""), quantity: "1", unit_cost: "0", expiry_date: "" }]);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء إذن الاستلام."); }
    finally { setBusy(false); }
  };

  // حفظ فاتورة المورد وربطها بالاستلام والضرائب والمبالغ المستحقة.
const saveInvoice = async (event: React.FormEvent) => {
    event.preventDefault(); if (!companyId) return;
    if (invoiceForm.invoice_type === "direct_expense") {
      if (!invoiceForm.supplier || !invoiceForm.branch) { setError("اختر المورد والفرع لفاتورة الخدمات والمصروفات."); return; }
      if (!directInvoiceLines.length || directInvoiceLines.some(line=>!line.description.trim() || !line.expense_account || Number(line.quantity)<=0 || Number(line.unit_price)<0)) {
        setError("أكمل وصف وحساب وكمية وقيمة كل بند مباشر."); return;
      }
      setBusy(true); setError("");
      try {
        await purchasingApi.createInvoice({
          company:companyId, supplier:Number(invoiceForm.supplier), invoice_type:"direct_expense",
          service_acceptance:invoiceForm.service_acceptance ? Number(invoiceForm.service_acceptance) : null,
          branch:Number(invoiceForm.branch),
          activity:invoiceForm.activity ? Number(invoiceForm.activity) : null,
          invoice_number:invoiceForm.invoice_number,
          invoice_date:invoiceForm.invoice_date, notes:invoiceForm.notes,
          transaction_currency:invoiceForm.transaction_currency ? Number(invoiceForm.transaction_currency) : null,
          exchange_rate:invoiceForm.transaction_currency ? invoiceForm.exchange_rate : null,
          exchange_rate_date:invoiceForm.transaction_currency ? invoiceForm.exchange_rate_date : null,
          lines:directInvoiceLines.map(line=>({
            description:line.description.trim(), expense_account:Number(line.expense_account),
            cost_center:line.cost_center ? Number(line.cost_center) : null,
            billed_quantity:line.quantity,
            ...(invoiceForm.transaction_currency ? {transaction_unit_price:line.unit_price} : {unit_price:line.unit_price}),
            tax_rate:invoiceForm.tax_rate, tax_code:invoiceForm.tax_code ? Number(invoiceForm.tax_code) : null,
          })),
        });
        setShowForm(false);
        setInvoiceForm(old=>({...old,receipt:"",service_acceptance:"",invoice_number:"",notes:""}));
        setDirectInvoiceLines([{description:"",expense_account:String(expenseAccounts[0]?.id || ""),cost_center:"",quantity:"1",unit_price:"0"}]);
        await refresh();
      } catch(reason) { setError(reason instanceof Error?reason.message:"تعذر إنشاء فاتورة الخدمات والمصروفات."); }
      finally { setBusy(false); }
      return;
    }
    const receipt = receipts.find((row) => row.id === Number(invoiceForm.receipt));
    if (!receipt) { setError("اختر إذن استلام مرحّلًا."); return; }
    const lines = receipt.line_items.map((line) => {
      const remaining = remainingReceiptLineQuantity(receipt, line);
      const billedQuantity = invoiceQuantities[line.id] ?? remaining.toFixed(4);
      return { line, remaining, billedQuantity };
    }).filter((item) => Number(item.billedQuantity) > 0);
    if (!lines.length) { setError("أدخل كمية واحدة على الأقل للفوترة."); return; }
    if (lines.some((item) => Number(item.billedQuantity) > item.remaining)) {
      setError("إحدى الكميات المطلوبة أكبر من المتبقي غير المفوتر من إذن الاستلام."); return;
    }
    setBusy(true); setError("");
    try {
      await purchasingApi.createInvoice({
        company: companyId, supplier: receipt.supplier, invoice_type:"inventory", receipt: receipt.id,
        invoice_number: invoiceForm.invoice_number, invoice_date: invoiceForm.invoice_date, notes: invoiceForm.notes,
        transaction_currency: invoiceForm.transaction_currency ? Number(invoiceForm.transaction_currency) : null,
        exchange_rate: invoiceForm.transaction_currency ? invoiceForm.exchange_rate : null,
        exchange_rate_date: invoiceForm.transaction_currency ? invoiceForm.exchange_rate_date : null,
        lines: lines.map(({ line, billedQuantity }) => ({
          receipt_line: line.id, billed_quantity: billedQuantity,
          ...(invoiceForm.transaction_currency ? { transaction_unit_price: divideDecimalToFixed(line.unit_cost, invoiceForm.exchange_rate, 4) } : { unit_price: line.unit_cost }),
          tax_rate: invoiceForm.tax_rate, tax_code: invoiceForm.tax_code ? Number(invoiceForm.tax_code) : null,
        })),
      });
      setShowForm(false); setInvoiceForm((old) => ({ ...old, receipt: "", invoice_number: "", notes: "" })); setInvoiceQuantities({});
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
        ? multiplyDecimalToFixed(paymentForm.transaction_amount, paymentForm.exchange_rate, 4)
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

  const saveRefund = async (event: FormEvent) => {
    event.preventDefault();
    const returned = returns.find((row) => row.id === Number(refundForm.return_document));
    if (!companyId || !returned) { setError("اختر مرتجعًا مرحّلًا."); return; }
    if (!refundForm.refund_number.trim() || Number(refundForm.amount) <= 0 || !refundForm.payment_account) { setError("أكمل رقم الاسترداد والمبلغ وحساب الخزينة."); return; }
    setBusy(true); setError("");
    try {
      await purchasingApi.createRefund({ company: companyId, supplier: returned.supplier, return_document: returned.id, refund_number: refundForm.refund_number.trim(), refund_date: refundForm.refund_date, method: refundForm.method, payment_account: Number(refundForm.payment_account), amount: refundForm.amount, reference: refundForm.reference, notes: refundForm.notes });
      setShowForm(false); setRefundForm((old) => ({ ...old, return_document: "", refund_number: "", amount: "", reference: "", notes: "" })); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء استرداد المورد."); }
    finally { setBusy(false); }
  };

  const postRefund = async (id: number) => {
    if (!window.confirm("سيتم إثبات استلام مبلغ الاسترداد في الخزينة أو البنك وتخفيض الرصيد الدائن للمورد. هل تريد الترحيل؟")) return;
    setBusy(true); setError("");
    try { await purchasingApi.postRefund(id); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر ترحيل استرداد المورد."); }
    finally { setBusy(false); }
  };

  const reverseRefund = async (id:number) => {
    if(!window.confirm("سيتم عكس قيد استرداد المورد وإعادة الرصيد الدائن للمورد. هل تريد المتابعة؟")) return;
    setBusy(true);setError("");
    try { await purchasingApi.reverseRefund(id); await refresh(); }
    catch(reason){setError(reason instanceof Error?reason.message:"تعذر عكس استرداد المورد.");}
    finally{setBusy(false);}
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
      <button className="subtle-button" type="button" onClick={()=>setShowPurchaseTemplates(value=>!value)}>{showPurchaseTemplates?"إخفاء قوالب الطباعة":"قوالب طباعة المشتريات"}</button>
      <button className="primary-button" type="button" disabled={busy || tab === "statement" || tab === "aging" || tab === "purchase-policies" || tab === "service-acceptances" || (tab === "sourcing" && sourcingView === "quotations" && !rfqs.some(x=>x.status==="open")) || ((tab === "invoices" && invoiceableReceipts.length === 0 && !(suppliers.length>0 && branches.length>0 && expenseAccounts.length>0)) || (tab === "payments" && paymentableInvoices.length === 0) || (tab === "returns" && returnableInvoices.length === 0) || (tab === "refunds" && refundableReturns.length === 0))} onClick={() => { if (!showForm && tab === "requisitions") { setEditingReqId(null); setRequisitionForm({ request_number: "", request_date: new Date().toISOString().slice(0, 10), required_date: "", department: "", purpose: "" }); setRequisitionLines([{ product: String(products[0]?.id || ""), requested_quantity: "1", estimated_unit_price: "" }]); } setShowForm((v) => !v); }}>{showForm ? "إغلاق النموذج" : tab === "sourcing" ? (sourcingView === "rfqs" ? "＋ طلب عروض أسعار" : "＋ تسجيل عرض مورد") : tab === "requisitions" ? "＋ طلب احتياج جديد" : tab === "orders" ? "＋ أمر شراء جديد" : tab === "service-acceptances" ? "محاضر إنجاز الخدمات" : tab === "receipts" ? "＋ إذن استلام جديد" : tab === "invoices" ? "＋ فاتورة مورد جديدة" : tab === "payments" ? "＋ سند صرف جديد" : tab === "returns" ? "＋ مرتجع مورد جديد" : tab === "refunds" ? "＋ استرداد من مورد" : tab === "landed-cost" ? "＋ رسملة تكلفة شراء" : tab === "purchase-policies" ? "سياسات الشراء" : "كشف الحساب"}</button>
    </div>
    <div className="inventory-tabs"><button className={tab === "sourcing" ? "inventory-tab active" : "inventory-tab"} onClick={() => {setTab("sourcing");setSourcingView("rfqs");setShowForm(false);setSearch("");}}>عروض الموردين <b>{rfqs.length}</b></button><button className={tab === "requisitions" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("requisitions"); setShowForm(false); setSearch(""); }}>طلبات الاحتياج <b>{requisitions.length}</b></button><button className={tab === "orders" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("orders"); setShowForm(false); setSearch(""); }}>أوامر الشراء <b>{orders.length}</b></button><button className={tab === "service-acceptances" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("service-acceptances"); setShowForm(false); setSearch(""); }}>استلام الخدمات <b>{serviceAcceptances.length}</b></button><button className={tab === "receipts" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("receipts"); setShowForm(false); setSearch(""); }}>أذون الاستلام <b>{receipts.length}</b></button><button className={tab === "invoices" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("invoices"); setShowForm(false); setSearch(""); }}>فواتير الموردين <b>{invoices.length}</b></button><button className={tab === "payments" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("payments"); setShowForm(false); setSearch(""); }}>سندات الصرف <b>{payments.length}</b></button><button className={tab === "returns" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("returns"); setShowForm(false); setSearch(""); }}>مرتجعات الموردين <b>{returns.length}</b></button><button className={tab === "refunds" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("refunds"); setShowForm(false); setSearch(""); }}>استردادات الموردين <b>{refunds.length}</b></button><button className={tab === "landed-cost" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("landed-cost"); setShowForm(false); setSearch(""); }}>رسملة تكاليف الشراء <b>{landedCosts.length}</b></button><button className={tab === "statement" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("statement"); setShowForm(false); setStatementSupplier((old) => old || String(suppliers[0]?.id || "")); setStatement(null); }}>كشف حساب المورد</button><button className={tab === "aging" ? "inventory-tab active" : "inventory-tab"} onClick={() => { setTab("aging"); setShowForm(false); setAging(null); }}>تقارير المشتريات</button><button className={tab === "purchase-policies" ? "inventory-tab active" : "inventory-tab"} onClick={() => { const firstBranch = branches[0]; const existing = branchPurchasePolicies.find(row => row.branch === firstBranch?.id); setTab("purchase-policies"); setShowForm(false); setSearch(""); setPolicyForm(existing ? {branch:String(existing.branch),purchase_enabled:existing.purchase_enabled,restrict_suppliers:existing.restrict_suppliers,allowed_supplier_ids:existing.allowed_supplier_ids.map(String)} : {branch:String(firstBranch?.id || ""),purchase_enabled:true,restrict_suppliers:false,allowed_supplier_ids:[]}); }}>صلاحيات شراء الفروع</button></div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}

    {tab === "purchase-policies" && <div className="inventory-form">
      <div className="inventory-wide"><h3>سياسة الشراء لكل فرع</h3><p>يمكن إيقاف الشراء بالكامل أو قصر التعامل على موردين محددين. التغيير يسجل في سجل التدقيق ويطبق على أوامر الشراء والاستلام والفواتير.</p></div>
      <form className="inventory-form inventory-wide" onSubmit={e => void saveBranchPurchasePolicy(e)}>
        <label>الفرع<select required value={policyForm.branch} onChange={e => {
          const selected = branchPurchasePolicies.find(row => row.branch === Number(e.target.value));
          setPolicyForm(selected ? {
            branch:String(selected.branch), purchase_enabled:selected.purchase_enabled,
            restrict_suppliers:selected.restrict_suppliers,
            allowed_supplier_ids:selected.allowed_supplier_ids.map(String),
          } : {branch:e.target.value,purchase_enabled:true,restrict_suppliers:false,allowed_supplier_ids:[]});
        }}><option value="">اختر الفرع</option>{branches.map(row=><option key={row.id} value={row.id}>{row.code} - {row.name_ar}</option>)}</select></label>
        <label className="checkbox-label"><input type="checkbox" checked={policyForm.purchase_enabled} onChange={e=>setPolicyForm(old=>({...old,purchase_enabled:e.target.checked}))}/> السماح للفرع بالشراء</label>
        <label className="checkbox-label"><input type="checkbox" checked={policyForm.restrict_suppliers} onChange={e=>setPolicyForm(old=>({...old,restrict_suppliers:e.target.checked}))}/> تقييد الشراء بموردين محددين</label>
        {policyForm.restrict_suppliers && <div className="inventory-wide"><strong>الموردون المسموحون</strong><div className="inventory-line-editor">{suppliers.map(supplier=><label key={supplier.id} className="checkbox-label"><input type="checkbox" checked={policyForm.allowed_supplier_ids.includes(String(supplier.id))} onChange={e=>setPolicyForm(old=>({...old,allowed_supplier_ids:e.target.checked?[...old.allowed_supplier_ids,String(supplier.id)]:old.allowed_supplier_ids.filter(id=>id!==String(supplier.id))}))}/>{supplier.code} - {supplier.name_ar}</label>)}</div></div>}
        <div className="inventory-wide"><button className="primary-button" type="submit" disabled={busy || !policyForm.branch}>حفظ سياسة الفرع</button></div>
      </form>
      <div className="inventory-wide data-table-wrap"><table><thead><tr><th>الفرع</th><th>الشراء</th><th>تقييد الموردين</th><th>عدد الموردين المسموحين</th><th>آخر تحديث</th><th>إجراء</th></tr></thead><tbody>{branchPurchasePolicies.map(row=><tr key={row.id}><td>{row.branch_name}</td><td>{row.purchase_enabled?"مسموح":"موقوف"}</td><td>{row.restrict_suppliers?"مقيد":"غير مقيد"}</td><td>{row.allowed_supplier_ids.length}</td><td>{new Date(row.updated_at).toLocaleString("ar-EG")}</td><td><button className="subtle-button" type="button" onClick={()=>editBranchPurchasePolicy(row)}>تعديل</button></td></tr>)}</tbody></table>{branchPurchasePolicies.length===0&&<div className="table-empty">لم يتم إعداد سياسات شراء للفروع بعد؛ الفروع غير المسجلة تظل مسموحًا لها بالشراء دون تقييد موردين.</div>}</div>
    </div>}
    {showForm && tab === "landed-cost" && <form className="inventory-form" onSubmit={e=>void saveLandedCost(e)}>
      <div className="inventory-wide"><h3>مستند رسملة تكلفة شراء</h3><p>اربط فاتورة مصروف شحن أو جمارك أو تأمين بسطور استلام البضاعة. سيظل المستند مسودة حتى تنفذ الترحيل.</p></div>
      <label className="inventory-wide">فاتورة المصروف المصدر<select required value={landedCostForm.source_invoice} onChange={e=>{setLandedCostForm({...landedCostForm,source_invoice:e.target.value});setLandedCostLines([{source_invoice_line:"",receipt_line:"",receipt_lines:[],amount:"",basis:"manual"}]);}}><option value="">اختر فاتورة مصروف مرحلة</option>{eligibleLandedCostInvoices.map(row=><option key={row.id} value={row.id}>{row.invoice_number} · {row.supplier_name} · {money(row.grand_total)}</option>)}</select></label>
      <label>رقم مستند الرسملة<input required value={landedCostForm.document_number} onChange={e=>setLandedCostForm({...landedCostForm,document_number:e.target.value})}/></label>
      <label>تاريخ الرسملة<input required type="date" value={landedCostForm.document_date} onChange={e=>setLandedCostForm({...landedCostForm,document_date:e.target.value})}/></label>
      <label className="inventory-wide">ملاحظات<textarea value={landedCostForm.notes} onChange={e=>setLandedCostForm({...landedCostForm,notes:e.target.value})}/></label>
      <div className="inventory-wide"><div className="work-panel-head"><h3>توزيع المصروف على الاستلامات</h3><button className="subtle-button" type="button" onClick={()=>setLandedCostLines(old=>[...old,{source_invoice_line:"",receipt_line:"",receipt_lines:[],amount:"",basis:"manual"}])}>＋ إضافة توزيع</button></div></div>
      {landedCostLines.map((line,index)=><div className="inventory-form-row inventory-wide" key={index}>
        <label>بند المصروف<select required value={line.source_invoice_line} onChange={e=>setLandedCostLines(old=>old.map((x,i)=>i===index?{...x,source_invoice_line:e.target.value}:x))}><option value="">اختر بندًا</option>{selectedLandedCostInvoice?.invoice_lines.filter(x=>!!x.expense_account).map(x=><option key={x.id} value={x.id}>{x.description||x.expense_account_name||("بند "+x.id)} · {money(x.net_amount)}</option>)}</select></label>
        <label>طريقة التوزيع<select value={line.basis} onChange={e=>setLandedCostLines(old=>old.map((x,i)=>i===index?{...x,basis:e.target.value as "manual"|"value"|"quantity",receipt_line:"",receipt_lines:[]}:x))}><option value="manual">يدوي</option><option value="value">حسب قيمة الاستلام</option><option value="quantity">حسب الكمية المستلمة</option><option value="weight">حسب الوزن</option><option value="volume">حسب الحجم</option></select></label>
        {line.basis === "manual" ? <label>بند الاستلام<select required value={line.receipt_line} onChange={e=>setLandedCostLines(old=>old.map((x,i)=>i===index?{...x,receipt_line:e.target.value}:x))}><option value="">اختر استلامًا</option>{receipts.filter(x=>x.movement_status==="posted").flatMap(receipt=>receipt.line_items.map(item=><option key={item.id} value={item.id}>{receipt.receipt_number} · {item.product_code} · {item.product_name}</option>))}</select></label> : <label>بنود الاستلام<select required multiple value={line.receipt_lines} onChange={e=>setLandedCostLines(old=>old.map((x,i)=>i===index?{...x,receipt_lines:Array.from(e.target.selectedOptions).map(option=>option.value)}:x))}>{receipts.filter(x=>x.movement_status==="posted").flatMap(receipt=>receipt.line_items.map(item=><option key={item.id} value={item.id}>{receipt.receipt_number} · {item.product_code} · {item.product_name} · كمية {item.quantity} · قيمة {item.cost_amount}</option>))}</select></label>}
        <label>{line.basis === "manual" ? "مبلغ التحميل" : "إجمالي المبلغ المراد توزيعه"}<input required type="number" min="0.0001" step="0.0001" value={line.amount} onChange={e=>setLandedCostLines(old=>old.map((x,i)=>i===index?{...x,amount:e.target.value}:x))}/></label>
        <button className="subtle-button" type="button" disabled={landedCostLines.length===1} onClick={()=>setLandedCostLines(old=>old.filter((_,i)=>i!==index))}>حذف</button>
      </div>)}
      <section className="inventory-wide" aria-live="polite">
        <h3>معاينة التوزيع قبل الحفظ</h3>
        {landedCostPreviews.map((preview, index) => <div className="data-table-wrap" key={index}>
          <table><thead><tr><th>بند الاستلام</th><th>نصيب المصروف</th></tr></thead>
            <tbody>{preview.rows.map(row => <tr key={row.id}><td>{row.label}</td><td>{money(row.share)}</td></tr>)}
              {preview.rows.length === 0 && <tr><td colSpan={2}>أدخل مبلغًا واختر بنود استلام صالحة لعرض المعاينة.</td></tr>}
            </tbody>
            <tfoot><tr><th>إجمالي التوزيع المتوقع</th><th>{money(preview.total)}</th></tr></tfoot>
          </table>
          {!preview.valid && <p className="error-banner">المعاينة غير مكتملة: راجع بنود الاستلام والأوزان؛ يجب أن تكون الكمية أو القيمة موجبة وأن يكون الاستلام مرحّلًا.</p>}
        </div>)}
        <p>تُحسب الأنصبة حتى 4 منازل عشرية، ويُضاف فرق التقريب إلى البند صاحب أكبر وزن؛ يجب أن يساوي الإجمالي مبلغ المصروف.</p>
      </section>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!eligibleLandedCostInvoices.length}>حفظ مسودة الرسملة</button><button className="subtle-button" type="button" onClick={()=>setShowForm(false)}>إلغاء</button></div>
    </form>}
    {tab === "landed-cost" && <div className="data-table-wrap"><table><thead><tr><th>رقم المستند</th><th>التاريخ</th><th>فاتورة المصروف</th><th>المورد</th><th>التوزيعات</th><th>الإجمالي</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{landedCosts.filter(row=>[row.document_number,row.source_invoice_number,row.supplier_name].join(" ").toLowerCase().includes(search.toLowerCase())).map(row=><tr key={row.id}><td className="code-cell">{row.document_number}</td><td>{row.document_date}</td><td>{row.source_invoice_number}</td><td>{row.supplier_name}</td><td>{row.allocation_lines.map(line=><div key={line.id}>{line.product_code} · {money(line.amount)}</div>)}</td><td><strong>{money(row.total_amount)}</strong></td><td><span className={statusPill(row.status)}>{row.status==="draft"?"مسودة":row.status==="posted"?"مرحّل":"معكوس"}</span></td><td><div className="table-actions">{row.status==="draft"?<button className="primary-button" type="button" disabled={busy} onClick={()=>void postLandedCost(row.id)}>ترحيل</button>:row.status==="posted"?<button className="subtle-button" type="button" disabled={busy} onClick={()=>void reverseLandedCost(row.id)}>عكس الرسملة</button>:"—"}</div></td></tr>)}</tbody></table>{landedCosts.length===0&&<div className="table-empty">لا توجد مستندات رسملة تكلفة حتى الآن.</div>}</div>}

    {tab === "sourcing" && <div className="inventory-tabs"><button className={sourcingView === "rfqs" ? "inventory-tab active" : "inventory-tab"} type="button" onClick={()=>{setSourcingView("rfqs");setShowForm(false);}}>طلبات العروض <b>{rfqs.length}</b></button><button className={sourcingView === "quotations" ? "inventory-tab active" : "inventory-tab"} type="button" onClick={()=>{setSourcingView("quotations");setShowForm(false);}}>مقارنة العروض <b>{quotations.length}</b></button></div>}
    {convertingRFQId !== null && tab === "sourcing" && <form className="inventory-form" onSubmit={e=>void submitAwardConversion(e)}>
      <div className="inventory-wide"><h3>إصدار أمر شراء من العرض الفائز</h3><p>سيتم نقل المورد والكميات والأسعار بعد الخصم والضريبة من العرض المعتمد إلى أمر شراء مسودة للمراجعة.</p></div>
      <label>مخزن الاستلام<select required value={awardOrderForm.warehouse} onChange={e=>setAwardOrderForm({...awardOrderForm,warehouse:e.target.value})}><option value="">اختر المخزن</option>{warehouses.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>رقم أمر الشراء<input required value={awardOrderForm.order_number} onChange={e=>setAwardOrderForm({...awardOrderForm,order_number:e.target.value})}/></label>
      <label>تاريخ الأمر<input required type="date" value={awardOrderForm.order_date} onChange={e=>setAwardOrderForm({...awardOrderForm,order_date:e.target.value})}/></label>
      <label>التوريد المتوقع<input type="date" min={awardOrderForm.order_date} value={awardOrderForm.expected_date} onChange={e=>setAwardOrderForm({...awardOrderForm,expected_date:e.target.value})}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>إنشاء أمر شراء مسودة</button><button className="subtle-button" type="button" onClick={()=>setConvertingRFQId(null)}>إلغاء</button></div>
    </form>}
    {showForm && tab === "sourcing" && sourcingView === "rfqs" && <form className="inventory-form" onSubmit={e=>void saveRFQ(e)}>
      <label>رقم طلب العروض<input required value={rfqForm.rfq_number} onChange={e=>setRfqForm({...rfqForm,rfq_number:e.target.value})}/></label>
      <label>تاريخ الإصدار<input required type="date" value={rfqForm.issue_date} onChange={e=>setRfqForm({...rfqForm,issue_date:e.target.value})}/></label>
      <label>آخر موعد للعروض<input required type="date" min={rfqForm.issue_date} value={rfqForm.closing_date} onChange={e=>setRfqForm({...rfqForm,closing_date:e.target.value})}/></label>
      <label className="inventory-wide">الشروط والمواصفات<textarea value={rfqForm.terms} onChange={e=>setRfqForm({...rfqForm,terms:e.target.value})}/></label>
      <div className="inventory-wide"><h3>الموردون المدعوون</h3><div className="inventory-form-row">{suppliers.map(x=><label key={x.id}><input type="checkbox" checked={rfqSuppliers.includes(String(x.id))} onChange={e=>setRfqSuppliers(old=>e.target.checked?[...old,String(x.id)]:old.filter(v=>v!==String(x.id)))}/>{x.code} - {x.name_ar}</label>)}</div></div>
      <div className="inventory-wide"><div className="work-panel-head"><h3>بنود طلب العروض</h3><button className="subtle-button" type="button" onClick={()=>setRfqLines(old=>[...old,{product:"",quantity:"1",notes:""}])}>＋ إضافة بند</button></div></div>
      {rfqLines.map((line,index)=><div className="inventory-form-row inventory-wide" key={index}>
        <label>الصنف<select required value={line.product} onChange={e=>setRfqLines(old=>old.map((x,i)=>i===index?{...x,product:e.target.value}:x))}><option value="">اختر الصنف</option>{products.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setRfqLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/></label>
        <label>مواصفة البند<input value={line.notes} onChange={e=>setRfqLines(old=>old.map((x,i)=>i===index?{...x,notes:e.target.value}:x))}/></label>
        <button className="subtle-button" type="button" disabled={rfqLines.length===1} onClick={()=>setRfqLines(old=>old.filter((_,i)=>i!==index))}>حذف</button>
      </div>)}
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>حفظ طلب العروض كمسودة</button><button className="subtle-button" type="button" onClick={()=>setShowForm(false)}>إلغاء</button></div>
    </form>}
    {showForm && tab === "sourcing" && sourcingView === "quotations" && <form className="inventory-form" onSubmit={e=>void saveQuotation(e)}>
      <label>طلب العروض<select required value={quotationRfqId} onChange={e=>{setQuotationRfqId(e.target.value);setQuotationForm(x=>({...x,supplier:""}));setQuotationPrices({});}}><option value="">اختر طلبًا مفتوحًا</option>{rfqs.filter(x=>x.status==="open").map(x=><option key={x.id} value={x.id}>{x.rfq_number} · {x.closing_date}</option>)}</select></label>
      <label>المورد المدعو<select required value={quotationForm.supplier} onChange={e=>setQuotationForm({...quotationForm,supplier:e.target.value})}><option value="">اختر المورد</option>{selectedRFQ?.supplier_list.filter(x=>x.status==="invited").map(x=><option key={x.id} value={x.id}>{x.code} - {x.name}</option>)}</select></label>
      <label>رقم عرض المورد<input required value={quotationForm.quotation_number} onChange={e=>setQuotationForm({...quotationForm,quotation_number:e.target.value})}/></label>
      <label>تاريخ العرض<input required type="date" value={quotationForm.quotation_date} onChange={e=>setQuotationForm({...quotationForm,quotation_date:e.target.value})}/></label>
      <label>صالح حتى<input required type="date" min={quotationForm.quotation_date} value={quotationForm.valid_until} onChange={e=>setQuotationForm({...quotationForm,valid_until:e.target.value})}/></label>
      <label>مدة التوريد (يوم)<input type="number" min="0" value={quotationForm.delivery_days} onChange={e=>setQuotationForm({...quotationForm,delivery_days:e.target.value})}/></label>
      <label>شروط السداد<input value={quotationForm.payment_terms} onChange={e=>setQuotationForm({...quotationForm,payment_terms:e.target.value})}/></label>
      <div className="inventory-wide"><h3>تسعير البنود</h3>{selectedRFQ?.line_items.map(line=><div className="inventory-form-row" key={line.id}><div><strong>{line.product_code} - {line.product_name}</strong><small>الكمية: {line.quantity}</small></div><label>سعر الوحدة<input required type="number" min="0" step="0.0001" value={quotationPrices[line.id]?.unit_price||""} onChange={e=>setQuotationPrices(old=>({...old,[line.id]:{unit_price:e.target.value,discount_percent:old[line.id]?.discount_percent||"0",tax_rate:old[line.id]?.tax_rate||"0"}}))}/></label><label>خصم %<input type="number" min="0" max="100" step="0.0001" value={quotationPrices[line.id]?.discount_percent||"0"} onChange={e=>setQuotationPrices(old=>({...old,[line.id]:{unit_price:old[line.id]?.unit_price||"",discount_percent:e.target.value,tax_rate:old[line.id]?.tax_rate||"0"}}))}/></label><label>ضريبة %<input type="number" min="0" max="100" step="0.0001" value={quotationPrices[line.id]?.tax_rate||"0"} onChange={e=>setQuotationPrices(old=>({...old,[line.id]:{unit_price:old[line.id]?.unit_price||"",discount_percent:old[line.id]?.discount_percent||"0",tax_rate:e.target.value}}))}/></label></div>)}</div>
      <label className="inventory-wide">ملاحظات<textarea value={quotationForm.notes} onChange={e=>setQuotationForm({...quotationForm,notes:e.target.value})}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!selectedRFQ}>حفظ عرض المورد</button><button className="subtle-button" type="button" onClick={()=>setShowForm(false)}>إلغاء</button></div>
    </form>}
    {tab === "sourcing" && sourcingView === "rfqs" && <div className="data-table-wrap"><table><thead><tr><th>رقم الطلب</th><th>الإصدار</th><th>الإغلاق</th><th>البنود</th><th>الموردون</th><th>العروض</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{rfqs.filter(x=>[x.rfq_number,...x.supplier_list.map(v=>v.name)].join(" ").toLowerCase().includes(search.toLowerCase())).map(x=><tr key={x.id}><td className="code-cell">{x.rfq_number}</td><td>{x.issue_date}</td><td>{x.closing_date}</td><td>{x.line_items.length}</td><td>{x.supplier_list.length}</td><td>{quotations.filter(q=>q.rfq===x.id).length}</td><td><span className={statusPill(x.status)}>{x.status==="draft"?"مسودة":x.status==="open"?"مفتوح":x.status==="closed"?"مغلق":x.status==="awarded"?"تمت الترسية":"ملغي"}</span></td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("purchase_rfq","طلب عروض أسعار",x.rfq_number,x.issue_date,x.supplier_list.map(v=>v.name).join("، "),x.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,detail:line.notes})),[{label:"آخر موعد لتقديم العروض",value:x.closing_date}],x.terms)}>طباعة</button>{x.status==="draft"?<button className="primary-button" type="button" disabled={busy} onClick={()=>void openRFQ(x.id)}>فتح استقبال العروض</button>:x.status==="open"?<button className="subtle-button" type="button" disabled={busy} onClick={()=>void closeRFQ(x.id)}>إغلاق الطلب</button>:x.status==="awarded"?(x.converted_order?"تم إنشاء أمر الشراء":<button className="primary-button" type="button" disabled={busy} onClick={()=>beginAwardConversion(x)}>إنشاء أمر شراء</button>):"—"}</div></td></tr>)}</tbody></table>{rfqs.length===0&&<div className="table-empty">لا توجد طلبات عروض أسعار.</div>}</div>}
    {tab === "sourcing" && sourcingView === "quotations" && <div className="data-table-wrap"><div className="customer-toolbar"><select value={quotationRfqId} onChange={e=>setQuotationRfqId(e.target.value)}><option value="">كل طلبات العروض</option>{rfqs.map(x=><option key={x.id} value={x.id}>{x.rfq_number}</option>)}</select></div><table><thead><tr><th>طلب العروض</th><th>المورد</th><th>رقم العرض</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>التوريد</th><th>السداد</th><th>الصلاحية</th><th>الترسية</th></tr></thead><tbody>{quotations.filter(q=>(!quotationRfqId||q.rfq===Number(quotationRfqId))&&[q.quotation_number,q.supplier_name].join(" ").toLowerCase().includes(search.toLowerCase())).map(q=>{const parent=rfqs.find(x=>x.id===q.rfq);return <tr key={q.id}><td>{parent?.rfq_number||q.rfq}</td><td>{q.supplier_name}</td><td className="code-cell">{q.quotation_number}</td><td>{money(q.total_before_tax)}</td><td>{money(q.tax_total)}</td><td><strong>{money(q.grand_total)}</strong></td><td>{q.delivery_days} يوم</td><td>{q.payment_terms||"—"}</td><td>{q.valid_until}</td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("supplier_quote","عرض سعر مورد",q.quotation_number,q.quotation_date,q.supplier_name,q.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,unitPrice:line.unit_price,amount:line.total_amount})),[{label:"الصافي",value:money(q.total_before_tax)},{label:"الضريبة",value:money(q.tax_total)},{label:"الإجمالي",value:money(q.grand_total),emphasis:true}],q.notes,parent?.rfq_number||"")}>طباعة</button>{parent?.status==="awarded"?(parent.awarded_quotation===q.id?"العرض الفائز":"—"):(parent?.status==="open"||parent?.status==="closed")?<button className="subtle-button" type="button" disabled={busy} onClick={()=>void awardQuotation(q.rfq,q.id)}>ترسية العرض</button>:"—"}</div></td></tr>})}</tbody></table>{quotations.length===0&&<div className="table-empty">لم تسجل عروض موردين بعد.</div>}</div>}

    {convertingReqId !== null && tab === "requisitions" && <form className="inventory-form" onSubmit={(event) => void convertRequisition(event)}>
      <div className="inventory-wide"><h3>تحويل طلب الاحتياج إلى أمر شراء</h3><p>اختيار المورد والمخزن يتم بعد اعتماد الاحتياج.</p></div>
      <label>المورد<select required value={conversionForm.supplier} onChange={e => setConversionForm({ ...conversionForm, supplier: e.target.value })}><option value="">اختر المورد</option>{suppliers.map(x => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>مخزن الاستلام<select required value={conversionForm.warehouse} onChange={e => setConversionForm({ ...conversionForm, warehouse: e.target.value })}><option value="">اختر المخزن</option>{warehouses.map(x => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
      <label>رقم أمر الشراء<input required value={conversionForm.order_number} onChange={e => setConversionForm({ ...conversionForm, order_number: e.target.value })} /></label>
      <label>تاريخ أمر الشراء<input required type="date" value={conversionForm.order_date} onChange={e => setConversionForm({ ...conversionForm, order_date: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>إنشاء أمر الشراء</button><button className="subtle-button" type="button" onClick={() => setConvertingReqId(null)}>إلغاء</button></div>
    </form>}
    {showForm && tab === "requisitions" && <form className="inventory-form" onSubmit={(event) => void saveRequisition(event)}>
      <div className="inventory-wide"><h3>{editingReqId ? "تعديل طلب الاحتياج" : "طلب احتياج جديد"}</h3>{editingReqId && <p>بعد حفظ تعديل الطلب المرفوض سيعود لمسودة ويحتاج إلى اعتماد جديد.</p>}</div>
      <label>رقم طلب الاحتياج<input required value={requisitionForm.request_number} onChange={e => setRequisitionForm({ ...requisitionForm, request_number: e.target.value })} /></label>
      <label>تاريخ الطلب<input required type="date" value={requisitionForm.request_date} onChange={e => setRequisitionForm({ ...requisitionForm, request_date: e.target.value })} /></label>
      <label>تاريخ الاحتياج<input type="date" value={requisitionForm.required_date} onChange={e => setRequisitionForm({ ...requisitionForm, required_date: e.target.value })} /></label>
      <label>القسم الطالب<input value={requisitionForm.department} onChange={e => setRequisitionForm({ ...requisitionForm, department: e.target.value })} /></label>
      <label className="inventory-wide">الغرض من الطلب<textarea value={requisitionForm.purpose} onChange={e => setRequisitionForm({ ...requisitionForm, purpose: e.target.value })} /></label>
      <div className="inventory-wide"><div className="work-panel-head"><div><h3>الأصناف المطلوبة</h3><p>طلب احتياج داخلي قبل تحديد المورد والسعر النهائي.</p></div><button className="subtle-button" type="button" onClick={() => setRequisitionLines(old => [...old, { product: "", requested_quantity: "1", estimated_unit_price: "" }])}>＋ إضافة صنف</button></div></div>
      {requisitionLines.map((line, index) => <div className="inventory-form-row inventory-wide" key={index}>
        <label>الصنف<select required value={line.product} onChange={e => setRequisitionLines(old => old.map((x,i) => i===index ? { ...x, product: e.target.value } : x))}><option value="">اختر الصنف</option>{products.map(x => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>الكمية المطلوبة<input required type="number" min="0.0001" step="0.0001" value={line.requested_quantity} onChange={e => setRequisitionLines(old => old.map((x,i) => i===index ? { ...x, requested_quantity: e.target.value } : x))} /></label>
        <label>سعر تقديري للوحدة<input required type="number" min="0" step="0.0001" value={line.estimated_unit_price} onChange={e => setRequisitionLines(old => old.map((x,i) => i===index ? { ...x, estimated_unit_price: e.target.value } : x))} /></label>
        <button className="subtle-button" type="button" disabled={requisitionLines.length===1} onClick={() => setRequisitionLines(old => old.filter((_,i)=>i!==index))}>حذف</button>
      </div>)}
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : editingReqId ? "حفظ التعديلات" : "حفظ طلب الاحتياج كمسودة"}</button><button className="subtle-button" type="button" disabled={busy} onClick={() => { setEditingReqId(null); setShowForm(false); }}>إلغاء</button></div>
    </form>}
    {tab === "requisitions" && <div className="inventory-table-wrap"><table className="inventory-table"><thead><tr><th>رقم الطلب</th><th>القسم</th><th>تاريخ الاحتياج</th><th>الأصناف</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
      {requisitions.filter(row => [row.request_number,row.department,row.purpose].join(" ").toLowerCase().includes(search.toLowerCase())).map(row => <tr key={row.id}>
        <td>{row.request_number}</td><td>{row.department || "—"}</td><td>{row.required_date || "—"}</td><td>{row.line_items.map(line => line.product_name + " × " + line.requested_quantity).join("، ")}</td><td><span className={statusPill(row.status)}>{({draft:"مسودة",submitted:"بانتظار الاعتماد",approved:"معتمد",rejected:"مرفوض",converted:"تم التحويل",cancelled:"ملغي"} as Record<string,string>)[row.status]}</span></td>
        <td className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("purchase_req","طلب احتياج",row.request_number,row.request_date,row.department||"—",row.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.requested_quantity,unitPrice:line.estimated_unit_price||undefined,detail:line.notes})),[{label:"تاريخ الاحتياج",value:row.required_date||"—"}],row.purpose)}>طباعة</button>
          {(row.status === "draft" || row.status === "rejected") && <button className="subtle-button" disabled={busy} onClick={() => openRequisitionEdit(row)}>تعديل الطلب</button>}
          {row.status === "draft" && <button className="subtle-button" disabled={busy} onClick={() => void submitRequisition(row.id)}>إرسال للاعتماد</button>}
          {(row.status === "draft" || row.status === "submitted" || row.status === "rejected") && <button className="subtle-button" disabled={busy} onClick={() => void cancelRequisition(row.id)}>إلغاء الطلب</button>}
          {row.status === "submitted" && <><button className="subtle-button" disabled={busy} onClick={() => void reviewRequisition(row.id,"approve")}>اعتماد</button><button className="subtle-button" disabled={busy} onClick={() => void reviewRequisition(row.id,"reject")}>رفض</button></>}
          {row.status === "approved" && <button className="subtle-button" disabled={busy} onClick={() => openRequisitionConversion(row)}>تحويل لأمر شراء</button>}
          {row.review_note && <small>{row.review_note}</small>}
        </td>
      </tr>)}
      {!requisitions.length && <tr><td colSpan={6} className="table-empty">لا توجد طلبات احتياج حتى الآن.</td></tr>}
    </tbody></table></div>}

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
    {showForm && tab === "refunds" && <form className="inventory-form" onSubmit={(event) => void saveRefund(event)}>
      <label className="inventory-wide">المرتجع المرحّل<select required value={refundForm.return_document} onChange={(e) => { const id=e.target.value; const row=returns.find((x)=>x.id===Number(id)); setRefundForm((old)=>({...old,return_document:id,amount:row?.grand_total||""})); }}><option value="">اختر مرتجع مورد مرحّل</option>{refundableReturns.map((row)=><option key={row.id} value={row.id}>{row.return_number} · {row.supplier_name} · {money(row.grand_total)}</option>)}</select></label>
      <label>رقم سند الاسترداد<input required maxLength={37} value={refundForm.refund_number} onChange={(e)=>setRefundForm({...refundForm,refund_number:e.target.value})}/></label>
      <label>تاريخ الاسترداد<input required type="date" value={refundForm.refund_date} onChange={(e)=>setRefundForm({...refundForm,refund_date:e.target.value})}/></label>
      <label>طريقة الاسترداد<select value={refundForm.method} onChange={(e)=>{const method=e.target.value as "cash"|"bank";setRefundForm((old)=>({...old,method,payment_account:String(treasuryAccounts.find((x)=>x.is_active&&x.kind===method)?.account||"")}));}}><option value="bank">تحويل بنكي</option><option value="cash">نقدي</option></select></label>
      <label>حساب الخزينة / البنك<select required value={refundForm.payment_account} onChange={(e)=>setRefundForm({...refundForm,payment_account:e.target.value})}><option value="">اختر الحساب</option>{treasuryAccounts.filter((x)=>x.is_active&&x.kind===refundForm.method).map((x)=><option key={x.id} value={x.account}>{x.code} · {x.name_ar}</option>)}</select></label>
      <label>المبلغ المسترد<input required type="number" min="0.0001" step="0.0001" value={refundForm.amount} onChange={(e)=>setRefundForm({...refundForm,amount:e.target.value})}/></label>
      <label>مرجع التحويل<input value={refundForm.reference} onChange={(e)=>setRefundForm({...refundForm,reference:e.target.value})}/></label>
      <label className="inventory-wide">ملاحظات<textarea value={refundForm.notes} onChange={(e)=>setRefundForm({...refundForm,notes:e.target.value})}/></label>
      <div className="inventory-wide"><p className="inventory-report-note">لا يجوز أن يتجاوز الاسترداد الرصيد الدائن المتاح للمورد. يجب أن يكون الحساب بعملة الشركة ومن نفس فرع المرتجع.</p></div>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy?"جارٍ الحفظ…":"حفظ سند الاسترداد كمسودة"}</button></div>
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
      {receiptForm.purchase_order && <div className="inventory-wide"><strong>أمر الشراء المرتبط: {orders.find((x) => x.id === Number(receiptForm.purchase_order))?.order_number || "—"}</strong><button className="subtle-button" type="button" onClick={() => { setReceiptForm((old) => ({ ...old, purchase_order: "" })); setReceiptLines((old) => old.map((line) => ({ product: line.product, quantity: line.quantity, unit_cost: line.unit_cost, expiry_date: line.expiry_date }))); }}>إلغاء ارتباط أمر الشراء</button></div>}
      <label>مرجع مستند المورد<input value={receiptForm.supplier_delivery_ref} onChange={(e) => setReceiptForm({ ...receiptForm, supplier_delivery_ref: e.target.value })} /></label>
      <div className="inventory-wide"><div className="work-panel-head"><div><h3>أصناف الاستلام</h3><p>أضف كل الأصناف والكميات وتكلفة الوحدة قبل الحفظ.</p></div>{!receiptForm.purchase_order && <button className="subtle-button" type="button" onClick={() => setReceiptLines((old) => [...old, { product: "", quantity: "1", unit_cost: "0", expiry_date: "" }])}>＋ إضافة صنف</button>}</div></div>
      {receiptLines.map((line, index) => <div className="inventory-form-row inventory-wide" key={index}>
        <label>الصنف<select required value={line.product} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, product: e.target.value } : x))}><option value="">اختر الصنف</option>{products.filter(x => x.product_type === "goods").map((x) => <option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, quantity: e.target.value } : x))} /></label>
        <label>تكلفة الوحدة<input required type="number" min="0" step="0.0001" value={line.unit_cost} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, unit_cost: e.target.value } : x))} /></label>
        {products.find((x) => x.id === Number(line.product))?.track_expiry && <label>تاريخ الصلاحية<input required type="date" value={line.expiry_date} onChange={(e) => setReceiptLines((old) => old.map((x, i) => i === index ? { ...x, expiry_date: e.target.value } : x))} /></label>}
        <button className="subtle-button" type="button" disabled={receiptLines.length === 1} onClick={() => setReceiptLines((old) => old.filter((_, i) => i !== index))}>حذف السطر</button>
      </div>)}
      <label className="inventory-wide">ملاحظات<textarea value={receiptForm.notes} onChange={(e) => setReceiptForm({ ...receiptForm, notes: e.target.value })} /></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : "حفظ إذن الاستلام كمسودة"}</button></div>
    </form>}
    {showForm && tab === "invoices" && <form className="inventory-form" onSubmit={(event) => void saveInvoice(event)}>
      <label>نوع فاتورة المورد<select value={invoiceForm.invoice_type} onChange={e=>setInvoiceForm({...invoiceForm,invoice_type:e.target.value as "inventory"|"direct_expense",receipt:""})}><option value="inventory">مشتريات مخزنية مرتبطة باستلام</option><option value="direct_expense">خدمات ومصروفات مباشرة</option></select></label>
      {invoiceForm.invoice_type === "inventory" ? <label className="inventory-wide">إذن الاستلام المرحّل<select required value={invoiceForm.receipt} onChange={(e) => {
         const receiptId = e.target.value;
         const selected = receipts.find((row) => row.id === Number(receiptId));
         setInvoiceForm({ ...invoiceForm, receipt: receiptId });
         setInvoiceQuantities(selected ? Object.fromEntries(selected.line_items.map((line) => [
           line.id, remainingReceiptLineQuantity(selected, line).toFixed(4),
         ])) as Record<number, string> : {});
       }}><option value="">اختر إذن الاستلام</option>{invoiceableReceipts.map((x) => <option key={x.id} value={x.id}>{x.receipt_number} - {x.supplier_name} - {x.receipt_date} · متبقي للفوترة</option>)}</select></label> : <>
        <label>المورد<select required value={invoiceForm.supplier} onChange={e=>setInvoiceForm({...invoiceForm,supplier:e.target.value,service_acceptance:""})}><option value="">اختر المورد</option>{suppliers.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        <label>محضر إنجاز الخدمة (اختياري)<select value={invoiceForm.service_acceptance} onChange={e=>setInvoiceForm({...invoiceForm,service_acceptance:e.target.value})}><option value="">بدون ربط بمحضر</option>{invoiceServiceAcceptances.map(x=><option key={x.id} value={x.id}>{x.acceptance_number} · أمر الشراء {x.order_number} · {x.acceptance_date}</option>)}</select><small>تظهر المحاضر المعتمدة الخاصة بالمورد المختار فقط.</small></label>
        <label>الفرع<select required value={invoiceForm.branch} onChange={e=>setInvoiceForm({...invoiceForm,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
        {activeActivityAssignments.length > 0 && <label>النشاط المحاسبي<select required value={invoiceForm.activity} onChange={e=>setInvoiceForm({...invoiceForm,activity:e.target.value})}><option value="">اختر النشاط</option>{activeActivityAssignments.map(item=><option key={item.id} value={item.id}>{{commercial:"تجاري",service:"خدمي",medical:"طبي",restaurants:"مطاعم",industrial:"صناعي"}[item.activity]}</option>)}</select><small>يحدد حساب المورد والضريبة المرتبطين بهذا النشاط.</small></label>}
      </>}
      <label>رقم فاتورة المورد<input required value={invoiceForm.invoice_number} onChange={(e) => setInvoiceForm({ ...invoiceForm, invoice_number: e.target.value })} /></label>
      <label>تاريخ الفاتورة<input required type="date" value={invoiceForm.invoice_date} onChange={(e) => setInvoiceForm({ ...invoiceForm, invoice_date: e.target.value })} /></label>
      <label>عملة فاتورة المورد<select value={invoiceForm.transaction_currency} onChange={e => { const rate=e.target.value ? findRate(e.target.value, invoiceForm.invoice_date) : null; setInvoiceForm({...invoiceForm, transaction_currency:e.target.value, exchange_rate:rate?.rate || "", exchange_rate_date:rate?.rate_date || invoiceForm.invoice_date}); }}><option value="">العملة الأساسية للشركة</option>{currencies.map(currency=><option key={currency.id} value={currency.id}>{currency.code} · {currency.name_ar}</option>)}</select></label>
      {invoiceForm.transaction_currency && <>
        <label>تاريخ سعر الصرف<input required type="date" value={invoiceForm.exchange_rate_date} onChange={e => { const rate=findRate(invoiceForm.transaction_currency,e.target.value); setInvoiceForm({...invoiceForm, exchange_rate_date:e.target.value, exchange_rate:rate?.rate || invoiceForm.exchange_rate}); }} /></label>
        <label>سعر الصرف مقابل العملة الأساسية<input required type="number" min="0.0000000001" step="0.0000000001" value={invoiceForm.exchange_rate} onChange={e => setInvoiceForm({...invoiceForm, exchange_rate:e.target.value})} /></label>
        <p className="inventory-wide">{invoiceForm.invoice_type === "inventory" ? "تُحوّل أسعار الوحدات الأجنبية إلى العملة الأساسية، ويجب أن تطابق قيمة صافي الفاتورة قيمة الاستلام المخزني." : "أدخل قيم البنود بالعملة الأجنبية المختارة؛ سيحوّلها النظام إلى العملة الأساسية عند الحفظ."}</p>
      </>}
      <label>كود ضريبة المدخلات<select value={invoiceForm.tax_code} onChange={(e) => { const selected=taxCodes.find(code=>code.id===Number(e.target.value)); setInvoiceForm({ ...invoiceForm, tax_code:e.target.value, tax_rate:selected?.rate || "0" }); }}><option value="">بدون كود — إدخال يدوي</option>{taxCodes.map(code=><option key={code.id} value={code.id}>{code.code} · {code.name_ar} · {code.rate}% · {code.input_tax_recoverable ? "قابلة للخصم" : "غير قابلة للخصم"}</option>)}</select><small>{invoiceForm.invoice_type === "inventory" ? "الضريبة غير القابلة للخصم تُعلّق في حساب التسوية حتى اكتمال فوترة كمية سطر الاستلام، ثم تُرسمل على المخزون والتكلفة المستهلكة." : "الضريبة غير القابلة للخصم تُضاف مباشرة إلى حساب المصروف المحدد في كل بند."}</small></label>
      {!invoiceForm.tax_code && <label>نسبة الضريبة %<input required type="number" min="0" max="100" step="0.0001" value={invoiceForm.tax_rate} onChange={(e) => setInvoiceForm({ ...invoiceForm, tax_rate: e.target.value })} /><small>أدخل النسبة المعتمدة للمعاملة؛ تُحسب الضريبة على قيمة الأصناف غير شاملة الضريبة.</small></label>}
      {invoiceForm.invoice_type === "direct_expense" && <div className="inventory-wide">
        <h3>بنود الخدمات والمصروفات</h3>
        {directInvoiceLines.map((line,index)=><div className="inventory-form" key={index}>
          <label className="inventory-wide">وصف الخدمة أو المصروف<input required value={line.description} onChange={e=>setDirectInvoiceLines(old=>old.map((x,i)=>i===index?{...x,description:e.target.value}:x))}/></label>
          <label>حساب المصروف<select required value={line.expense_account} onChange={e=>setDirectInvoiceLines(old=>old.map((x,i)=>i===index?{...x,expense_account:e.target.value}:x))}><option value="">اختر حساب المصروف</option>{expenseAccounts.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
          <label>مركز التكلفة<select value={line.cost_center} onChange={e=>setDirectInvoiceLines(old=>old.map((x,i)=>i===index?{...x,cost_center:e.target.value}:x))}><option value="">بدون مركز تكلفة</option>{costCenters.map(x=><option key={x.id} value={x.id}>{x.code} - {x.name_ar}</option>)}</select></label>
          <label>الكمية<input required type="number" min="0.0001" step="0.0001" value={line.quantity} onChange={e=>setDirectInvoiceLines(old=>old.map((x,i)=>i===index?{...x,quantity:e.target.value}:x))}/></label>
          <label>سعر الوحدة {invoiceForm.transaction_currency ? "(بالعملة الأجنبية)" : "(بالعملة الأساسية)"}<input required type="number" min="0" step="0.0001" value={line.unit_price} onChange={e=>setDirectInvoiceLines(old=>old.map((x,i)=>i===index?{...x,unit_price:e.target.value}:x))}/></label>
          {directInvoiceLines.length>1 && <button className="subtle-button" type="button" onClick={()=>setDirectInvoiceLines(old=>old.filter((_,i)=>i!==index))}>حذف البند</button>}
        </div>)}
        <button className="subtle-button" type="button" onClick={()=>setDirectInvoiceLines(old=>[...old,{description:"",expense_account:String(expenseAccounts[0]?.id || ""),cost_center:"",quantity:"1",unit_price:"0"}])}>＋ إضافة بند مصروف</button>
      </div>}
      {invoiceForm.receipt && (() => {
        const selectedReceipt = receipts.find((x) => x.id === Number(invoiceForm.receipt));
        if (!selectedReceipt) return null;
        return <div className="inventory-wide data-table-wrap">
          <table><thead><tr><th>الصنف</th><th>المستلم</th><th>مفوتر سابقًا</th><th>محجوز بمسودات</th><th>متبقي</th><th>الكمية في الفاتورة الحالية</th><th>سعر الوحدة</th><th>الصافي</th></tr></thead>
            <tbody>{selectedReceipt.line_items.map((line) => {
              const remaining = remainingReceiptLineQuantity(selectedReceipt, line);
              if (remaining <= 0) return null;
              const billed = invoiceQuantities[line.id] ?? remaining.toFixed(4);
              return <tr key={line.id}>
                <td>{line.product_code} - {line.product_name}</td>
                <td>{line.quantity}</td>
                <td>{receiptLineQuantityForStatus(selectedReceipt.id, line.id, "posted").toFixed(4)}</td>
                <td>{receiptLineQuantityForStatus(selectedReceipt.id, line.id, "draft").toFixed(4)}</td>
                <td>{remaining.toFixed(4)}</td>
                <td><input aria-label={`كمية الفوترة ${line.product_code}`} type="number" min="0" max={remaining} step="0.0001" value={billed} onChange={(e) => setInvoiceQuantities((old) => ({ ...old, [line.id]: e.target.value }))} /></td>
                <td>{money(line.unit_cost)}</td>
                <td>{money(multiplyDecimalToFixed(billed || "0", line.unit_cost, 4))}</td>
              </tr>;
            })}</tbody>
          </table>
          <p>يمكن فوترة جزء من الكمية أو كل المتبقي. يجب أن يساوي صافي كل فاتورة تكلفة الكمية المفوترة، ويُخفض GRNI بقيمة الجزء المفوتر فقط.</p>
        </div>;
      })()}
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
        <label>المقابل بالعملة الأساسية<input readOnly value={formatMoney(multiplyDecimalToFixed(paymentForm.transaction_amount || "0", paymentForm.exchange_rate || "0", 4))} /></label>
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
    {tab === "aging" && <div className="inventory-tabs"><button type="button" className={agingView==="supplier"?"inventory-tab active":"inventory-tab"} onClick={()=>setAgingView("supplier")}>أعمار مديونية الموردين</button><button type="button" className={agingView==="grni"?"inventory-tab active":"inventory-tab"} onClick={()=>setAgingView("grni")}>مطابقة GRNI</button><button type="button" className={agingView==="landed-cost"?"inventory-tab active":"inventory-tab"} onClick={()=>{setAgingView("landed-cost");setLandedCostReport(null);}}>تسوية تكاليف الشراء</button><button type="button" className={agingView==="matching"?"inventory-tab active":"inventory-tab"} onClick={()=>{setAgingView("matching");setPurchaseMatchingReport(null);}}>مطابقة أمر الشراء والاستلام والفاتورة</button><button type="button" className={agingView==="analysis"?"inventory-tab active":"inventory-tab"} onClick={()=>{setAgingView("analysis");setPurchaseAnalysisReport(null);}}>تحليل المورد والصنف</button></div>}
    {tab === "aging" && agingView === "supplier" && <div className="inventory-form">
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
    {tab === "aging" && agingView === "landed-cost" && <div className="inventory-form">
      <div className="inventory-form-row"><label>من تاريخ<input type="date" value={landedCostReportFrom} onChange={e=>{setLandedCostReportFrom(e.target.value);setLandedCostReport(null);}}/></label><label>إلى تاريخ<input type="date" value={landedCostReportTo} onChange={e=>{setLandedCostReportTo(e.target.value);setLandedCostReport(null);}}/></label></div>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy} onClick={()=>void loadLandedCostReport()}>{busy?"جارٍ التحميل…":"عرض تسوية تكاليف الشراء"}</button>{landedCostReport && <a className="subtle-button" href={`/api/purchasing/landed-costs/export-excel/?company=${companyId}${landedCostReportFrom? `&date_from=${landedCostReportFrom}`:""}${landedCostReportTo? `&date_to=${landedCostReportTo}`:""}`}>تصدير Excel</a>}</div>
      {landedCostReport && <div className="inventory-wide">
        <div className="stock-card-summary">
          <div><span>إجمالي التكلفة الموزعة</span><strong>{money(landedCostReport.totals.allocated)}</strong></div>
          <div><span>المخزون المتبقي</span><strong>{money(landedCostReport.totals.inventory)}</strong></div>
          <div><span>تكلفة المبيعات</span><strong>{money(landedCostReport.totals.cogs)}</strong></div>
          <div><span>خسائر المخزون</span><strong>{money(landedCostReport.totals.adjustment_loss)}</strong></div>
          <div><span>رسملات معكوسة</span><strong>{money(landedCostReport.totals.reversed)}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>مستند الرسملة</th><th>فاتورة المصروف</th><th>المورد</th><th>الصنف</th><th>الفرع</th><th>المخزون</th><th>تكلفة المبيعات</th><th>الخسائر</th><th>القيد</th></tr></thead><tbody>{landedCostReport.rows.map((row,index)=><tr key={row.document_id+"-"+row.expense_line_id+"-"+row.branch_id+"-"+index}><td>{row.document_number}<small>{row.document_date}</small></td><td>{row.source_invoice_number}</td><td>{row.supplier_name}</td><td>{row.product_code} · {row.product_name}</td><td>{row.branch_name}</td><td>{money(row.inventory_amount)}</td><td>{money(row.cogs_amount)}</td><td>{money(row.adjustment_loss_amount)}</td><td>{row.journal_entry||"—"}</td></tr>)}</tbody></table>{landedCostReport.rows.length===0&&<div className="table-empty">لا توجد رسملات في الفترة المحددة.</div>}</div>
        <p className="inventory-report-note">يتم عرض أثر الرسملة حسب الفرع، مع فصل قيمة المخزون عن تكلفة المبيعات وخسائر المخزون، وربط كل توزيع بقيده المحاسبي.</p>
      </div>}
    </div>}
    {tab === "aging" && agingView === "analysis" && <div className="inventory-form">
      <div className="inventory-form-row"><label>من تاريخ الفاتورة<input type="date" value={purchaseAnalysisFrom} onChange={e=>{setPurchaseAnalysisFrom(e.target.value);setPurchaseAnalysisReport(null);}}/></label><label>إلى تاريخ الفاتورة<input type="date" value={purchaseAnalysisTo} onChange={e=>{setPurchaseAnalysisTo(e.target.value);setPurchaseAnalysisReport(null);}}/></label></div>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy} onClick={()=>void loadPurchaseAnalysisReport()}>{busy?"جارٍ التحميل…":"عرض تحليل المشتريات"}</button>{purchaseAnalysisReport && <a className="subtle-button" href={`/api/purchasing/invoices/analysis-report-export-excel/?company=${companyId}${purchaseAnalysisFrom? `&date_from=${purchaseAnalysisFrom}`:""}${purchaseAnalysisTo? `&date_to=${purchaseAnalysisTo}`:""}`}>تصدير Excel</a>}</div>
      {purchaseAnalysisReport && <div className="inventory-wide">
        <div className="stock-card-summary">
          <div><span>صافي المشتريات</span><strong>{money(purchaseAnalysisReport.totals.net_purchases)}</strong></div>
          <div><span>مرتجعات الموردين</span><strong>{money(purchaseAnalysisReport.totals.returned_net)}</strong></div>
          <div><span>ضريبة غير قابلة للخصم</span><strong>{money(purchaseAnalysisReport.totals.nonrecoverable_tax)}</strong></div>
          <div><span>منها على المخزون / COGS</span><strong>{money(purchaseAnalysisReport.totals.inventory_nonrecoverable_tax)}</strong></div>
          <div><span>التكلفة بعد الضريبة غير القابلة للخصم</span><strong>{money(purchaseAnalysisReport.totals.total_cost)}</strong></div>
          <div><span>عدد الفواتير</span><strong>{purchaseAnalysisReport.totals.invoice_count}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>النوع</th><th>المورد</th><th>الصنف / حساب المصروف</th><th>الفرع / المخزن</th><th>الكمية</th><th>المرتجع</th><th>صافي المشتريات</th><th>ضريبة غير قابلة للخصم</th><th>التكلفة</th></tr></thead><tbody>{purchaseAnalysisReport.rows.map((row,index)=><tr key={row.kind+"-"+row.supplier_id+"-"+(row.product_id||row.expense_account_id)+"-"+row.branch_id+"-"+index}><td>{row.kind==="inventory"?"مخزون":"مصروف مباشر"}</td><td>{row.supplier_name}</td><td>{row.kind==="inventory"?(row.product_code+" · "+row.product_name):(row.expense_account_code+" · "+row.expense_account_name)}</td><td>{row.branch_name}<small>{row.warehouse_name}</small></td><td>{row.quantity}</td><td>{row.returned_quantity}</td><td>{money(row.net_purchases)}</td><td>{money(row.nonrecoverable_tax)}</td><td>{money(row.total_cost)}</td></tr>)}</tbody></table>{purchaseAnalysisReport.rows.length===0&&<div className="table-empty">لا توجد فواتير مشتريات مرحلة في الفترة المحددة.</div>}</div>
        <p className="inventory-report-note">ضريبة المدخلات غير القابلة للخصم الموزعة على المخزون وCOGS تظهر في إجمالي الملخص على مستوى الفاتورة والفرع، ولا تُكرر على صفوف الأصناف.</p>
        <p className="inventory-report-note">يحلل التقرير فواتير المشتريات المرحلة حسب المورد والصنف أو حساب المصروف والفرع والمخزن والنشاط، ويخصم مرتجعات الموردين المرحلة.</p>
      </div>}
    </div>}
    {tab === "aging" && agingView === "matching" && <div className="inventory-form">
      <div className="inventory-form-row"><label>من تاريخ الأمر<input type="date" value={matchingReportFrom} onChange={e=>{setMatchingReportFrom(e.target.value);setPurchaseMatchingReport(null);}}/></label><label>إلى تاريخ الأمر<input type="date" value={matchingReportTo} onChange={e=>{setMatchingReportTo(e.target.value);setPurchaseMatchingReport(null);}}/></label></div>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy} onClick={()=>void loadPurchaseMatchingReport()}>{busy?"جارٍ التحميل…":"عرض تقرير المطابقة"}</button>{purchaseMatchingReport && <a className="subtle-button" href={`/api/purchasing/orders/matching-report-export-excel/?company=${companyId}${matchingReportFrom? `&date_from=${matchingReportFrom}`:""}${matchingReportTo? `&date_to=${matchingReportTo}`:""}`}>تصدير Excel</a>}</div>
      {purchaseMatchingReport && <div className="inventory-wide">
        <div className="stock-card-summary">
          <div><span>قيمة أوامر الشراء</span><strong>{money(purchaseMatchingReport.totals.order_value)}</strong></div>
          <div><span>صافي الفواتير المرحلة</span><strong>{money(purchaseMatchingReport.totals.invoiced_net_amount)}</strong></div>
          <div><span>فرق السعر</span><strong>{money(purchaseMatchingReport.totals.price_variance)}</strong></div>
          <div><span>بنود غير مستلمة</span><strong>{purchaseMatchingReport.totals.unreceived_line_count}</strong></div>
          <div><span>بنود مستلمة غير مفوترة</span><strong>{purchaseMatchingReport.totals.uninvoiced_line_count}</strong></div>
          <div><span>بنود مفوترة بأكثر من الاستلام</span><strong>{purchaseMatchingReport.totals.overinvoiced_line_count}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>أمر الشراء</th><th>المورد / المخزن</th><th>الصنف</th><th>المطلوب</th><th>المستلم</th><th>المفوتر</th><th>غير مفوتر</th><th>غير مستلم</th><th>فرق السعر</th><th>الاستلامات / الفواتير</th></tr></thead><tbody>{purchaseMatchingReport.rows.map(row=><tr key={row.order_line_id}><td>{row.order_number}<small>{row.order_date}</small></td><td>{row.supplier_name}<small>{row.warehouse_name}</small></td><td>{row.product_code} · {row.product_name}</td><td>{row.ordered_quantity}</td><td>{row.received_quantity}<small>{Number(row.receipt_link_discrepancy)!==0?("فرق ربط: "+row.receipt_link_discrepancy):""}</small></td><td>{row.invoiced_quantity}</td><td>{row.uninvoiced_received_quantity}</td><td>{row.unreceived_quantity}</td><td>{money(row.price_variance)}</td><td><small>استلام: {row.receipt_numbers.join("، ")||"—"}</small><small>فاتورة: {row.invoice_numbers.join("، ")||"—"}</small></td></tr>)}</tbody></table>{purchaseMatchingReport.rows.length===0&&<div className="table-empty">لا توجد بنود أوامر شراء في الفترة المحددة.</div>}</div>
        <p className="inventory-report-note">يقارن التقرير كميات أمر الشراء بالاستلام والفواتير المرحلة، ويظهر الكميات غير المفوترة أو غير المستلمة وفروق أسعار الفواتير ومرتجعات الموردين.</p>
      </div>}
    </div>}
    {tab === "aging" && agingView === "grni" && <div className="inventory-form">
      <label>تاريخ المطابقة<input type="date" value={agingAsOf} onChange={e=>{setAgingAsOf(e.target.value);setGrniReport(null);}}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="button" disabled={busy} onClick={()=>void loadGrni()}>{busy?"جارٍ التحميل…":"عرض تقرير GRNI"}</button></div>
      {grniReport && <div className="inventory-wide">
        <div className="stock-card-summary">
          <div><span>0–30 يوم</span><strong>{money(grniReport.aging["0_30"])}</strong></div>
          <div><span>31–60 يوم</span><strong>{money(grniReport.aging["31_60"])}</strong></div>
          <div><span>61–90 يوم</span><strong>{money(grniReport.aging["61_90"])}</strong></div>
          <div><span>أكثر من 90 يوم</span><strong>{money(grniReport.aging.over_90)}</strong></div>
          <div><span>إجمالي GRNI المفتوح</span><strong>{money(grniReport.grand_total)}</strong></div>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>إذن الاستلام</th><th>التاريخ</th><th>عمر الرصيد</th><th>المورد</th><th>المخزن</th><th>الفاتورة</th><th>القيمة</th></tr></thead><tbody>{grniReport.rows.map(row=><tr key={row.receipt_id}><td className="code-cell">{row.receipt_number}</td><td>{row.receipt_date}</td><td>{row.age_days} يوم</td><td>{row.supplier_name}</td><td>{row.warehouse_name}</td><td>{row.invoice_number||"لم تفوتر"}</td><td><strong>{money(row.amount)}</strong></td></tr>)}</tbody></table>{grniReport.rows.length===0&&<div className="table-empty">لا توجد استلامات غير مفوترة في تاريخ التقرير.</div>}</div>
        <div className="data-table-wrap"><h3>ملخص شهري حسب تاريخ الاستلام</h3><table><thead><tr><th>الشهر</th><th>قيمة الاستلامات المفتوحة</th></tr></thead><tbody>{grniReport.monthly_summary.map(row=><tr key={row.month}><td>{row.month}</td><td>{money(row.amount)}</td></tr>)}</tbody></table></div>
        <p className="inventory-report-note">يعرض التقرير الرصيد غير المفوتر المتبقي لكل استلام؛ كل فاتورة جزئية تخفض GRNI بقيمة صافي الكمية المفوترة، ويُصفّر الرصيد عند اكتمال الفوترة.</p>
      </div>}
    </div>}

    {tab !== "aging" && tab !== "statement" && tab !== "landed-cost" && <div className="customer-toolbar"><div className="customer-count">{tab === "sourcing" ? "عدد طلبات عروض الأسعار" : tab === "requisitions" ? "عدد طلبات الاحتياج" : tab === "orders" ? "عدد أوامر الشراء" : tab === "service-acceptances" ? "عدد محاضر إنجاز الخدمات" : tab === "receipts" ? "عدد أذون الاستلام" : tab === "invoices" ? "عدد فواتير الموردين" : tab === "returns" ? "عدد مرتجعات الموردين" : "عدد سندات الصرف"} <strong>{(tab === "requisitions" ? requisitions : tab === "orders" ? orders : tab === "service-acceptances" ? serviceAcceptances : tab === "receipts" ? receipts : tab === "invoices" ? invoices : tab === "returns" ? returns : payments).length.toLocaleString("ar-EG")}</strong></div><input placeholder="بحث برقم المستند أو المورد…" value={search} onChange={(e) => setSearch(e.target.value)} /><button className="subtle-button" type="button" onClick={() => void refresh()} disabled={busy}>{busy ? "تحديث…" : "تحديث"}</button></div>}
    {tab === "service-acceptances" && <div className="data-table-wrap"><table><thead><tr><th>رقم المحضر</th><th>أمر الشراء</th><th>المورد</th><th>التاريخ</th><th>الخدمات والكميات</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{serviceAcceptances.filter(row => [row.acceptance_number, row.order_number, row.supplier_name].join(" ").toLowerCase().includes(search.toLowerCase())).map(row => <tr key={row.id}><td className="code-cell">{row.acceptance_number}</td><td>{row.order_number}</td><td>{row.supplier_name}</td><td>{row.acceptance_date}</td><td>{row.line_items.map(line => <div key={line.id}>{line.product_name}: {line.quantity}</div>)}</td><td><span className={statusPill(row.status)}>{row.status === "accepted" ? "معتمد" : row.status === "draft" ? "مسودة" : "مرفوض"}</span></td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("service_accept","محضر إنجاز خدمة",row.acceptance_number,row.acceptance_date,row.supplier_name,row.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,detail:line.notes})),[{label:"أمر الشراء",value:row.order_number}],row.notes)}>طباعة</button>{row.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void acceptServiceAcceptance(row.id)}>اعتماد الإنجاز</button> : "—"}</div></td></tr>)}</tbody></table>{!busy && serviceAcceptances.length === 0 && <div className="table-empty">لا توجد محاضر استلام خدمات.</div>}</div>}
  {tab === "orders" && <div className="data-table-wrap"><table><thead><tr><th>رقم الأمر</th><th>المورد</th><th>المخزن</th><th>تاريخ الأمر</th><th>التوريد المتوقع</th><th>البنود المستلمة/المطلوبة</th><th>الإجمالي</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{orders.filter((row) => [row.order_number, row.supplier_name, row.warehouse_name].join(" ").toLowerCase().includes(search.toLowerCase())).map((row) => <tr key={row.id}><td className="code-cell">{row.order_number}</td><td>{row.supplier_name}</td><td>{row.warehouse_name}</td><td>{row.order_date}</td><td>{row.expected_date || "—"}</td><td>{row.line_items.map((line) => <div key={line.id}>{line.product_code}: {line.product_type === "service" ? `${serviceAcceptances.filter(doc => doc.order === row.id && doc.status === "accepted").flatMap(doc => doc.line_items).filter(item => item.order_line === line.id).reduce((sum, item) => sum + Number(item.quantity), 0)}/${line.quantity} (إنجاز)` : `${line.received_quantity}/${line.quantity}`}</div>)}</td><td><strong>{money(row.grand_total)}</strong></td><td><span className={statusPill(row.status)}>{row.status === "draft" ? (row.approval_status === "pending" ? "بانتظار موافقة المدير" : row.approval_status === "rejected" ? "مرفوض - يحتاج إعادة إرسال" : "مسودة") : row.status === "confirmed" ? "معتمد" : row.status === "partially_received" ? "استلام جزئي" : row.status === "received" ? "مستلم بالكامل" : "ملغي"}</span></td><td><div className="form-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("purchase_order","أمر شراء",row.order_number,row.order_date,row.supplier_name,row.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,unitPrice:line.unit_price,amount:multiplyDecimalToFixed(line.quantity,line.unit_price,4)})),[{label:"إجمالي الأمر",value:money(row.grand_total),emphasis:true}],row.notes,row.warehouse_name)}>طباعة</button>{row.status === "draft" && row.approval_status === "pending" ? "بانتظار موافقة المدير" : row.status === "draft" && <button className="primary-button" type="button" disabled={busy} onClick={() => void confirmOrder(row.id)}>{row.approval_status === "rejected" ? "إعادة الإرسال للاعتماد" : "اعتماد الأمر"}</button>}{(row.status === "confirmed" || row.status === "partially_received") && row.line_items.some(line => line.product_type === "goods") && <button className="subtle-button" type="button" onClick={() => { setTab("receipts"); setShowForm(true); setReceiptForm((old) => ({ ...old, supplier: String(row.supplier), warehouse: String(row.warehouse), purchase_order: String(row.id), receipt_number: "" })); setReceiptLines(row.line_items.filter((line) => line.product_type === "goods" && Number(line.remaining_quantity) > 0).map((line) => ({ product: String(line.product), quantity: line.remaining_quantity, unit_cost: line.unit_price, expiry_date: "", purchase_order_line: String(line.id) }))); }}>إنشاء إذن استلام</button>}{(row.status === "confirmed" || row.status === "partially_received" || row.status === "received") && row.line_items.some(line => line.product_type === "service") && <button className="subtle-button" type="button" disabled={busy} onClick={() => void createServiceAcceptanceForOrder(row)}>محضر إنجاز خدمة</button>}{(row.status === "draft" || row.status === "confirmed") && <button className="subtle-button" type="button" disabled={busy} onClick={() => void cancelOrder(row.id)}>إلغاء الأمر</button>}{(row.status === "received" || row.status === "cancelled") && "—"}</div></td></tr>)}</tbody></table>{!busy && orders.length === 0 && <div className="table-empty">لا توجد أوامر شراء.</div>}</div>}
    {tab === "receipts" && <div className="data-table-wrap"><table><thead><tr><th>رقم الإذن</th><th>المورد</th><th>المخزن</th><th>التاريخ</th><th>مرجع المورد</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>{filteredReceipts.map((x) => <tr key={x.id}><td className="code-cell">{x.receipt_number}</td><td>{x.supplier_name}</td><td>{x.warehouse_name}</td><td>{x.receipt_date}</td><td>{x.supplier_delivery_ref || "—"}</td><td><span className={statusPill(x.movement_status)}>{x.movement_status === "posted" ? "مرحّل" : "مسودة"}</span></td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("purchase_receipt","إذن استلام مشتريات",x.receipt_number,x.receipt_date,x.supplier_name,x.line_items.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,unitPrice:line.unit_cost,amount:line.cost_amount})),[{label:"المخزن",value:x.warehouse_name},{label:"مرجع المورد",value:x.supplier_delivery_ref||"—"}],x.notes)}>طباعة</button>{x.movement_status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postReceipt(x.id)}>ترحيل الاستلام</button> : "تم تحديث المخزون"}</div></td></tr>)}</tbody></table>{!busy && filteredReceipts.length === 0 && <div className="table-empty">لا توجد أذون استلام مطابقة.</div>}</div>}
    {tab === "invoices" && <div className="data-table-wrap"><table><thead><tr><th>رقم الفاتورة</th><th>المورد</th><th>إذن الاستلام</th><th>محضر الخدمة</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>المدفوع</th><th>المتبقي</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredInvoices.map((x) => <tr key={x.id}><td className="code-cell">{x.invoice_number}</td><td>{x.supplier_name}</td><td>{x.receipt_number || "مباشرة"}</td><td>{x.service_acceptance_number || "—"}</td><td>{x.invoice_date}</td><td>{money(x.net_total)}</td><td>{money(x.tax_total)}</td><td>{money(x.grand_total)}</td><td>{money(x.paid_total)}</td><td>{money(x.balance_due)}</td><td><span className={statusPill(x.status === "posted" && Number(x.balance_due) > 0 && Number(x.paid_total) > 0 ? "partial" : x.status)}>{x.status === "posted" ? (Number(x.balance_due) <= 0 ? "مدفوعة" : Number(x.paid_total) > 0 ? "مدفوعة جزئيًا" : "مرحّلة") : "مسودة"}</span></td><td>{x.journal_number || "—"}</td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("purchase_invoice","فاتورة مورد",x.invoice_number,x.invoice_date,x.supplier_name,x.invoice_lines.map(line=>({label:line.description||line.product_name||"بند مشتريات",code:line.product_code||undefined,quantity:line.billed_quantity,unitPrice:line.transaction_unit_price||line.unit_price,amount:line.transaction_total_amount||line.total_amount})),[{label:"الصافي",value:money(x.transaction_net_total||x.net_total)},{label:"الضريبة",value:money(x.transaction_tax_total||x.tax_total)},{label:"الإجمالي",value:money(x.transaction_grand_total||x.grand_total),emphasis:true},{label:"المدفوع",value:money(x.paid_total)},{label:"المتبقي",value:money(x.balance_due)}],x.notes,x.receipt_number||x.service_acceptance_number||"فاتورة مباشرة")}>طباعة</button>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postInvoice(x.id)}>ترحيل الفاتورة</button> : <div className="table-actions"><span>تم الترحيل</span>{x.transaction_currency && Number(x.balance_due)>0 && <button type="button" className="subtle-button" disabled={busy} onClick={()=>void beginFxRevaluation(x)}>إعادة تقييم العملة</button>}</div>}</div></td></tr>)}</tbody></table>{!busy && filteredInvoices.length === 0 && <div className="table-empty">لا توجد فواتير موردين مطابقة.</div>}</div>}
    {tab === "invoices" && fxInvoice && <form className="inventory-form" onSubmit={submitFxRevaluation}>
      <div className="inventory-wide"><strong>إعادة تقييم رصيد المورد: {fxInvoice.invoice_number}</strong><p>إجمالي الفاتورة بالعملة الأجنبية: {money(fxInvoice.transaction_grand_total || "0")} · المتبقي بالعملة الأساسية: {money(fxInvoice.balance_due)}</p></div>
      <label>تاريخ إعادة التقييم<input required type="date" value={fxForm.revaluation_date} onChange={e=>setFxForm({...fxForm,revaluation_date:e.target.value})}/></label>
      <label>تاريخ سعر الصرف المعتمد<input required type="date" max={fxForm.revaluation_date} value={fxForm.rate_date} onChange={e=>{const rate=findRate(String(fxInvoice.transaction_currency),e.target.value);setFxForm({...fxForm,rate_date:e.target.value,rate:rate?.rate||fxForm.rate});}}/></label>
      <label>سعر الصرف مقابل عملة الشركة<input required type="number" min="0.0000000001" step="0.0000000001" value={fxForm.rate} onChange={e=>setFxForm({...fxForm,rate:e.target.value})}/></label>
      <div className="inventory-form-actions inventory-wide"><button className="primary-button" type="submit" disabled={busy||!fxForm.rate}>{busy?"جارٍ التقييم…":"حفظ وإثبات فرق العملة"}</button><button className="subtle-button" type="button" onClick={()=>setFxInvoice(null)}>إغلاق</button></div>
      {fxHistory.length>0&&<div className="data-table-wrap inventory-wide"><table><thead><tr><th>تاريخ التقييم</th><th>سعر الصرف</th><th>الرصيد الأجنبي</th><th>الفرق</th><th>القيد</th><th>الحالة</th></tr></thead><tbody>{fxHistory.map(row=><tr key={row.id}><td>{row.revaluation_date}</td><td>{row.rate}</td><td>{money(row.foreign_balance)}</td><td>{money(row.difference)}</td><td>{row.journal_number||"—"}</td><td>{row.status==="reversed"?"معكوس":row.status==="no_difference"?"دون فرق":"مرحّل"}</td></tr>)}</tbody></table></div>}
    </form>}
    {tab === "returns" && <div className="data-table-wrap"><table><thead><tr><th>رقم المرتجع</th><th>المورد</th><th>فاتورة المورد</th><th>التاريخ</th><th>الصافي</th><th>الضريبة</th><th>الإجمالي</th><th>بالعملة الأجنبية</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredReturns.map((row) => <tr key={row.id}><td className="code-cell">{row.return_number}</td><td>{row.supplier_name}</td><td>{row.invoice_number}</td><td>{row.return_date}</td><td>{money(row.net_amount)}</td><td>{money(row.tax_amount)}</td><td>{money(row.grand_total)}</td><td>{row.transaction_currency_code ? `${money(row.transaction_grand_total || "0")} ${row.transaction_currency_code}` : "—"}</td><td><span className={statusPill(row.status)}>{row.status === "posted" ? "مرحّل" : row.status === "reversed" ? "معكوس" : "مسودة"}</span></td><td>{row.status === "reversed" ? row.reversal_journal_number || "—" : row.journal_number || "—"}</td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("supplier_return","مرتجع مورد",row.return_number,row.return_date,row.supplier_name,row.return_lines.map(line=>({label:line.product_name,code:line.product_code,quantity:line.quantity,amount:line.net_amount})),[{label:"الصافي",value:money(row.net_amount)},{label:"الضريبة",value:money(row.tax_amount)},{label:"الإجمالي",value:money(row.grand_total),emphasis:true}],row.notes,row.invoice_number)}>طباعة</button>{row.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postReturn(row.id)}>ترحيل المرتجع</button> : row.status === "posted" ? <button className="subtle-button" type="button" disabled={busy} onClick={() => void reverseReturn(row.id)}>عكس المرتجع</button> : "تم العكس"}</div></td></tr>)}</tbody></table>{!busy && filteredReturns.length === 0 && <div className="table-empty">لا توجد مرتجعات موردين مطابقة.</div>}</div>}
    {tab === "refunds" && <div className="data-table-wrap"><table><thead><tr><th>رقم الاسترداد</th><th>المرتجع</th><th>المورد</th><th>التاريخ</th><th>الطريقة</th><th>حساب الاستلام</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredRefunds.map((row)=><tr key={row.id}><td className="code-cell">{row.refund_number}</td><td>{row.return_number}</td><td>{row.supplier_name}</td><td>{row.refund_date}</td><td>{row.method==="cash"?"نقدي":"تحويل بنكي"}</td><td>{row.payment_account_name}</td><td>{money(row.amount)}</td><td><span className={statusPill(row.status)}>{row.status==="posted"?"مرحّل":row.status==="reversed"?"معكوس":"مسودة"}</span></td><td>{row.status==="reversed"?row.reversal_journal_number||"—":row.journal_number||"—"}</td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("supplier_refund","استرداد من مورد",row.refund_number,row.refund_date,row.supplier_name,[],[{label:"مبلغ الاسترداد",value:money(row.amount),emphasis:true},{label:"طريقة الاسترداد",value:row.method==="cash"?"نقدي":"تحويل بنكي"},{label:"حساب الاستلام",value:row.payment_account_name}],row.notes,row.return_number)}>طباعة</button>{row.status==="draft"?<button className="primary-button" type="button" disabled={busy} onClick={()=>void postRefund(row.id)}>ترحيل الاسترداد</button>:row.status==="posted"?<button className="subtle-button" type="button" disabled={busy} onClick={()=>void reverseRefund(row.id)}>عكس الاسترداد</button>:"معكوس"}</div></td></tr>)}</tbody></table>{!busy&&filteredRefunds.length===0&&<div className="table-empty">لا توجد استردادات موردين مطابقة.</div>}</div>}
    {tab === "payments" && <div className="data-table-wrap"><table><thead><tr><th>رقم السند</th><th>المورد</th><th>الفاتورة</th><th>التاريخ</th><th>الطريقة</th><th>حساب الصرف</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>الإجراء</th></tr></thead><tbody>{filteredPayments.map((x) => <tr key={x.id}><td className="code-cell">{x.payment_number}</td><td>{x.supplier_name}</td><td>{x.invoice_number}</td><td>{x.payment_date}</td><td>{x.method === "cash" ? "نقدي" : "تحويل بنكي"}</td><td>{x.payment_account_name || "الحساب الافتراضي"}</td><td>{money(x.amount)}</td><td><span className={statusPill(x.status)}>{x.status === "posted" ? "مرحّل" : x.status === "reversed" ? "معكوس" : "مسودة"}</span></td><td>{x.journal_number || "—"}</td><td><div className="table-actions"><button className="subtle-button" type="button" onClick={()=>openPrint("supplier_payment","سند صرف مورد",x.payment_number,x.payment_date,x.supplier_name,[],[{label:"المبلغ",value:money(x.transaction_amount||x.amount),emphasis:true},{label:"طريقة الصرف",value:x.method==="cash"?"نقدي":"تحويل بنكي"},{label:"حساب الصرف",value:x.payment_account_name||"الحساب الافتراضي"},{label:"المرجع",value:x.reference||"—"}],x.reference,x.invoice_number)}>طباعة</button>{x.status === "draft" ? <button className="primary-button" type="button" disabled={busy} onClick={() => void postPayment(x.id)}>ترحيل السند</button> : x.status === "reversed" ? "تم العكس" : <button className="subtle-button" type="button" disabled={busy} onClick={() => { if (window.confirm("سيتم إنشاء قيد عكسي وإلغاء أثر سند الصرف على رصيد المورد. يجب عكس أحدث سند أولًا. هل تريد المتابعة؟")) void reversePayment(x.id); }}>عكس السند</button>}</div></td></tr>)}</tbody></table>{!busy && filteredPayments.length === 0 && <div className="table-empty">لا توجد سندات صرف مطابقة.</div>}</div>}
    {printDocument&&<PurchaseDocumentPrint company={company||null} document={printDocument} onClose={()=>setPrintDocument(null)}/>}
  </section>;
}
