import { useEffect, useState, type FormEvent } from "react";
import { api, type FiscalYear, type FiscalYearClosingReview } from "./api";

type Props = { companyId: number | null; onStructureChanged: () => Promise<void> };
const currentYear = new Date().getFullYear();

export default function FiscalYearsPanel({ companyId, onStructureChanged }: Props) {
  const [years, setYears] = useState<FiscalYear[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [review, setReview] = useState<FiscalYearClosingReview | null>(null);
  const [name, setName] = useState(String(currentYear));
  const [startDate, setStartDate] = useState(`${currentYear}-01-01`);
  const [endDate, setEndDate] = useState(`${currentYear}-12-31`);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    if (!companyId) { setYears([]); return; }
    const rows = await api.fiscalYears(companyId);
    setYears(rows.filter(row => row.company === companyId));
  };

  useEffect(() => {
    setYears([]); setSelectedId(null); setReview(null); setEditingId(null);
    setError(""); setNotice("");
    if (companyId) void api.fiscalYears(companyId)
      .then(rows => setYears(rows.filter(row => row.company === companyId)))
      .catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل السنوات المالية."));
  }, [companyId]);

  const resetForm = () => {
    setEditingId(null); setName(String(currentYear));
    setStartDate(`${currentYear}-01-01`); setEndDate(`${currentYear}-12-31`);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !name.trim() || !startDate || !endDate || startDate > endDate) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = { name: name.trim(), start_date: startDate, end_date: endDate };
      if (editingId) await api.updateFiscalYear(editingId, payload);
      else await api.createFiscalYear({ company: companyId, ...payload });
      resetForm(); await load(); await onStructureChanged();
      setNotice(editingId ? "تم تحديث بيانات السنة المالية." : "تم إنشاء السنة المالية.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ السنة المالية."); }
    finally { setBusy(false); }
  };

  const inspect = async (year: FiscalYear) => {
    setSelectedId(year.id); setReview(null); setError(""); setNotice("");
    try { setReview(await api.fiscalYearClosingReview(year.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إجراء مراجعة الإقفال."); }
  };

  const beginEdit = (year: FiscalYear) => {
    setEditingId(year.id); setName(year.name); setStartDate(year.start_date); setEndDate(year.end_date);
    setSelectedId(year.id); setReview(null); setError(""); setNotice("");
  };

  const closeYear = async () => {
    if (!selectedId || !review?.can_close) return;
    if (!window.confirm("تأكيد إقفال السنة المالية؟ لن يمكن ترحيل قيود جديدة عليها بعد الإقفال.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.closeFiscalYear(selectedId);
      await load(); await onStructureChanged(); setReview(result.review); setNotice(result.detail);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إقفال السنة المالية."); }
    finally { setBusy(false); }
  };

  const checks = [
    { key: "no_drafts", label: "لا توجد قيود مسودة غير معتمدة", passed: review?.checks.no_drafts, detail: review ? `المسودات: ${review.draft_entries}` : "" },
    { key: "balanced_entries", label: "كل القيود المرحلة والمعكوسة متوازنة", passed: review?.checks.balanced_entries, detail: review ? `قيود غير متوازنة: ${review.unbalanced_entries}` : "" },
    { key: "closing_entry", label: "قيد الإقفال مرحّل داخل السنة", passed: review?.checks.closing_entry, detail: review?.closing_entry_posted ? "قيد الإقفال موجود" : "يلزم إنشاء وترحيل قيد إقفال" },
    { key: "next_year_opening", label: "قيد افتتاح السنة التالية مرحّل", passed: review?.checks.next_year_opening, detail: review?.opening_entry_required ? (review.opening_entry_posted ? "قيد الافتتاح موجود" : "يلزم قيد افتتاح في أول يوم للسنة التالية") : "لا توجد سنة مالية تالية مسجلة بعد" },
  ];

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>إدارة السنوات المالية والإقفال</h2><p>إدارة الفترات ومراجعة القيود قبل الإقفال. لا يتم إنشاء قيود افتتاح أو إقفال تلقائيًا ولا تغيير الأرصدة من هذه الشاشة.</p></div><span className="module-count">{years.length} سنة</span></div>
    {!companyId && <div className="empty-state">اختر الشركة أولًا لإدارة سنواتها المالية.</div>}
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {companyId && <>
      <form className="journal-form" onSubmit={submit}>
        <h3 className="form-wide">{editingId ? "تعديل سنة مالية" : "إضافة سنة مالية"}</h3>
        <label>اسم السنة المالية<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label>
        <label>تاريخ البداية<input required type="date" value={startDate} onChange={e => setStartDate(e.target.value)} /></label>
        <label>تاريخ النهاية<input required type="date" min={startDate} value={endDate} onChange={e => setEndDate(e.target.value)} /></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || !name.trim() || startDate > endDate}>{busy ? "جارٍ الحفظ…" : editingId ? "حفظ التعديلات" : "إنشاء السنة المالية"}</button>{editingId !== null && <button type="button" className="subtle-button" onClick={resetForm} disabled={busy}>إلغاء التعديل</button>}</div>
        {editingId !== null && <p className="asset-report-note form-wide">يسمح بتعديل الاسم والتواريخ فقط قبل تسجيل أي قيود على السنة. حالة السنة لا تتغير من نموذج التعديل.</p>}
      </form>
      <div className="section-heading"><div><h3>الفترات المسجلة</h3><p>اختر سنة لمراجعة جاهزيتها للإقفال أو تعديل بياناتها إذا لم تُستخدم بعد.</p></div><button type="button" className="subtle-button" onClick={() => void load()} disabled={busy}>تحديث</button></div>
      <div className="data-table-wrap"><table><thead><tr><th>السنة</th><th>من</th><th>إلى</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
        {years.map(year => <tr key={year.id}><td>{year.name}</td><td>{year.start_date}</td><td>{year.end_date}</td><td><span className={year.status === "open" ? "status-pill status-posted" : "status-pill"}>{year.status === "open" ? "مفتوحة" : "مغلقة"}</span></td><td><div className="table-actions"><button type="button" className="text-action-button" onClick={() => void inspect(year)}>مراجعة الإقفال</button>{year.status === "open" && <button type="button" className="text-action-button" onClick={() => beginEdit(year)}>تعديل</button>}</div></td></tr>)}
        {!years.length && <tr><td colSpan={5} className="table-empty">لا توجد سنوات مالية لهذه الشركة.</td></tr>}
      </tbody></table></div>
      {selectedId !== null && <div className="journal-review">
        <div className="section-heading"><div><h3>قائمة مراجعة الإقفال</h3><p>{years.find(year => year.id === selectedId)?.name || "السنة المحددة"}</p></div><button type="button" className="subtle-button" onClick={() => { setSelectedId(null); setReview(null); }}>إغلاق المراجعة</button></div>
        {!review && <div className="empty-state">جارٍ تحميل نتائج المراجعة…</div>}
        {review && <>
          <div className="data-table-wrap"><table><thead><tr><th>شرط الإقفال</th><th>النتيجة</th><th>التفاصيل</th></tr></thead><tbody>{checks.map(item => <tr key={item.key}><td>{item.label}</td><td><span className={item.passed ? "status-pill status-posted" : "status-pill"}>{item.passed ? "مكتمل" : "غير مكتمل"}</span></td><td>{item.detail}</td></tr>)}</tbody></table></div>
          <p className="asset-report-note">الإقفال محمي من التغيير المباشر: يجب ترحيل قيد إقفال متوازن أولًا، وترحيل قيد افتتاح في أول يوم من السنة التالية إذا كانت مسجلة. هذه الشاشة لا تنشئ القيود بدلًا من المحاسب.</p>
          {review.status === "open" && <div className="form-actions"><button type="button" className="primary-button" disabled={busy || !review.can_close} onClick={() => void closeYear()}>{busy ? "جارٍ الإقفال…" : "اعتماد إقفال السنة المالية"}</button></div>}
          {review.status === "closed" && <div className="success-banner">هذه السنة مغلقة بالفعل، ولا يمكن إعادة فتحها من هذه الشاشة.</div>}
        </>}
      </div>}
    </>}
  </section>;
}
