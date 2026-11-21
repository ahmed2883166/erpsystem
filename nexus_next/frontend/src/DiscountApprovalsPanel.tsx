import { useEffect, useState, type FormEvent } from "react";
import { api, approvalsApi, type DiscountApprovalPolicy, type SalesInvoiceApproval } from "./api";

type Props = { companyId: number | null };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
const statusLabel: Record<SalesInvoiceApproval["status"], string> = {
  pending: "بانتظار الموافقة", approved: "تمت الموافقة", rejected: "مرفوض", superseded: "استبدل بطلب أحدث",
};

export default function DiscountApprovalsPanel({ companyId }: Props) {
  const [policy, setPolicy] = useState<DiscountApprovalPolicy | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [requests, setRequests] = useState<SalesInvoiceApproval[]>([]);
  const [limit, setLimit] = useState("");
  const [invoiceAmountLimit, setInvoiceAmountLimit] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = async () => {
    if (!companyId) { setPolicy(null); setRequests([]); return; }
    setBusy(true); setError("");
    try {
      const [membership, policies, rows] = await Promise.all([
        api.currentMembership(companyId), approvalsApi.policies(companyId), approvalsApi.requests(companyId),
      ]);
      setCanManage(membership.role === "admin");
      const current = policies[0] || null;
      setPolicy(current);
      if (current) {
        setLimit(current.max_discount_percent || "");
        setInvoiceAmountLimit(current.max_sales_invoice_amount || "");
      } else {
        setLimit("");
        setInvoiceAmountLimit("");
      }
      setRequests(rows);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل إعدادات وطلبات الموافقة.");
    } finally { setBusy(false); }
  };
  useEffect(() => { void refresh(); }, [companyId]);

  const savePolicy = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = {
        max_discount_percent: limit.trim() || null,
        max_sales_invoice_amount: invoiceAmountLimit.trim() || null,
      };
      if (policy) await approvalsApi.updatePolicy(policy.id, payload);
      else await approvalsApi.createPolicy({ company: companyId, ...payload });
      setNotice("تم حفظ حدود الاعتماد. تسري الحدود الجديدة على الفواتير الجديدة.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ حد الخصم.");
    } finally { setBusy(false); }
  };

  const decide = async (row: SalesInvoiceApproval, decision: "approve" | "reject") => {
    let note = "";
    if (decision === "reject") {
      const entered = window.prompt("اكتب سبب رفض طلب الموافقة:");
      if (entered === null) return;
      note = entered.trim();
      if (!note) { setError("سبب الرفض مطلوب."); return; }
    } else {
      note = window.prompt("ملاحظة الموافقة (اختياري):") || "";
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
  const pending = requests.filter(row => row.status === "pending").length;

  return <section className="work-panel">
    <div className="work-panel-head">
      <div><h2>الموافقات الإدارية والحدود المالية</h2><p>الخصم الأعلى من الحد أو إجمالي الفاتورة الذي يتجاوز السقف المحدد يتطلب اعتماد مدير الشركة قبل الترحيل.</p></div>
      <span className="coming-soon">{canManage ? "مدير الشركة" : "عرض فقط"} · طلبات معلقة: {pending.toLocaleString("ar-EG")}</span>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {canManage && <form className="inventory-form" onSubmit={savePolicy}>
      <label>أقصى خصم دون موافقة (%)<input type="number" min="0" max="100" step="0.0001" value={limit} onChange={e => setLimit(e.target.value)} placeholder="اتركه فارغًا لتعطيل الحد" /></label>
      <label>أقصى إجمالي فاتورة دون موافقة (عملة الشركة الأساسية)<input type="number" min="0" step="0.0001" value={invoiceAmountLimit} onChange={e => setInvoiceAmountLimit(e.target.value)} placeholder="اتركه فارغًا لإلغاء الحد" /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : policy ? "تحديث سياسة الموافقات" : "تفعيل سياسة الموافقات"}</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button></div>
      <p className="inventory-report-note inventory-wide">{policy ? "الحدود الحالية محفوظة للشركة. تغييرها لا يغيّر الحدود المسجلة على الفواتير السابقة." : "لم يتم إعداد سياسة موافقات لهذه الشركة بعد."} يمكن ضبط حد واحد أو الحدين معًا، ولا يمكن تركهما فارغين معًا. يجب تحديد حد واحد على الأقل. إعداد السياسة وتعديلها متاحان لمدير الشركة فقط.</p>
    </form>}
    {!canManage && <p className="inventory-report-note">عرض طلبات الموافقة متاح حسب صلاحيات الشركة؛ تعديل السياسة واعتماد الطلبات متاحان لمدير الشركة فقط.</p>}
    <div className="data-table-wrap">
      <table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>التاريخ</th><th>الإجمالي</th><th>حد الخصم وقت الطلب</th><th>حد إجمالي الفاتورة وقت الطلب</th><th>إجمالي الخصم</th><th>تفاصيل النسخة محل المراجعة</th><th>مقدم الطلب</th><th>الحالة</th><th>القرار</th></tr></thead>
        <tbody>{requests.map(row => <tr key={row.id}>
          <td className="code-cell">{row.invoice_number}</td><td>{row.customer_name}</td><td>{row.invoice_date}</td>
          <td>{money(row.invoice_total)}</td><td>{row.requested_limit === null ? "غير محدد" : Number(row.requested_limit).toLocaleString("ar-EG", {maximumFractionDigits:4}) + "%"}</td>
          <td>{row.requested_invoice_limit === null ? "غير محدد" : money(row.requested_invoice_limit)}</td>
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
        {!requests.length && <tr><td colSpan={11}>لا توجد طلبات موافقة مالية لهذه الشركة.</td></tr>}</tbody>
      </table>
    </div>
    <p className="inventory-report-note">لا يمكن لمُنشئ طلب الموافقة اعتماده بنفسه. قرارات الموافقة والرفض نهائية ومحفوظة في سجل التدقيق، ولا يمكن حذف طلب الموافقة.</p>
  </section>;
}
