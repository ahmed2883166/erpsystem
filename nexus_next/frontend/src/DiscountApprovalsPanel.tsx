/*
 * موافقات الخصومات: ضبط سياسة حدود الخصم ومراجعة طلبات الموافقة على فواتير البيع.
 * تعرض حالة الطلب وتتيح الإجراءات وفق صلاحيات الاعتماد الممنوحة للمستخدم.
 */
import { useEffect, useState, type FormEvent } from "react";
import { api, approvalsApi, type DiscountApprovalPolicy, type SalesInvoiceApproval, type FinancialApprovalRequest } from "./api";

type Props = { companyId: number | null };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
const statusLabel: Record<SalesInvoiceApproval["status"], string> = {
  pending: "بانتظار الموافقة", approved: "تمت الموافقة", rejected: "مرفوض", superseded: "استبدل بطلب أحدث",
};

// شاشة سياسات الخصم وطلبات اعتماد الخصومات والموافقات المالية.
export default function DiscountApprovalsPanel({ companyId }: Props) {
  const [policy, setPolicy] = useState<DiscountApprovalPolicy | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [requests, setRequests] = useState<SalesInvoiceApproval[]>([]);
  const [limit, setLimit] = useState("");
  const [invoiceAmountLimit, setInvoiceAmountLimit] = useState("");
  const [purchaseAmountLimit, setPurchaseAmountLimit] = useState("");
  const [purchaseOrderAmountLimit, setPurchaseOrderAmountLimit] = useState("");
  const [paymentAmountLimit, setPaymentAmountLimit] = useState("");
  const [financialRequests, setFinancialRequests] = useState<FinancialApprovalRequest[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // تحميل سياسة الخصم الحالية وطلبات الموافقة وصلاحيات الإدارة.
const refresh = async () => {
    if (!companyId) { setPolicy(null); setRequests([]); return; }
    setBusy(true); setError("");
    try {
      const [membership, policies, rows, financialRows] = await Promise.all([
        api.currentMembership(companyId), approvalsApi.policies(companyId), approvalsApi.requests(companyId),
        approvalsApi.financialRequests(companyId),
      ]);
      setCanManage(membership.role === "admin");
      const current = policies[0] || null;
      setPolicy(current);
      if (current) {
        setLimit(current.max_discount_percent || "");
        setInvoiceAmountLimit(current.max_sales_invoice_amount || "");
        setPurchaseAmountLimit(current.max_purchase_invoice_amount || "");
        setPurchaseOrderAmountLimit(current.max_purchase_order_amount || "");
        setPaymentAmountLimit(current.max_payment_amount || "");
      } else {
        setLimit("");
        setInvoiceAmountLimit("");
        setPurchaseAmountLimit("");
        setPurchaseOrderAmountLimit("");
        setPaymentAmountLimit("");
      }
      setRequests(rows);
      setFinancialRequests(financialRows);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل إعدادات وطلبات الموافقة.");
    } finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  // حفظ حدود الخصم وإعدادات الموافقة الخاصة بالشركة.
const savePolicy = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = {
        max_discount_percent: limit.trim() || null,
        max_sales_invoice_amount: invoiceAmountLimit.trim() || null,
        max_purchase_invoice_amount: purchaseAmountLimit.trim() || null,
        max_purchase_order_amount: purchaseOrderAmountLimit.trim() || null,
        max_payment_amount: paymentAmountLimit.trim() || null,
      };
      if (policy) await approvalsApi.updatePolicy(policy.id, payload);
      else await approvalsApi.createPolicy({ company: companyId, ...payload });
      setNotice("تم حفظ حدود الاعتماد. تسري الحدود الجديدة على المستندات الجديدة.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ حد الخصم.");
    } finally { setBusy(false); }
  };

  // اعتماد أو رفض طلب الموافقة المالية المحدد.
const decideFinancial = async (row: FinancialApprovalRequest, decision: "approve" | "reject") => {
    let note = "";
    if (decision === "reject") {
      const entered = window.prompt("اكتب سبب رفض طلب الموافقة المالية:");
      if (entered === null) return;
      note = entered.trim();
      if (!note) { setError("سبب الرفض مطلوب."); return; }
    } else {
      note = window.prompt("ملاحظة الموافقة (اختياري):") || "";
    }
    setBusy(true); setError(""); setNotice("");
    try {
      if (decision === "approve") await approvalsApi.approveFinancial(row.id, note);
      else await approvalsApi.rejectFinancial(row.id, note);
      setNotice(decision === "approve" ? "تم اعتماد المستند المالي." : "تم رفض طلب الموافقة المالية.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تنفيذ قرار الموافقة.");
    } finally { setBusy(false); }
  };

  // اعتماد أو رفض طلب تجاوز حد الخصم على فاتورة البيع.
const decide = async (row: SalesInvoiceApproval, decision: "approve" | "reject") => {
    let note = "";
    if (decision === "reject") {
      const entered = window.prompt("اكتب سبب رفض طلب الموافقة:");
      if (entered === null) return;
      note = entered.trim();
      if (!note) { setError("سبب الرفض مطلوب."); return; }
    } else {
      const entered = window.prompt(
        row.requested_credit_limit !== null
          ? "اكتب سبب الاستثناء الائتماني ومبرر السماح بتجاوز الحد:"
          : "ملاحظة الموافقة (اختياري):",
      );
      if (entered === null) return;
      note = entered.trim();
      if (row.requested_credit_limit !== null && !note) {
        setError("سبب الاستثناء الائتماني مطلوب.");
        return;
      }
    }
    setBusy(true); setError(""); setNotice("");
    try {
      if (decision === "approve") await approvalsApi.approve(row.id, note);
      else await approvalsApi.reject(row.id, note);
      setNotice(decision === "approve" ? "تم اعتماد الطلب. أصبح بالإمكان ترحيل الفاتورة." : "تم رفض طلب الموافقة.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تنفيذ قرار الموافقة.");
    } finally { setBusy(false); }
  };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر الشركة لعرض الموافقات.</div></section>;
  const pending = requests.filter(row => row.status === "pending").length + financialRequests.filter(row => row.status === "pending").length;
  const financialDocumentLabel: Record<FinancialApprovalRequest["document_type"], string> = {
    purchase_invoice: "فاتورة مشتريات", purchase_order: "أمر شراء", supplier_payment: "سند صرف مورد", treasury_payment: "سند صرف خزينة",
    asset_purchase_invoice: "فاتورة شراء أصل ثابت", asset_supplier_payment: "سند سداد أصل ثابت",
  };

  return <section className="work-panel">
    <div className="work-panel-head">
      <div><h2>الموافقات الإدارية والحدود المالية</h2><p>الخصم أو الفواتير أو أوامر الشراء أو سندات الصرف أو تجاوز حد ائتمان العميل تتطلب اعتماد مدير الشركة قبل الترحيل.</p></div>
      <span className="coming-soon">{canManage ? "مدير الشركة" : "عرض فقط"} · طلبات معلقة: {pending.toLocaleString("ar-EG")}</span>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {!busy && !error && !policy && <div className="error-banner"><strong>تنبيه أمني:</strong> لا توجد سياسة حدود مالية لهذه الشركة؛ لذلك لن تُطلب موافقة مالية بناءً على حدود المشتريات أو سندات الصرف حتى يضبط مدير الشركة السياسة.</div>}
    {canManage && <form className="inventory-form" onSubmit={savePolicy}>
      <label>أقصى خصم دون موافقة (%)<input type="number" min="0" max="100" step="0.0001" value={limit} onChange={e => setLimit(e.target.value)} placeholder="اتركه فارغًا لتعطيل الحد" /></label>
      <label>أقصى إجمالي فاتورة مبيعات دون موافقة<input type="number" min="0" step="0.0001" value={invoiceAmountLimit} onChange={e => setInvoiceAmountLimit(e.target.value)} placeholder="اتركه فارغًا لإلغاء الحد" /></label>
      <label>أقصى إجمالي فاتورة مشتريات دون موافقة<input type="number" min="0" step="0.0001" value={purchaseAmountLimit} onChange={e => setPurchaseAmountLimit(e.target.value)} placeholder="اتركه فارغًا لإلغاء الحد" /></label>
      <label>أقصى إجمالي أمر شراء دون موافقة<input type="number" min="0" step="0.0001" value={purchaseOrderAmountLimit} onChange={e => setPurchaseOrderAmountLimit(e.target.value)} placeholder="اتركه فارغًا لإلغاء الحد" /></label>
      <label>أقصى قيمة سند صرف دون موافقة<input type="number" min="0" step="0.0001" value={paymentAmountLimit} onChange={e => setPaymentAmountLimit(e.target.value)} placeholder="اتركه فارغًا لإلغاء الحد" /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : policy ? "تحديث سياسة الموافقات" : "تفعيل سياسة الموافقات"}</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button></div>
      <p className="inventory-report-note inventory-wide">{policy ? "الحدود الحالية محفوظة للشركة. تغييرها لا يغيّر الحدود المسجلة على الفواتير السابقة." : "لم يتم إعداد سياسة موافقات لهذه الشركة بعد."} يمكن ضبط أي حد أو أكثر، ولا يمكن ترك جميع الحدود فارغة معًا. جميع الحدود النقدية بعملة الشركة الأساسية. إعداد السياسة وتعديلها متاحان لمدير الشركة فقط.</p>
    </form>}
    {!canManage && <p className="inventory-report-note">عرض طلبات الموافقة متاح حسب صلاحيات الشركة؛ تعديل السياسة واعتماد الطلبات متاحان لمدير الشركة فقط.</p>}
    <div className="data-table-wrap">
      <table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>التاريخ</th><th>الإجمالي</th><th>حد الخصم وقت الطلب</th><th>حد إجمالي الفاتورة وقت الطلب</th><th>حد ائتمان العميل وقت الطلب</th><th>التعرض الائتماني وقت الطلب</th><th>قيمة الفاتورة</th><th>إجمالي الخصم</th><th>تفاصيل النسخة محل المراجعة</th><th>مقدم الطلب</th><th>الحالة</th><th>القرار</th></tr></thead>
        <tbody>{requests.map(row => <tr key={row.id}>
          <td className="code-cell">{row.invoice_number}</td><td>{row.customer_name}</td><td>{row.invoice_date}</td>
          <td>{money(row.invoice_total)}</td><td>{row.requested_limit === null ? "غير محدد" : Number(row.requested_limit).toLocaleString("ar-EG", {maximumFractionDigits:4}) + "%"}</td>
          <td>{row.requested_invoice_limit === null ? "غير محدد" : money(row.requested_invoice_limit)}</td>
          <td>{row.requested_credit_limit === null ? "—" : money(row.requested_credit_limit)}</td>
          <td>{row.requested_credit_exposure === null ? "—" : money(row.requested_credit_exposure)}</td>
          <td>{row.requested_credit_amount === null ? "—" : money(row.requested_credit_amount)}</td>
          <td>{money(row.requested_discount_total)}</td>
          <td><details className="approval-snapshot"><summary>عرض {row.requested_lines_snapshot?.length || 0} صنف</summary>
            <div className="data-table-wrap"><table><thead><tr><th>الصنف</th><th>الكمية</th><th>سعر الوحدة</th><th>الخصم %</th><th>قيمة الخصم</th><th>كود الضريبة</th><th>الضريبة</th><th>الإجمالي</th></tr></thead>
              <tbody>{(row.requested_lines_snapshot || []).map(line => <tr key={line.delivery_line_id}>
                <td>{line.product_code} · {line.product_name}</td><td>{money(line.quantity)}</td><td>{money(line.unit_price)}</td>
                <td>{Number(line.discount_percent).toLocaleString("ar-EG", {maximumFractionDigits:4})}%</td><td>{money(line.discount_amount)}</td>
                <td>{line.tax_code || "—"} ({Number(line.tax_rate).toLocaleString("ar-EG", {maximumFractionDigits:4})}%)</td>
                <td>{money(line.tax_amount)}</td><td>{money(line.total_amount)}</td>
              </tr>)}
              {!row.requested_lines_snapshot?.length && <tr><td colSpan={8}>لا تتوفر لقطة تفصيلية لهذا الطلب القديم؛ يلزم إنشاء طلب مراجعة جديد.</td></tr>}</tbody>
            </table></div>
          </details></td>
          <td>{row.requested_by_name}</td><td>{statusLabel[row.status]}</td>
          <td>{row.status === "pending" ? canManage ? <div className="table-actions"><button type="button" className="primary-button" disabled={busy} onClick={() => void decide(row, "approve")}>اعتماد</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void decide(row, "reject")}>رفض</button></div> : "بانتظار قرار المدير" : <span>{row.decided_by_name || "—"}{row.decision_note ? ` · ${row.decision_note}` : ""}</span>}</td>
        </tr>)}
        {!requests.length && <tr><td colSpan={14}>لا توجد طلبات موافقة مالية لهذه الشركة.</td></tr>}</tbody>
      </table>
    </div>

    <section className="work-panel-subsection">
      <div className="work-panel-head"><div><h3>موافقات المشتريات والأصول وسندات الصرف</h3><p>تتطلب المستندات التي تتجاوز حدودها اعتماد مدير الشركة قبل الترحيل.</p></div></div>
      <div className="data-table-wrap">
        <table><thead><tr><th>نوع المستند</th><th>رقم المستند</th><th>المبلغ وقت الطلب</th><th>الحد وقت الطلب</th><th>مقدم الطلب</th><th>تاريخ الطلب</th><th>الحالة</th><th>تفاصيل النسخة</th><th>القرار</th></tr></thead>
          <tbody>{financialRequests.map(row => <tr key={row.id}>
            <td>{financialDocumentLabel[row.document_type]}</td><td className="code-cell">{row.document_number}</td>
            <td>{money(row.requested_amount)}</td><td>{money(row.requested_limit)}</td>
            <td>{row.requested_by_name}</td><td>{new Date(row.requested_at).toLocaleString("ar-EG")}</td>
            <td>{statusLabel[row.status]}</td>
            <td><details className="approval-snapshot"><summary>عرض التفاصيل</summary><pre>{JSON.stringify(row.requested_snapshot, null, 2)}</pre></details></td>
            <td>{row.status === "pending" ? canManage ? <div className="table-actions"><button type="button" className="primary-button" disabled={busy} onClick={() => void decideFinancial(row, "approve")}>اعتماد</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void decideFinancial(row, "reject")}>رفض</button></div> : "بانتظار قرار المدير" : <span>{row.decided_by_name || "—"}{row.decision_note ? ` · ${row.decision_note}` : ""}</span>}</td>
          </tr>)}
          {!financialRequests.length && <tr><td colSpan={9}>لا توجد طلبات موافقة مشتريات أو أصول أو صرف.</td></tr>}</tbody>
        </table>
      </div>
    </section>
    <p className="inventory-report-note">لا يمكن لمُنشئ طلب الموافقة اعتماده بنفسه. قرارات الموافقة والرفض نهائية ومحفوظة في سجل التدقيق، ولا يمكن حذف طلب الموافقة.</p>
  </section>;
}
