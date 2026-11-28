import { useEffect, useMemo, useState } from "react";

type ExportField = { key: string; label: string };
type ExportPreview = { headers: string[]; total_rows: number; preview_rows: unknown[][] };

type Props = {
  resource: "inventory" | "customers" | "suppliers" | "bank" | "employees";
  companyId: number | null;
  branchId?: number | null;
};

const resourceNames: Record<Props["resource"], string> = {
  inventory: "المخزون",
  customers: "العملاء",
  suppliers: "الموردون",
  bank: "البنك والخزائن",
  employees: "الموظفون",
};

export default function CustomExcelExportButton({ resource, companyId, branchId }: Props) {
  const [open, setOpen] = useState(false);
  const [fields, setFields] = useState<ExportField[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const templateKey = `admrail-export-template:${resource}`;
  const selectedFields = useMemo(() => fields.filter(field => selected.includes(field.key)), [fields, selected]);

  useEffect(() => {
    if (!open || !companyId) return;
    let cancelled = false;
    setBusy(true);
    setError("");
    const query = new URLSearchParams({ resource, company: String(companyId), mode: "fields" });
    fetch(`/api/exports/custom-excel/?${query}`, { credentials: "same-origin" })
      .then(async response => {
        if (!response.ok) throw new Error(response.status === 403 ? "ليس لديك صلاحية التصدير." : "تعذر تحميل أعمدة التصدير.");
        return response.json();
      })
      .then(data => {
        if (cancelled) return;
        const available = data.fields as ExportField[];
        setFields(available);
        let saved: string[] = [];
        try { saved = JSON.parse(localStorage.getItem(templateKey) || "[]") as string[]; } catch { saved = []; }
        const validSaved = saved.filter(key => available.some(field => field.key === key));
        setSelected(validSaved.length ? validSaved : available.map(field => field.key));
      })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : "حدث خطأ غير متوقع."); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [open, companyId, resource, templateKey]);

  const makeQuery = (isPreview: boolean) => {
    if (!companyId) throw new Error("اختر الشركة أولاً.");
    if (!selected.length) throw new Error("اختر عمودًا واحدًا على الأقل.");
    const query = new URLSearchParams({ resource, company: String(companyId), fields: selected.join(",") });
    if (branchId) query.set("branch", String(branchId));
    if (isPreview) query.set("preview", "1");
    return query;
  };

  const runPreview = async () => {
    setBusy(true); setError(""); setPreview(null);
    try {
      const response = await fetch(`/api/exports/custom-excel/?${makeQuery(true)}`, { credentials: "same-origin" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "تعذر إنشاء المعاينة.");
      setPreview(data as ExportPreview);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر إنشاء المعاينة.");
    } finally { setBusy(false); }
  };

  const download = async () => {
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/exports/custom-excel/?${makeQuery(false)}`, { credentials: "same-origin" });
      if (!response.ok) {
        let message = "تعذر إنشاء ملف Excel.";
        try { const data = await response.json(); message = data.detail || message; } catch { /* no JSON body */ }
        throw new Error(message);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${resource}-export.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تنزيل الملف.");
    } finally { setBusy(false); }
  };

  const toggleField = (key: string) => {
    setSelected(current => current.includes(key) ? current.filter(item => item !== key) : [...current, key]);
    setPreview(null);
  };
  const moveField = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= selectedFields.length) return;
    const next = [...selectedFields.map(field => field.key)];
    [next[index], next[target]] = [next[target], next[index]];
    setSelected(next);
    setPreview(null);
  };
  const saveTemplate = () => {
    localStorage.setItem(templateKey, JSON.stringify(selected));
    setError("");
  };

  return <>
    <button type="button" className="subtle-button" disabled={!companyId} onClick={() => setOpen(true)}>
      تصدير Excel مخصص
    </button>
    {open && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setOpen(false); }}>
      <section className="journal-modal" role="dialog" aria-modal="true" aria-labelledby={`custom-export-title-${resource}`}>
        <div className="modal-heading">
          <div><h2 id={`custom-export-title-${resource}`}>تصدير ${resourceNames[resource]}</h2><p>اختر الأعمدة، رتّبها، ثم راجع عينة البيانات قبل تنزيل الملف.</p></div>
          <button type="button" className="subtle-button" onClick={() => setOpen(false)}>إغلاق</button>
        </div>
        {busy && <p className="table-empty">جارٍ تجهيز البيانات…</p>}
        {error && <div className="error-banner" role="alert">{error}</div>}
        <div className="custom-export-fields">
          {fields.map(field => <label key={field.key}>
            <input type="checkbox" checked={selected.includes(field.key)} onChange={() => toggleField(field.key)} />
            {field.label}
          </label>)}
        </div>
        <h3>ترتيب الأعمدة في الملف</h3>
        <ol className="custom-export-order">
          {selectedFields.map((field, index) => <li key={field.key}>
            <span>{field.label}</span>
            <button type="button" className="subtle-button" aria-label="تحريك لأعلى" disabled={index === 0} onClick={() => moveField(index, -1)}>↑</button>
            <button type="button" className="subtle-button" aria-label="تحريك لأسفل" disabled={index === selectedFields.length - 1} onClick={() => moveField(index, 1)}>↓</button>
          </li>)}
        </ol>
        <div className="table-actions">
          <button type="button" className="subtle-button" disabled={busy || !selected.length} onClick={saveTemplate}>حفظ قالب الأعمدة</button>
          <button type="button" className="subtle-button" disabled={busy || !selected.length} onClick={() => void runPreview()}>معاينة / اختبار</button>
          <button type="button" className="primary-button" disabled={busy || !preview || preview.headers.length !== selected.length} onClick={() => void download()}>تنزيل Excel</button>
        </div>
        {preview && <section className="custom-export-preview">
          <strong>المعاينة: {preview.total_rows.toLocaleString("ar-EG")} سجل — عرض أول {preview.preview_rows.length} سجلات</strong>
          <div className="data-table-wrap"><table><thead><tr>{preview.headers.map((header, index) => <th key={index}>{header}</th>)}</tr></thead>
            <tbody>{preview.preview_rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((value, cellIndex) => <td key={cellIndex}>{value == null ? "" : String(value)}</td>)}</tr>)}</tbody>
          </table></div>
          <small>تم إنشاء المعاينة دون تعديل أو حفظ أي بيانات في النظام. لن يتاح التنزيل إلا بعد نجاح المعاينة.</small>
        </section>}
      </section>
    </div>}
  </>;
}
