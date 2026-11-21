import { useEffect, useState, type FormEvent } from "react";
import { approvalsApi, type DiscountApprovalPolicy, type SalesInvoiceApproval } from "./api";

type Props = { companyId: number | null };
const money = (value: string) => Number(value || 0).toLocaleString("ar-EG", { maximumFractionDigits: 2 });
const statusLabel: Record<SalesInvoiceApproval["status"], string> = {
  pending: "بانتظار الموافقة", approved: "تمت الموافقة", rejected: "مرفوض",
};

export default function DiscountApprovalsPanel({ companyId }: Props) {
  const [policy, setPolicy] = useState<DiscountApprovalPolicy | null>(null);
  const [requests, setRequests] = useState<SalesInvoiceApproval[]>([]);
  const [limit, setLimit] = useState("0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = async () => {
    if (!companyId) { setPolicy(null); setRequests([]); return; }
    setBusy(true); setError("");
    try {
      const [policies, rows] = await Promise.all([
        approvalsApi.policies(companyId), approvalsApi.requests(companyId),
      ]);
      const current = policies[0] || null;
      setPolicy(current);
      if (current) setLimit(current.max_discount_percent);
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
      if (policy) await approvalsApi.updatePolicy(policy.id, { max_discount_percent: limit });
      else await approvalsApi.createPolicy({ company: companyId, max_discount_percent: limit });
      setNotice("تم حفظ حد الخصم. يسري الحد الجديد على الفواتير الجديدة.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ حد الخصم.");
    } finally { setBusy(false); }
  };

  const decide = async (row: SalesInvoiceApproval, decision: "approve" | "reject") => {
    let note = "";
    if (decision === "reject") {
      const entered = window.prompt("اكتب سبب رفض الخصم:");
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
      setNotice(decision === "approve" ? "تم اعتماد الخصم. أصبح بالإمكان ترحيل الفاتورة." : "تم رفض طلب الخصم.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تنفيذ قرار الموافقة.");
    } finally { setBusy(false); }
  };

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر الشركة لعرض الموافقات.</div></section>;
  const pending = requests.filter(row => row.status === "pending").length;

  return <section className="work-panel">
    <div className="work-panel-head">
      <div><h2>الموافقات الإدارية وحدود الخصم</h2><p>الخصم الأعلى من الحد يتطلب اعتمادًا مستقلًا من مدير الشركة قبل ترحيل الفاتورة.</p></div>
      <span className="coming-soon">طلبات معلقة: {pending.toLocaleString("ar-EG")}</span>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    <form className="inventory-form" onSubmit={savePolicy}>
      <label>أقصى خصم دون موافقة (%)<input required type="number" min="0" max="100" step="0.0001" value={limit} onChange={e => setLimit(e.target.value)} /></label>
      <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : policy ? "تحديث حد الخصم" : "تفعيل حد الخصم"}</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث</button></div>
      <p className="inventory-report-note inventory-wide">{policy ? "الحد الحالي محفوظ للشركة. تغيير الحد لا يغيّر حد الموافقة الذي سُجّل على الفواتير السابقة." : "لم يتم إعداد حد خصم لهذه الشركة بعد. قبل تفعيل السياسة، لا تُطلب موافقة إضافية على الخصم."} إعداد السياسة وتعديلها متاحان لمدير الشركة فقط.</p>
    </form>
    <div className="data-table-wrap">
      <table><thead><tr><th>رقم الفاتورة</th><th>العميل</th><th>التاريخ</th><th>الإجمالي</th><th>حد الخصم وقت الطلب</th><th>مقدم الطلب</th><th>الحالة</th><th>القرار</th></tr></thead>
        <tbody>{requests.map(row => <tr key={row.id}>
          <td className="code-cell">{row.invoice_number}</td><td>{row.customer_name}</td><td>{row.invoice_date}</td>
          <td>{money(row.invoice_total)}</td><td>{Number(row.requested_limit).toLocaleString("ar-EG", {maximumFractionDigits:4})}%</td>
          <td>{row.requested_by_name}</td><td>{statusLabel[row.status]}</td>
          <td>{row.status === "pending" ? <div className="table-actions"><button type="button" className="primary-button" disabled={busy} onClick={() => void decide(row, "approve")}>اعتماد</button><button type="button" className="subtle-button" disabled={busy} onClick={() => void decide(row, "reject")}>رفض</button></div> : <span>{row.decided_by_name || "—"}{row.decision_note ? ` · ${row.decision_note}` : ""}</span>}</td>
        </tr>)}
        {!requests.length && <tr><td colSpan={8}>لا توجد طلبات موافقة خصم لهذه الشركة.</td></tr>}</tbody>
      </table>
    </div>
    <p className="inventory-report-note">لا يمكن لمُنشئ طلب الخصم اعتماده بنفسه. قرارات الموافقة والرفض نهائية ومحفوظة في سجل التدقيق، ولا يمكن حذف طلب الموافقة.</p>
  </section>;
}
