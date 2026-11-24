import { useEffect, useState, type FormEvent } from "react";
import { api, type Company, type CompanyMembership, type CompanyUserLookup, type Currency } from "./api";
import AboutPanel from "./AboutPanel";

type Props = {
  companies: Company[];
  onCompaniesChanged: (companies: Company[]) => void;
  onLogout: () => Promise<void>;
};

const activities = [
  ["commercial", "تجاري"], ["service", "خدمي"], ["medical", "طبي"],
  ["restaurants", "مطاعم"], ["industrial", "صناعي"],
] as const;

export default function OwnerPortalPanel({ companies, onCompaniesChanged, onLogout }: Props) {
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [selectedId, setSelectedId] = useState<number | "">("");
  const [members, setMembers] = useState<CompanyMembership[]>([]);
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<CompanyUserLookup[]>([]);
  const [userId, setUserId] = useState<number | "">("");
  const [newAdminUsername, setNewAdminUsername] = useState("");
  const [newAdminName, setNewAdminName] = useState("");
  const [newAdminEmail, setNewAdminEmail] = useState("");
  const [newAdminPassword, setNewAdminPassword] = useState("");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [country, setCountry] = useState<"EG" | "SA">("EG");
  const [activity, setActivity] = useState<Company["activity"]>("commercial");
  const [currencyId, setCurrencyId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editNameAr, setEditNameAr] = useState("");
  const [editNameEn, setEditNameEn] = useState("");
  const [editTaxNumber, setEditTaxNumber] = useState("");
  const [editAddress, setEditAddress] = useState("");
  const [editPhone, setEditPhone] = useState("");
  const [editEmail, setEditEmail] = useState("");
  const [showAbout, setShowAbout] = useState(false);

  const selected = companies.find(item => item.id === selectedId) || null;
  const refreshCompanies = async () => {
    const rows = await api.companies();
    onCompaniesChanged(rows);
    if (selectedId && !rows.some(row => row.id === selectedId)) setSelectedId("");
  };
  const refreshMembers = async (companyId: number) => {
    setMembers(await api.memberships(companyId));
  };

  useEffect(() => {
    api.currencies().then(rows => {
      const active = rows.filter(row => row.is_active);
      setCurrencies(active);
      const preferred = active.find(row => row.code === "EGP") || active[0];
      if (preferred) setCurrencyId(String(preferred.id));
    }).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل العملات."));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setEditNameAr(selected.name_ar || "");
    setEditNameEn(selected.name_en || "");
    setEditTaxNumber(selected.tax_registration_number || "");
    setEditAddress(selected.address || "");
    setEditPhone(selected.phone || "");
    setEditEmail(selected.email || "");
  }, [selectedId, companies]);

  useEffect(() => {
    setMembers([]);
    setMatches([]);
    setUserId("");
    if (selectedId) refreshMembers(selectedId).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل مديري الشركة."));
  }, [selectedId]);

  const createCompany = async (event: FormEvent) => {
    event.preventDefault();
    if (!code.trim() || !name.trim() || !currencyId || !newAdminUsername.trim() || !newAdminPassword) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const created = await api.createCompanyWithAdmin({
        company: {
          code: code.trim().toUpperCase(), name_ar: name.trim(), country, activity,
          base_currency: Number(currencyId), is_active: true,
        },
        manager: {
          username: newAdminUsername.trim(), display_name: newAdminName.trim(),
          email: newAdminEmail.trim(), password: newAdminPassword,
        },
      });
      await refreshCompanies();
      setSelectedId(created.company.id);
      setMembers([created.manager]);
      setCode(""); setName("");
      setNewAdminUsername(""); setNewAdminName(""); setNewAdminEmail(""); setNewAdminPassword("");
      setNotice("تم حفظ الشركة وإنشاء حساب مديرها وربطه بها بنجاح.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر حفظ الشركة ومديرها."); }
    finally { setBusy(false); }
  };

  const saveCompanyProfile = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !editNameAr.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateCompany(selected.id, {
        name_ar: editNameAr.trim(),
        name_en: editNameEn.trim(),
        tax_registration_number: editTaxNumber.trim(),
        address: editAddress.trim(),
        phone: editPhone.trim(),
        email: editEmail.trim(),
      });
      await refreshCompanies();
      setNotice("تم تحديث البيانات التعريفية وبيانات التواصل. كود الشركة والدولة والنشاط والعملة الأساسية تظل ثابتة لحماية الدفاتر.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحديث بيانات الشركة."); }
    finally { setBusy(false); }
  };

  const toggleCompany = async (company: Company) => {
    const next = company.is_active === false;
    if (!window.confirm(next
      ? "إعادة تفعيل الشركة؟ ستعود الشركة للظهور ضمن الشركات النشطة."
      : "إيقاف الشركة؟ ستظل بياناتها ودفاترها محفوظة ولن تُحذف.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateCompany(company.id, { is_active: next });
      await refreshCompanies();
      setNotice(next ? "تمت إعادة تفعيل الشركة." : "تم إيقاف الشركة مع الاحتفاظ بكل سجلاتها.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة الشركة."); }
    finally { setBusy(false); }
  };

  const searchUsers = async () => {
    if (!selected || query.trim().length < 2) return;
    setBusy(true); setError(""); setMatches([]); setUserId("");
    try { setMatches(await api.searchCompanyUsers(selected.id, query.trim())); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر البحث عن المستخدمين."); }
    finally { setBusy(false); }
  };

  const createAdminAccount = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !newAdminUsername.trim() || !newAdminPassword) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createCompanyAdmin({
        company: selected.id, username: newAdminUsername.trim(),
        display_name: newAdminName.trim(), email: newAdminEmail.trim(),
        password: newAdminPassword,
      });
      await refreshMembers(selected.id);
      setNewAdminUsername(""); setNewAdminName(""); setNewAdminEmail(""); setNewAdminPassword("");
      setNotice("تم إنشاء حساب مدير الشركة وربطه بالشركة. الحساب ليس Superuser ولا يملك صلاحيات منصة.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء حساب مدير الشركة."); }
    finally { setBusy(false); }
  };

  const setMembershipRole = async (member: CompanyMembership, role: CompanyMembership["role"]) => {
    if (role === member.role || !selected) return;
    if (role === "admin" && !window.confirm("منح المستخدم صلاحية مدير الشركة على جميع الفروع؟")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateMembership(member.id, {
        role,
        ...(role === "admin" ? { all_branches: true, branches: [] } : {}),
      });
      await refreshMembers(selected.id);
      setNotice("تم تحديث دور المستخدم. تم الاحتفاظ بسجل العضوية والتغيير في سجل التدقيق.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تحديث دور المستخدم."); }
    finally { setBusy(false); }
  };

  const toggleMembership = async (member: CompanyMembership) => {
    if (!selected) return;
    const nextActive = !member.is_active;
    if (!window.confirm(nextActive
      ? "إعادة تفعيل عضوية المستخدم في هذه الشركة؟"
      : "إيقاف عضوية المستخدم؟ لن تُحذف بياناته أو سجلاته.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateMembership(member.id, { is_active: nextActive });
      await refreshMembers(selected.id);
      setNotice(nextActive ? "تمت إعادة تفعيل العضوية." : "تم إيقاف العضوية مع الاحتفاظ بسجلها.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة العضوية."); }
    finally { setBusy(false); }
  };

  const assignAdmin = async () => {
    if (!selected || !userId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createMembership({
        company: selected.id, user: Number(userId), role: "admin",
        all_branches: true, branches: [], is_active: true,
      });
      await refreshMembers(selected.id);
      setMatches([]); setQuery(""); setUserId("");
      setNotice("تم تعيين مدير الشركة. مالك المنصة لم يُضف إلى عضويات الشركة أو دفاترها.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر تعيين مدير الشركة."); }
    finally { setBusy(false); }
  };

  return <div className="app-shell" dir="rtl">
    <aside className="sidebar">
      <div className="brand-lockup"><div className="brand-symbol">A</div><div><strong>ADMRAIL</strong><span>PLATFORM OWNER</span></div></div>
      <div className="workspace"><div className="workspace-avatar">PO</div><div><strong>مالك المنصة</strong><small>إدارة سجل الشركات فقط</small></div></div>
      <nav className="navigation"><div className="nav-group"><p>إدارة المنصة</p><div className="nav-item nav-active"><span className="nav-icon">▦</span><span>سجل الشركات</span></div></div></nav>
      <div className="sidebar-bottom"><div className="user-profile"><div className="user-avatar">P</div><div><strong>Platform Owner</strong><small>صلاحيات منصة منفصلة</small></div></div></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="breadcrumb"><span>Admrail ERP v2</span><b>/</b>لوحة مالك المنصة</div>
        <div className="topbar-actions"><span className="coming-soon">وصول إداري محدود</span><button type="button" className="subtle-button" onClick={() => setShowAbout(true)}>حول البرنامج</button><button type="button" className="subtle-button" onClick={() => void onLogout()}>تسجيل الخروج</button></div>
      </header>
      <div className="page-content">
        {showAbout && <AboutPanel onClose={() => setShowAbout(false)} />}
        <section className="welcome-row"><div><p className="eyebrow">إدارة المنصة · Platform Owner</p><h1>لوحة مالك المنصة</h1><p className="welcome-copy">إدارة سجل الشركات وتعيين مديريها دون فتح دفاترها أو الاطلاع على معاملاتها المالية.</p></div></section>
        {error && <div className="error-banner">{error}</div>}
        {notice && <div className="success-banner">{notice}</div>}
        <div className="metrics-grid">
          <article className="metric-card"><div className="metric-top"><span>إجمالي الشركات</span><i className="metric-icon blue">▦</i></div><strong className="metric-value">{companies.length.toLocaleString("ar-EG")}</strong><small>يشمل النشطة والموقوفة</small></article>
          <article className="metric-card"><div className="metric-top"><span>الشركات النشطة</span><i className="metric-icon green">✓</i></div><strong className="metric-value">{companies.filter(row => row.is_active !== false).length.toLocaleString("ar-EG")}</strong><small>متاحة لإدارة الشركة</small></article>
          <article className="metric-card"><div className="metric-top"><span>الشركات الموقوفة</span><i className="metric-icon orange">Ⅱ</i></div><strong className="metric-value">{companies.filter(row => row.is_active === false).length.toLocaleString("ar-EG")}</strong><small>بياناتها محفوظة</small></article>
        </div>
        <section className="work-panel">
          <div className="work-panel-head"><div><h2>سجل الشركات</h2><p>بيانات تعريفية وحالة التشغيل فقط؛ لا توجد روابط لدفاتر الأستاذ.</p></div></div>
          <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم الشركة</th><th>الدولة</th><th>النشاط</th><th>العملة</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
            {companies.map(company => <tr key={company.id}><td>{company.code}</td><td><button type="button" className="text-action-button" onClick={() => setSelectedId(company.id)}>{company.name_ar}</button></td><td>{company.country === "EG" ? "مصر" : "السعودية"}</td><td>{activities.find(([key]) => key === company.activity)?.[1] || "—"}</td><td>{company.base_currency_code}</td><td><span className={`status-pill ${company.is_active === false ? "draft" : "posted"}`}>{company.is_active === false ? "موقوفة" : "نشطة"}</span></td><td><button type="button" className="subtle-button" disabled={busy} onClick={() => void toggleCompany(company)}>{company.is_active === false ? "إعادة تفعيل" : "إيقاف الشركة"}</button></td></tr>)}
            {!companies.length && <tr><td colSpan={7}>لا توجد شركات مسجلة حتى الآن.</td></tr>}
          </tbody></table></div>
        </section>
        <section className="work-panel">
          <div className="work-panel-head"><div><h2>إنشاء شركة جديدة</h2><p>إنشاء سجل الشركة فقط؛ لا يتم إنشاء عضوية لمالك المنصة تلقائيًا.</p></div></div>
          <form className="journal-form" onSubmit={createCompany}>
            <label>كود الشركة<input required maxLength={30} value={code} onChange={event => setCode(event.target.value)} placeholder="COMP-001" /></label>
            <label>اسم الشركة<input required maxLength={180} value={name} onChange={event => setName(event.target.value)} /></label>
            <label>الدولة<select value={country} onChange={event => {const value=event.target.value as "EG"|"SA";setCountry(value);const preferred=currencies.find(row=>row.code===(value==="EG"?"EGP":"SAR"));if(preferred)setCurrencyId(String(preferred.id));}}><option value="EG">مصر</option><option value="SA">السعودية</option></select></label>
            <label>النشاط<select value={activity} onChange={event => setActivity(event.target.value as Company["activity"])}>{activities.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
            <label>العملة الأساسية<select required value={currencyId} onChange={event => setCurrencyId(event.target.value)}><option value="">اختر العملة</option>{currencies.map(row=><option key={row.id} value={row.id}>{row.code} · {row.name_ar}</option>)}</select></label>
            <div className="setup-section-title form-wide"><div><h3>بيانات صاحب الشركة / مدير الشركة</h3><p>سيتم إنشاء حساب مستقل له وربطه بالشركة كمدير لجميع فروعها.</p></div></div>
            <label>اسم المستخدم<input required maxLength={150} autoComplete="off" value={newAdminUsername} onChange={event => setNewAdminUsername(event.target.value)} /></label>
            <label>اسم صاحب الشركة / المدير<input required maxLength={160} value={newAdminName} onChange={event => setNewAdminName(event.target.value)} /></label>
            <label>البريد الإلكتروني<input required type="email" maxLength={254} value={newAdminEmail} onChange={event => setNewAdminEmail(event.target.value)} /></label>
            <label>كلمة المرور<input required type="password" autoComplete="new-password" value={newAdminPassword} onChange={event => setNewAdminPassword(event.target.value)} /></label>
            <div className="form-actions form-wide"><button type="submit" className="primary-button" disabled={busy || !currencies.length || !newAdminUsername.trim() || !newAdminPassword}>{busy?"جارٍ الحفظ…":"حفظ الشركة وإنشاء حساب المدير"}</button></div>
          </form>
        </section>
        <section className="work-panel">
          <div className="work-panel-head"><div><h2>تعيين مدير الشركة</h2><p>اختر شركة ثم ابحث عن حساب مستخدم موجود لتعيينه مديرًا لكل فروعها.</p></div></div>
          <div className="asset-report-filters"><label>الشركة<select value={selectedId} onChange={event => setSelectedId(event.target.value ? Number(event.target.value) : "")}><option value="">اختر الشركة</option>{companies.map(row=><option key={row.id} value={row.id}>{row.name_ar} · {row.code}</option>)}</select></label></div>
          {selected && <>
            <section className="setup-section-title">
              <div><h3>البيانات التعريفية وبيانات التواصل</h3><p>يمكن تحديث الاسم وبيانات التواصل والرقم الضريبي دون تغيير هوية الشركة المحاسبية.</p></div>
            </section>
            <form className="journal-form" onSubmit={saveCompanyProfile}>
              <label>اسم الشركة بالعربية<input required maxLength={180} value={editNameAr} onChange={event => setEditNameAr(event.target.value)} /></label>
              <label>اسم الشركة بالإنجليزية<input maxLength={180} value={editNameEn} onChange={event => setEditNameEn(event.target.value)} /></label>
              <label>رقم التسجيل الضريبي<input maxLength={80} value={editTaxNumber} onChange={event => setEditTaxNumber(event.target.value)} /></label>
              <label>رقم الهاتف<input maxLength={40} value={editPhone} onChange={event => setEditPhone(event.target.value)} /></label>
              <label>البريد الإلكتروني<input type="email" maxLength={254} value={editEmail} onChange={event => setEditEmail(event.target.value)} /></label>
              <label>العنوان<input value={editAddress} onChange={event => setEditAddress(event.target.value)} /></label>
              <div className="form-actions form-wide"><button type="submit" className="primary-button" disabled={busy || !editNameAr.trim()}>{busy ? "جارٍ الحفظ…" : "حفظ بيانات الشركة"}</button></div>
            </form>
            {selected.is_active === false && <div className="asset-report-note">الشركة موقوفة؛ أعد تفعيلها قبل تعيين مدير جديد.</div>}
            <form className="journal-form" onSubmit={event => {event.preventDefault();void searchUsers();}}>
              <label className="form-wide">اسم المستخدم أو الاسم الظاهر<input minLength={2} value={query} onChange={event => setQuery(event.target.value)} placeholder="اكتب حرفين على الأقل" /></label>
              <div className="form-actions form-wide"><button type="submit" className="subtle-button" disabled={busy || selected.is_active === false || query.trim().length < 2}>بحث عن مستخدم</button></div>
              {matches.length > 0 && <label className="form-wide">المستخدم<select required value={userId} onChange={event => setUserId(event.target.value ? Number(event.target.value) : "")}><option value="">اختر المستخدم</option>{matches.map(row=><option key={row.id} value={row.id}>{row.display_name} · {row.username}</option>)}</select></label>}
              {matches.length > 0 && <div className="form-actions form-wide"><button type="button" className="primary-button" disabled={busy || !userId} onClick={() => void assignAdmin()}>تعيين مدير الشركة</button></div>}
            </form>
            {selected.is_active !== false && <section className="setup-section-title">
              <div><h3>إنشاء حساب مدير جديد</h3><p>إنشاء حساب عادي غير إداري للمنصة وربطه بهذه الشركة مباشرة.</p></div>
            </section>}
            {selected.is_active !== false && <form className="journal-form" onSubmit={createAdminAccount}>
              <label>اسم المستخدم<input required maxLength={150} autoComplete="off" value={newAdminUsername} onChange={event => setNewAdminUsername(event.target.value)} /></label>
              <label>الاسم الظاهر<input maxLength={160} value={newAdminName} onChange={event => setNewAdminName(event.target.value)} /></label>
              <label>البريد الإلكتروني<input type="email" maxLength={254} value={newAdminEmail} onChange={event => setNewAdminEmail(event.target.value)} /></label>
              <label>كلمة المرور<input required type="password" autoComplete="new-password" value={newAdminPassword} onChange={event => setNewAdminPassword(event.target.value)} /></label>
              <div className="form-actions form-wide"><button type="submit" className="primary-button" disabled={busy || !newAdminUsername.trim() || !newAdminPassword}>إنشاء الحساب وتعيينه مديرًا</button></div>
            </form>}
            <h3>المديرون والعضويات الحالية</h3>
            <div className="data-table-wrap"><table><thead><tr><th>المستخدم</th><th>الدور</th><th>النطاق</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
              {members.map(member=><tr key={member.id}>
                <td>{member.username}</td>
                <td><select aria-label={`دور ${member.username}`} value={member.role} disabled={busy} onChange={event => void setMembershipRole(member, event.target.value as CompanyMembership["role"])}>
                  <option value="admin">مدير شركة</option><option value="accountant">محاسب</option><option value="viewer">مشاهد</option>
                </select></td>
                <td>{member.all_branches?"كل الفروع":`${member.branches.length} فرع`}</td>
                <td>{member.is_active?"نشطة":"موقوفة"}</td>
                <td><button type="button" className="subtle-button" disabled={busy} onClick={() => void toggleMembership(member)}>{member.is_active?"إيقاف العضوية":"إعادة التفعيل"}</button></td>
              </tr>)}
              {!members.length && <tr><td colSpan={5}>لا توجد عضويات مسجلة لهذه الشركة.</td></tr>}
            </tbody></table></div>
          </>}
        </section>
        <p className="asset-report-note">ضابط أمني: مالك المنصة يدير السجل الإداري فقط. لا تمنحه هذه اللوحة عضوية شركة، ولا تعرض قيودًا أو أرصدة أو فواتير. الحذف النهائي للشركات غير متاح.</p>
      </div>
    </main>
  </div>;
}
