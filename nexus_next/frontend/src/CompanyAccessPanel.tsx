import { useEffect, useState, type FormEvent } from "react";
import { api, type Branch, type CompanyMembership, type CompanyUserLookup } from "./api";

type Props = { companyId: number | null };
const roleNames: Record<CompanyMembership["role"], string> = {
  admin: "مدير الشركة", accountant: "محاسب", viewer: "مشاهد",
};

export default function CompanyAccessPanel({ companyId }: Props) {
  const [rows, setRows] = useState<CompanyMembership[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [search, setSearch] = useState("");
  const [users, setUsers] = useState<CompanyUserLookup[]>([]);
  const [selectedUser, setSelectedUser] = useState("");
  const [role, setRole] = useState<CompanyMembership["role"]>("accountant");
  const [allBranches, setAllBranches] = useState(true);
  const [selectedBranches, setSelectedBranches] = useState<number[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [editRole, setEditRole] = useState<CompanyMembership["role"]>("viewer");
  const [editActive, setEditActive] = useState(true);
  const [editAllBranches, setEditAllBranches] = useState(false);
  const [editBranches, setEditBranches] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = async () => {
    if (!companyId) return;
    setBusy(true); setError("");
    try {
      const [membership, list, branchList] = await Promise.all([
        api.currentMembership(companyId), api.memberships(companyId), api.branches(companyId),
      ]);
      setCanManage(membership.role === "admin");
      setRows(list); setBranches(branchList);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحميل المستخدمين والصلاحيات.");
    } finally { setBusy(false); }
  };
  useEffect(() => {
    setRows([]); setUsers([]); setEditing(null); setError(""); setNotice("");
    void refresh();
  }, [companyId]);

  const findUsers = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || search.trim().length < 2) return;
    setBusy(true); setError("");
    try { setUsers(await api.searchCompanyUsers(companyId, search.trim())); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر البحث عن المستخدم."); }
    finally { setBusy(false); }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !selectedUser) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createMembership({
        company: companyId, user: Number(selectedUser), role, all_branches: allBranches,
        branches: allBranches ? [] : selectedBranches, is_active: true,
      });
      setSelectedUser(""); setUsers([]); setSearch("");
      setNotice("تمت إضافة المستخدم بالصلاحيات المحددة.");
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذرت إضافة المستخدم."); }
    finally { setBusy(false); }
  };

  const beginEdit = (row: CompanyMembership) => {
    setEditing(row.id); setEditRole(row.role); setEditActive(row.is_active);
    setEditAllBranches(row.all_branches); setEditBranches(row.branches);
  };
  const saveEdit = async (row: CompanyMembership) => {
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateMembership(row.id, {
        role: editRole, is_active: editActive, all_branches: editAllBranches,
        branches: editAllBranches ? [] : editBranches,
      });
      setEditing(null); setNotice("تم تحديث صلاحيات المستخدم.");
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحديث الصلاحيات."); }
    finally { setBusy(false); }
  };
  const toggle = (items: number[], id: number, setter: (value: number[]) => void) =>
    setter(items.includes(id) ? items.filter(x => x !== id) : [...items, id]);

  if (!companyId) return <section className="work-panel"><div className="table-empty">اختر الشركة لإدارة المستخدمين والصلاحيات.</div></section>;
  return <section className="work-panel">
    <div className="work-panel-head"><div><h2>إدارة المستخدمين والصلاحيات</h2><p>تحديد دور كل مستخدم ونطاق الفروع المسموح له بالوصول إليها.</p></div><span className="coming-soon">{canManage ? "مدير الشركة" : "عرض فقط"}</span></div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}
    {canManage && <>
      <form className="inventory-form" onSubmit={findUsers}>
        <label>البحث عن مستخدم لإضافته<input minLength={2} value={search} onChange={e => setSearch(e.target.value)} placeholder="اسم المستخدم أو الاسم الظاهر" /></label>
        <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || search.trim().length < 2}>بحث</button></div>
      </form>
      {users.length > 0 && <form className="inventory-form" onSubmit={create}>
        <label>المستخدم<select required value={selectedUser} onChange={e => setSelectedUser(e.target.value)}><option value="">اختر المستخدم</option>{users.map(user => <option key={user.id} value={user.id}>{user.display_name || user.username} · @{user.username}</option>)}</select></label>
        <label>الدور<select value={role} onChange={e => setRole(e.target.value as CompanyMembership["role"])}><option value="accountant">محاسب</option><option value="viewer">مشاهد</option><option value="admin">مدير الشركة</option></select></label>
        <label className="inventory-wide"><span><input type="checkbox" checked={allBranches} onChange={e => setAllBranches(e.target.checked)} /> السماح بالوصول إلى جميع فروع الشركة</span></label>
        {!allBranches && <div className="inventory-wide inventory-form-row">{branches.map(branch => <label key={branch.id}><span><input type="checkbox" checked={selectedBranches.includes(branch.id)} onChange={() => toggle(selectedBranches, branch.id, setSelectedBranches)} /> {branch.code} · {branch.name_ar}</span></label>)}</div>}
        <div className="inventory-form-actions"><button className="primary-button" type="submit" disabled={busy || !selectedUser}>إضافة المستخدم</button></div>
      </form>}
    </>}
    <div className="customer-toolbar"><div className="customer-count">المستخدمون المرتبطون بالشركة <strong>{rows.length.toLocaleString("ar-EG")}</strong></div><button type="button" className="subtle-button" disabled={busy} onClick={() => void refresh()}>تحديث القائمة</button></div>
    <div className="data-table-wrap"><table><thead><tr><th>اسم المستخدم</th><th>الدور</th><th>نطاق الفروع</th><th>الحالة</th><th>الإجراء</th></tr></thead><tbody>
      {rows.map(row => <tr key={row.id}>
        <td>{row.username}</td>
        <td>{editing === row.id ? <select value={editRole} onChange={e => setEditRole(e.target.value as CompanyMembership["role"])}><option value="admin">مدير الشركة</option><option value="accountant">محاسب</option><option value="viewer">مشاهد</option></select> : roleNames[row.role]}</td>
        <td>{editing === row.id ? <div className="access-branch-editor"><label><input type="checkbox" checked={editAllBranches} onChange={e => setEditAllBranches(e.target.checked)} /> كل الفروع</label>{!editAllBranches && branches.map(branch => <label key={branch.id}><input type="checkbox" checked={editBranches.includes(branch.id)} onChange={() => toggle(editBranches, branch.id, setEditBranches)} /> {branch.code}</label>)}</div> : row.all_branches ? "جميع الفروع" : branches.filter(b => row.branches.includes(b.id)).map(b => b.code).join("، ") || "لا توجد فروع محددة"}</td>
        <td>{editing === row.id ? <label><input type="checkbox" checked={editActive} onChange={e => setEditActive(e.target.checked)} /> نشط</label> : row.is_active ? "نشط" : "معطل"}</td>
        <td>{canManage && (editing === row.id ? <div className="table-actions"><button type="button" className="primary-button" disabled={busy} onClick={() => void saveEdit(row)}>حفظ</button><button type="button" className="subtle-button" onClick={() => setEditing(null)}>إلغاء</button></div> : <button type="button" className="subtle-button" disabled={busy} onClick={() => beginEdit(row)}>تعديل الصلاحيات</button>)}</td>
      </tr>)}
      {!rows.length && <tr><td colSpan={5}>لا توجد عضويات مسجلة لهذه الشركة.</td></tr>}
    </tbody></table></div>
    <p className="inventory-report-note">الأدوار: مدير الشركة لإدارة المستخدمين والإعدادات واعتماد الخصومات؛ المحاسب لتنفيذ العمليات المالية؛ المشاهد للقراءة فقط. لا يمكن تعطيل آخر مدير نشط للشركة، ولا يمكن منح المستخدم فروعًا تتبع شركة أخرى.</p>
  </section>;
}
