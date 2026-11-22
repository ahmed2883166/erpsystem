import { useEffect, useMemo, useState, type FormEvent } from "react";
import { api, type CostCenter } from "./api";

type Props = { companyId: number | null };

export default function CostCentersPanel({ companyId }: Props) {
  const [items, setItems] = useState<CostCenter[]>([]);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    if (!companyId) { setItems([]); return; }
    try { setItems(await api.costCenters(companyId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحميل مراكز التكلفة."); }
  };

  useEffect(() => {
    setItems([]); setError(""); setNotice(""); resetForm();
    void load();
  }, [companyId]);

  const resetForm = () => { setCode(""); setName(""); setParent(""); setEditingId(null); };
  const parentOptions = useMemo(() => items.filter(item => item.is_active && item.id !== editingId), [items, editingId]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !code.trim() || !name.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = { company: companyId, code: code.trim().toUpperCase(), name_ar: name.trim(), parent: parent ? Number(parent) : null };
      if (editingId) await api.updateCostCenter(editingId, payload);
      else await api.createCostCenter(payload);
      resetForm();
      await load();
      setNotice(editingId ? "تم تحديث مركز التكلفة." : "تم إنشاء مركز التكلفة.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ مركز التكلفة."); }
    finally { setBusy(false); }
  };

  const edit = (item: CostCenter) => {
    setEditingId(item.id); setCode(item.code); setName(item.name_ar); setParent(item.parent ? String(item.parent) : "");
    setError(""); setNotice("");
  };

  const toggle = async (item: CostCenter) => {
    const action = item.is_active ? "إيقاف" : "إعادة تفعيل";
    if (item.is_active && !window.confirm("إيقاف مركز التكلفة؟ ستظل الحركات التاريخية المرتبطة به محفوظة.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateCostCenter(item.id, { is_active: !item.is_active });
      await load();
      setNotice(`تم ${action} مركز التكلفة مع الاحتفاظ بسجلاته.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة المركز."); }
    finally { setBusy(false); }
  };

  const byId = new Map(items.map(item => [item.id, item]));
  const pathOf = (item: CostCenter) => {
    const parts = [item.name_ar]; let parentId = item.parent; let guard = 0;
    while (parentId && guard++ < 30) { const p = byId.get(parentId); if (!p) break; parts.unshift(p.name_ar); parentId = p.parent; }
    return parts.join(" / ");
  };

  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>مراكز التكلفة</h2><p>تعريف هيكل مراكز التكلفة وربطه بقيود اليومية لتحليل المصروفات والإيرادات حسب الإدارة أو المشروع أو الفرع.</p></div><span className="module-count">{items.length} مركز</span></div>
    {!companyId && <div className="empty-state">اختر الشركة أولًا لإدارة مراكز التكلفة.</div>}
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {companyId && <>
      <form className="journal-form" onSubmit={submit}>
        <label>كود المركز<input required maxLength={32} value={code} onChange={e => setCode(e.target.value)} placeholder="CC-001" /></label>
        <label>اسم مركز التكلفة<input required maxLength={160} value={name} onChange={e => setName(e.target.value)} placeholder="الإدارة العامة" /></label>
        <label>المركز الأب<select value={parent} onChange={e => setParent(e.target.value)}><option value="">مركز رئيسي</option>{parentOptions.map(item => <option key={item.id} value={item.id}>{item.code} · {item.name_ar}</option>)}</select></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || !code.trim() || !name.trim()}>{busy ? "جارٍ الحفظ…" : editingId ? "حفظ التعديلات" : "إضافة مركز تكلفة"}</button>{editingId !== null && <button type="button" className="subtle-button" onClick={resetForm} disabled={busy}>إلغاء التعديل</button>}</div>
      </form>
      <div className="section-heading"><div><h3>هيكل مراكز التكلفة</h3><p>يمكن إيقاف المركز بدل حذفه حتى لا تتأثر الحركات السابقة.</p></div><button type="button" className="subtle-button" onClick={() => void load()} disabled={busy}>تحديث</button></div>
      <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم المركز والمسار</th><th>المركز الأب</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
        {items.map(item => <tr key={item.id}><td>{item.code}</td><td>{pathOf(item)}</td><td>{item.parent ? byId.get(item.parent)?.name_ar || "—" : "رئيسي"}</td><td><span className={item.is_active ? "status-pill status-posted" : "status-pill"}>{item.is_active ? "نشط" : "موقوف"}</span></td><td><div className="table-actions"><button type="button" className="text-action-button" onClick={() => edit(item)}>تعديل</button><button type="button" className="text-action-button" disabled={busy} onClick={() => void toggle(item)}>{item.is_active ? "إيقاف" : "تفعيل"}</button></div></td></tr>)}
        {!items.length && <tr><td colSpan={5} className="table-empty">لا توجد مراكز تكلفة مسجلة لهذه الشركة.</td></tr>}
      </tbody></table></div>
    </>}
  </section>;
}
