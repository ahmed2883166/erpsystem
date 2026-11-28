/*
 * كشوف حساب العملاء والموردين ضمن مركز التقارير.
 * تعرض الرصيد الافتتاحي والحركات المدينة والدائنة والرصيد المتحرك من واجهات التقارير الحالية.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { formatMoney } from "./formatters";
import {
  api, salesApi, purchasingApi,
  type Customer, type Supplier, type CustomerStatement, type SupplierStatement,
} from "./api";

type Props = { companyId: number | null; currency: string };
type Statement = CustomerStatement | SupplierStatement;
type StatementRow = Statement["rows"][number];

const localDate = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const money = (value: string | number, currency: string) => `${formatMoney(value)} ${currency}`.trim();
const documentLabel = (kind: string) => ({
  invoice: "فاتورة مبيعات",
  receipt: "سند قبض",
  receipt_reversal: "عكس سند قبض",
  receipt_batch: "سند قبض مجمع",
  receipt_batch_reversal: "عكس سند قبض مجمع",
  sales_return: "مردود مبيعات",
  sales_return_reversal: "عكس مردود مبيعات",
  fx_revaluation: "إعادة تقييم عملة",
  fx_revaluation_reversal: "عكس إعادة تقييم عملة",
  payment: "دفعة",
  asset_invoice: "فاتورة شراء أصل",
  asset_payment: "سداد فاتورة أصل",
}[kind] || kind);

// عرض كشف حساب طرف واحد مع حركة تفصيلية ورصيد متحرك.
export default function PartyStatementsPanel({ companyId, currency }: Props) {
  const today = localDate(new Date());
  const [partyType, setPartyType] = useState<"customer" | "supplier">("customer");
  const [partyId, setPartyId] = useState("");
  const [dateFrom, setDateFrom] = useState(`${new Date().getFullYear()}-01-01`);
  const [dateTo, setDateTo] = useState(today);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [statement, setStatement] = useState<Statement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [movementSearch, setMovementSearch] = useState("");
  const requestSequence = useRef(0);

  const parties = partyType === "customer" ? customers : suppliers;
  const selectedParty = useMemo(() => parties.find((party) => party.id === Number(partyId)), [parties, partyId]);
  const partyName = statement
    ? ("customer_name" in statement ? statement.customer_name : statement.supplier_name)
    : selectedParty?.name_ar || "";

  useEffect(() => {
    let active = true;
    setCustomers([]);
    setSuppliers([]);
    setPartyId("");
    setStatement(null);
    setError("");
    if (!companyId) return () => { active = false; };
    Promise.all([api.customers(companyId), api.suppliers(companyId)])
      .then(([customerRows, supplierRows]) => {
        if (!active) return;
        setCustomers(customerRows.filter((item) => item.is_active));
        setSuppliers(supplierRows.filter((item) => item.is_active));
      })
      .catch((reason: Error) => { if (active) setError(reason.message || "تعذر تحميل قائمة العملاء والموردين."); });
    return () => { active = false; };
  }, [companyId]);

  useEffect(() => {
    setPartyId("");
    setStatement(null);
    setError("");
  }, [partyType]);

  const load = async () => {
    if (!companyId || !partyId || !dateFrom || !dateTo || dateFrom > dateTo) {
      setError("اختر العميل أو المورد وحدد نطاق تاريخ صحيح.");
      return;
    }
    const requestId = ++requestSequence.current;
    setBusy(true);
    setError("");
    try {
      const result = partyType === "customer"
        ? await salesApi.customerStatement(companyId, Number(partyId), dateFrom, dateTo)
        : await purchasingApi.supplierStatement(companyId, Number(partyId), dateFrom, dateTo);
      if (requestId === requestSequence.current) setStatement(result);
    } catch (reason) {
      if (requestId === requestSequence.current) {
        setStatement(null);
        setError(reason instanceof Error ? reason.message : "تعذر تحميل كشف الحساب.");
      }
    } finally {
      if (requestId === requestSequence.current) setBusy(false);
    }
  };

  const rows: StatementRow[] = statement?.rows || [];
  const visibleRows = useMemo(() => rows.filter(row => `${row.date} ${documentLabel(row.document_type)} ${row.document_number} ${row.description}`.toLocaleLowerCase("ar").includes(movementSearch.trim().toLocaleLowerCase("ar"))), [rows, movementSearch]);
  const downloadCsv = () => {
    if (!statement) return;
    const values = [
      ["التاريخ", "نوع المستند", "رقم المستند", "البيان", "مدين", "دائن", "الرصيد"],
      ...rows.map((row) => [row.date, documentLabel(row.document_type), row.document_number, row.description, row.debit, row.credit, row.balance]),
    ];
    const csv = "\uFEFF" + values.map((line) => line.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${partyType}-statement-${partyId}-${dateFrom}-${dateTo}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return <section className="work-panel party-statements-panel print-active">
    <div className="work-panel-head"><div><h2>كشوف حساب العملاء والموردين</h2><p>الرصيد الافتتاحي والحركات المرحلة والرصيد المتحرك خلال الفترة</p></div><div className="report-actions"><span className="coming-soon">{busy ? "جارٍ التحميل…" : statement ? "كشف محدث" : "كشف تفصيلي"}</span>{statement && <><button type="button" className="subtle-button" onClick={downloadCsv}>تصدير CSV</button><button type="button" className="subtle-button" onClick={() => window.print()}>طباعة / PDF</button></>}</div></div>
    <div className="asset-report-filters">
      <label>نوع الحساب<select value={partyType} onChange={(event) => setPartyType(event.target.value as "customer" | "supplier")}><option value="customer">عميل</option><option value="supplier">مورد</option></select></label>
      <label>{partyType === "customer" ? "العميل" : "المورد"}<select value={partyId} onChange={(event) => { setPartyId(event.target.value); setStatement(null); }}><option value="">اختر {partyType === "customer" ? "العميل" : "المورد"}</option>{parties.map((party) => <option key={party.id} value={party.id}>{party.code} — {party.name_ar}</option>)}</select></label>
      <label>من تاريخ<input type="date" value={dateFrom} max={dateTo} onChange={(event) => setDateFrom(event.target.value)} /></label>
      <label>إلى تاريخ<input type="date" value={dateTo} min={dateFrom} max={today} onChange={(event) => setDateTo(event.target.value)} /></label>
      <button type="button" className="primary-button" disabled={busy || !companyId || !partyId || dateFrom > dateTo} onClick={() => void load()}>{busy ? "جارٍ التحميل…" : "عرض الكشف"}</button>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {statement && <>
      <div className="metrics-grid report-metrics">
        <article className="metric-card"><div className="metric-top">الطرف</div><strong className="metric-value">{partyName}</strong></article>
        <article className="metric-card"><div className="metric-top">رصيد أول المدة</div><strong className="metric-value">{money(statement.opening_balance, currency)}</strong></article>
        <article className="metric-card"><div className="metric-top">رصيد آخر المدة</div><strong className="metric-value">{money(statement.closing_balance, currency)}</strong></article>
      </div>
      <div className="work-panel-head"><div><h3>حركة الحساب</h3><p>{statement.date_from || dateFrom} إلى {statement.date_to || dateTo}</p></div></div>
      <div className="table-toolbar"><input aria-label="البحث في حركة كشف الحساب" placeholder="ابحث برقم المستند أو البيان أو التاريخ…" value={movementSearch} onChange={event=>setMovementSearch(event.target.value)}/><span className="module-count">{visibleRows.length} من {rows.length} حركة</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>النوع</th><th>رقم المستند</th><th>البيان</th><th>مدين</th><th>دائن</th><th>الرصيد المتحرك</th></tr></thead><tbody>
        {visibleRows.map((row, index) => <tr key={`${row.document_type}-${row.document_number}-${index}`}><td>{row.date}</td><td>{documentLabel(row.document_type)}</td><td className="code-cell">{row.document_number}</td><td>{row.description}</td><td>{money(row.debit, currency)}</td><td>{money(row.credit, currency)}</td><td>{money(row.balance, currency)}</td></tr>)}
      </tbody></table>{!rows.length && <div className="table-empty">لا توجد حركات للطرف خلال الفترة المحددة.</div>}{rows.length>0&&!visibleRows.length&&<div className="table-empty">لا توجد حركات مطابقة للبحث.</div>}</div>
    </>}
    {!statement && !busy && !error && <div className="table-empty">اختر عميلًا أو موردًا لعرض كشف حسابه.</div>}
  </section>;
}
