/*
 * سجل التدقيق: عرض أحداث المستخدمين مع التصفية بالتاريخ ونوع الإجراء.
 * هذه الشاشة للمتابعة والمراجعة ولا تنفذ تعديلات على السجلات المعروضة.
 */
import { useEffect, useState } from "react";
import { api, type AuditEvent } from "./api";

type AuditAction = "" | "create" | "update" | "delete" | "action";
const actionNames: Record<Exclude<AuditAction, "">, string> = {
  create: "إنشاء", update: "تعديل", delete: "حذف", action: "إجراء",
};

// شاشة سجل التدقيق مع فلاتر الفترة ونوع العملية وعرض تفاصيل الأحداث.
export default function AuditPanel({ companyId }: { companyId: number | null }) {
  const today = new Date().toISOString().slice(0, 10);
  const [from, setFrom] = useState(new Date().getFullYear() + "-01-01");
  const [to, setTo] = useState(today);
  const [action, setAction] = useState<AuditAction>("");
  const [items, setItems] = useState<AuditEvent[]>([]);
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const pageCount = Math.max(1, Math.ceil(count / pageSize));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // إعادة الصفحة الأولى عند الانتقال إلى شركة أخرى.
  useEffect(() => { setPage(1); }, [companyId]);

  // إعادة تحميل سجل التدقيق عند تغير الشركة أو فلاتر الفترة ونوع الإجراء.
useEffect(() => {
    let cancelled = false;
    if (!companyId || from > to) {
      setItems([]);
      setCount(0);
      setBusy(false);
      return;
    }
    setBusy(true);
    setError("");
    api.auditEvents(companyId, from, to, action, pageSize, (page - 1) * pageSize)
      .then(result => {
        if (!cancelled) {
          setItems(result.results);
          setCount(result.count);
        }
      })
      .catch(reason => {
        if (!cancelled) {
          setItems([]);
          setCount(0);
          setError(reason instanceof Error ? reason.message : "تعذر تحميل سجل المراجعة.");
        }
      })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [companyId, from, to, action, page]);

  // تنسيق وقت الحدث للعرض بالتنسيق المحلي العربي.
const dateTime = (value: string) => new Date(value).toLocaleString("ar-EG", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });

  // تحويل اسم نوع السجل البرمجي إلى تسمية مفهومة في واجهة المستخدم.
const resourceLabel = (resource: string) => {
    const approval = resource.match(/^approvals\/requests\/(\d+)\/(approve|reject)$/);
    if (approval) {
      return `طلبات الخصم · ${approval[2] === "approve" ? "اعتماد" : "رفض"} رقم ${approval[1]}`;
    }
    if (resource.startsWith("approvals/discount-policies")) return "إعدادات حدود الخصم";
    return resource;
  };

  return <section className="work-panel audit-panel">
    <div className="work-panel-head">
      <div>
        <h2>سجل المراجعة والتدقيق</h2>
        <p>تتبع محاولات الإنشاء والتعديل والحذف والإجراءات على بيانات الشركة، بما فيها المحاولات المرفوضة</p>
      </div>
      <span className="coming-soon">سجل للقراءة فقط</span>
    </div>
    {!companyId && <div className="empty-state">اختر الشركة لعرض سجل عملياتها.</div>}
    {companyId && <>
      <div className="asset-report-filters audit-filters">
        <label>من تاريخ<input type="date" value={from} max={to} onChange={event => { setFrom(event.target.value); setPage(1); }} /></label>
        <label>إلى تاريخ<input type="date" value={to} min={from} onChange={event => { setTo(event.target.value); setPage(1); }} /></label>
        <label>نوع العملية<select value={action} onChange={event => { setAction(event.target.value as AuditAction); setPage(1); }}>
          <option value="">كل العمليات</option>
          <option value="create">إنشاء</option><option value="update">تعديل</option>
          <option value="delete">حذف</option><option value="action">إجراء / ترحيل / اعتماد</option>
        </select></label>
        <div className="audit-count">{busy ? "جارٍ التحميل…" : count.toLocaleString("ar-EG") + " سجل إجمالي"}</div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      <div className="data-table-wrap">
        <table>
          <thead><tr><th>التاريخ والوقت</th><th>المستخدم</th><th>العملية</th><th>المسار</th><th>رقم السجل</th><th>الفرع</th><th>النتيجة</th><th>عنوان الشبكة</th></tr></thead>
          <tbody>
            {items.map(item => <tr key={item.id}>
              <td className="audit-time">{dateTime(item.created_at)}</td><td>{item.actor}</td>
              <td>{actionNames[item.action] || item.action}</td>
              <td title={item.resource}>{resourceLabel(item.resource)}</td><td>{item.object_id || "—"}</td>
              <td>{item.branch || "كل الشركة"}</td>
              <td><span className={item.succeeded ? "status-pill posted" : "status-pill reversed"}>{item.succeeded ? "نجاح " + item.status_code : "مرفوض " + item.status_code}</span></td>
              <td className="code-cell" dir="ltr">{item.ip_address || "—"}</td>
            </tr>)}
            {!busy && !items.length && !error && <tr><td colSpan={8}>لا توجد عمليات مسجلة في الفترة المحددة أو لا تتوفر صلاحية عرض السجل.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="table-pagination" aria-label="التنقل بين صفحات سجل التدقيق">
        <span>عرض {(count ? (page - 1) * pageSize + 1 : 0).toLocaleString("ar-EG")}–{Math.min(page * pageSize, count).toLocaleString("ar-EG")} من {count.toLocaleString("ar-EG")}</span>
        <div className="table-pagination-actions">
          <button type="button" className="subtle-button" disabled={busy || page <= 1} onClick={() => setPage(value => Math.max(1, value - 1))}>السابق</button>
          <span>صفحة {page.toLocaleString("ar-EG")} من {pageCount.toLocaleString("ar-EG")}</span>
          <button type="button" className="subtle-button" disabled={busy || page >= pageCount} onClick={() => setPage(value => Math.min(pageCount, value + 1))}>التالي</button>
        </div>
      </div>
      <p className="asset-report-note">لا يسجل النظام محتوى الطلبات أو كلمات المرور أو بيانات الفواتير داخل سجل التدقيق. السجلات للعرض فقط ولا يمكن تعديلها أو حذفها من واجهة النظام.</p>
    </>}
  </section>;
}
