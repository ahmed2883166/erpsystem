import { useEffect, useMemo, useRef, useState } from "react";
import { bankReconciliationApi, stockApi, treasuryApi, type Branch, type BankReconciliation, type BankReconciliationSummary, type InventoryReconciliation, type InventoryValuationIntegrity, type TreasuryAccount } from "./api";

type Props = { companyId: number | null; currency: string; branches: Branch[] };
const money = (value: string | number | null | undefined, currency: string) => value == null ? "—" : `${Number(value).toLocaleString("ar-EG", { maximumFractionDigits: 2 })} ${currency}`.trim();

export default function OperationalReconciliationPanel({ companyId, currency, branches }: Props) {
  const [branchId, setBranchId] = useState("");
  const [inventory, setInventory] = useState<InventoryReconciliation | null>(null);
  const [integrity, setIntegrity] = useState<InventoryValuationIntegrity | null>(null);
  const [bankAccounts, setBankAccounts] = useState<TreasuryAccount[]>([]);
  const [reconciliations, setReconciliations] = useState<BankReconciliation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [summary, setSummary] = useState<BankReconciliationSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  const selectedBranch = branches.find((item) => item.id === Number(branchId));
  const visibleReconciliations = useMemo(() => {
    if (!branchId) return reconciliations;
    const ids = new Set(bankAccounts.filter((item) => item.branch === Number(branchId)).map((item) => item.id));
    return reconciliations.filter((item) => ids.has(item.bank_account));
  }, [reconciliations, bankAccounts, branchId]);

  useEffect(() => {
    if (!companyId) { setInventory(null); setIntegrity(null); setBankAccounts([]); setReconciliations([]); setSummary(null); setSelectedId(""); return; }
    let active = true;
    const id = ++sequence.current;
    setBusy(true); setError(""); setSummary(null); setSelectedId("");
    Promise.all([
      stockApi.reconciliation(companyId, branchId ? Number(branchId) : undefined),
      stockApi.valuationIntegrity(companyId, branchId ? Number(branchId) : undefined),
      treasuryApi.accounts(companyId),
      bankReconciliationApi.list(companyId),
    ]).then(([stock, checks, accounts, bankRows]) => {
      if (!active || id !== sequence.current) return;
      setInventory(stock); setIntegrity(checks);
      setBankAccounts(accounts.filter((item) => item.kind === "bank"));
      setReconciliations(bankRows);
    }).catch((reason: Error) => {
      if (active && id === sequence.current) setError(reason.message || "تعذر تحميل بيانات المطابقات.");
    }).finally(() => { if (active && id === sequence.current) setBusy(false); });
    return () => { active = false; sequence.current += 1; };
  }, [companyId, branchId]);

  useEffect(() => { setSelectedId(""); setSummary(null); }, [companyId, branchId]);

  useEffect(() => {
    if (!selectedId) { setSummary(null); return; }
    let active = true;
    const id = ++sequence.current;
    setBusy(true); setError("");
    bankReconciliationApi.summary(Number(selectedId)).then((data) => {
      if (active && id === sequence.current) setSummary(data);
    }).catch((reason: Error) => {
      if (active && id === sequence.current) setError(reason.message || "تعذر تحميل ملخص التسوية البنكية.");
    }).finally(() => { if (active && id === sequence.current) setBusy(false); });
    return () => { active = false; sequence.current += 1; };
  }, [selectedId]);

  const accountName = (id: number) => bankAccounts.find((item) => item.id === id)?.name_ar || "حساب بنكي";
  const exportCsv = () => {
    const rows: (string | number)[][] = [["التقرير", "البند", "القيمة"]];
    if (inventory) {
      rows.push(["مطابقة المخزون", "قيمة المخزون", inventory.stock_value], ["مطابقة المخزون", "رصيد دفتر الأستاذ", inventory.gl_balance], ["مطابقة المخزون", "الفرق", inventory.difference]);
    }
    integrity?.fifo_mismatches.forEach((item) => rows.push(["اختلاف FIFO", item.product_code + " - " + item.product_name, item.value_difference]));
    integrity?.pending_movements.forEach((item) => rows.push(["حركة غير مرحلة", item.movement_number, item.movement_date]));
    if (summary) {
      rows.push(["تسوية بنكية", "رقم الكشف", summary.reconciliation.statement_number], ["تسوية بنكية", "فرق غير مفسر", summary.unexplained_difference]);
      summary.lines.filter((line) => !line.matched_transaction).forEach((line) => rows.push(["سطر بنك غير مطابق", line.description || line.reference, line.amount]));
    }
    const csv = "\\uFEFF" + rows.map((row) => row.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\\r\\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url; link.download = "operational-reconciliations.csv"; link.click(); URL.revokeObjectURL(url);
  };

  return <section className="work-panel print-active">
    <div className="work-panel-head"><div><h2>مطابقات المخزون والبنوك</h2><p>مراجعة الفروق والحركات المعلقة — دون ترحيل أو تعديل</p></div><div className="report-actions"><button type="button" className="subtle-button" onClick={exportCsv} disabled={!inventory && !integrity && !summary}>تصدير CSV</button><button type="button" className="subtle-button" onClick={() => window.print()}>طباعة / PDF</button><span className="coming-soon">{busy ? "جارٍ التحديث…" : "قراءة فقط"}</span></div></div>
    <div className="asset-report-filters"><label>الفرع<select value={branchId} onChange={(event) => setBranchId(event.target.value)}><option value="">كل الفروع المسموح بها</option>{branches.map((item) => <option key={item.id} value={item.id}>{item.name_ar} ({item.code})</option>)}</select></label><span>{selectedBranch ? selectedBranch.name_ar : "النطاق المجمع حسب صلاحيات المستخدم"}</span></div>
    {error && <div className="error-banner">{error}</div>}
    <div className="activity-panel">
      <div className="activity-title"><div><h3>مطابقة قيمة المخزون مع دفتر الأستاذ</h3><p>لقطة أرصدة المخزون الحالية مقابل رصيد حساب المخزون المرحّل</p></div><span className={inventory?.is_reconciled ? "status-pill posted" : "status-pill draft"}>{inventory ? (inventory.is_reconciled ? "متطابق" : "يوجد فرق") : "—"}</span></div>
      {inventory ? <>
        <div className="metrics-grid report-metrics">
          <article className="metric-card"><div className="metric-top">قيمة المخزون</div><strong className="metric-value">{money(inventory.stock_value, inventory.currency || currency)}</strong></article>
          <article className="metric-card"><div className="metric-top">رصيد دفتر الأستاذ</div><strong className="metric-value">{money(inventory.gl_balance, inventory.currency || currency)}</strong></article>
          <article className="metric-card"><div className="metric-top">الفرق</div><strong className="metric-value">{money(inventory.difference, inventory.currency || currency)}</strong></article>
        </div><p className="asset-report-note">{inventory.account_code} — {inventory.account_name}. حد التقريب: {inventory.rounding_tolerance}. {inventory.scope_note}</p>
      </> : <div className="table-empty">لا توجد بيانات مطابقة محملة.</div>}
    </div>
    <div className="activity-panel">
      <div className="activity-title"><div><h3>سلامة تقييم المخزون والحركات المعلقة</h3><p>اختلافات طبقات FIFO والحركات التي لم تُرحّل</p></div></div>
      {integrity ? <div className="metrics-grid report-metrics">
        <article className="metric-card"><div className="metric-top">اختلافات طبقات التكلفة</div><strong className="metric-value">{integrity.fifo_mismatch_count.toLocaleString("ar-EG")}</strong></article>
        <article className="metric-card"><div className="metric-top">حركات غير مرحلة</div><strong className="metric-value">{integrity.pending_movements_count.toLocaleString("ar-EG")}</strong></article>
      </div> : <div className="table-empty">لا توجد بيانات فحص محملة.</div>}
      {!!integrity?.fifo_mismatches.length && <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>المخزن</th><th>كمية الرصيد</th><th>كمية الطبقات</th><th>فرق القيمة</th></tr></thead><tbody>{integrity.fifo_mismatches.slice(0, 20).map((row, index) => <tr key={`${row.product}-${row.warehouse}-${index}`}><td>{row.product_code} — {row.product_name}</td><td>{row.warehouse_name}</td><td>{row.balance_quantity}</td><td>{row.layer_quantity}</td><td>{money(row.value_difference, currency)}</td></tr>)}</tbody></table>{integrity.fifo_mismatch_count > 20 && <p className="asset-report-note">يُعرض أول 20 اختلافًا من إجمالي {integrity.fifo_mismatch_count}.</p>}</div>}
      {!!integrity?.pending_movements.length && <div className="data-table-wrap"><table><thead><tr><th>رقم الحركة</th><th>التاريخ</th><th>النوع</th><th>من مخزن</th><th>إلى مخزن</th><th>السطور</th></tr></thead><tbody>{integrity.pending_movements.map((row) => <tr key={row.id}><td className="code-cell">{row.movement_number}</td><td>{row.movement_date}</td><td>{row.movement_type}</td><td>{row.source_warehouse || "—"}</td><td>{row.destination_warehouse || "—"}</td><td>{row.lines_count}</td></tr>)}</tbody></table>{integrity.pending_movements_truncated && <p className="asset-report-note">القائمة مختصرة وتوجد حركات معلقة إضافية.</p>}</div>}
      {integrity && !integrity.fifo_mismatch_count && !integrity.pending_movements_count && <div className="table-empty">لا توجد اختلافات FIFO أو حركات معلقة في النطاق المحدد.</div>}
    </div>
    <div className="activity-panel">
      <div className="activity-title"><div><h3>ملخص التسويات البنكية</h3><p>اختر تسوية لمراجعة كشف البنك والدفتر والسطور غير المطابقة</p></div></div>
      <div className="asset-report-filters"><label>التسوية البنكية<select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}><option value="">اختر تسوية</option>{visibleReconciliations.map((item) => <option key={item.id} value={item.id}>{accountName(item.bank_account)} — {item.statement_number} — {item.statement_date}</option>)}</select></label><span>{visibleReconciliations.length} تسوية</span></div>
      {summary && <>
        <div className="metrics-grid report-metrics">
          <article className="metric-card"><div className="metric-top">رصيد كشف البنك</div><strong className="metric-value">{money(summary.reconciliation.closing_balance, currency)}</strong></article>
          <article className="metric-card"><div className="metric-top">رصيد الدفاتر</div><strong className="metric-value">{money(summary.book_balance, currency)}</strong></article>
          <article className="metric-card"><div className="metric-top">الفرق غير المفسر</div><strong className="metric-value">{money(summary.unexplained_difference, currency)}</strong></article>
          <article className="metric-card"><div className="metric-top">سطور غير مطابقة</div><strong className="metric-value">{summary.unmatched_lines.toLocaleString("ar-EG")}</strong></article>
        </div>
        <div className="data-table-wrap"><table><thead><tr><th>البيان</th><th>التاريخ</th><th>المبلغ</th><th>الحالة</th></tr></thead><tbody>{summary.lines.filter((line) => !line.matched_transaction).map((line) => <tr key={line.id}><td>{line.description || line.reference || "سطر كشف بنك"}</td><td>{line.transaction_date}</td><td>{money(line.amount, currency)}</td><td><span className="status-pill draft">غير مطابق</span></td></tr>)}{!summary.lines.some((line) => !line.matched_transaction) && <tr><td colSpan={4}>جميع سطور كشف البنك مرتبطة بحركات.</td></tr>}</tbody></table></div>
      </>}
      {!visibleReconciliations.length && <div className="table-empty">لا توجد تسويات بنكية مسجلة في النطاق المحدد.</div>}
    </div>
    <p className="asset-report-note">هذه الشاشة للمراجعة فقط؛ لا تعتمد التسويات ولا ترحّل الحركات ولا تغيّر الأرصدة.</p>
  </section>;
}
