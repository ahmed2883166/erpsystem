/*
 * إعداد الشركة: إنشاء الشركة وتحديد النشاط والعملة والفروع والسنوات المالية.
 * يرسل تغييرات الهيكل إلى الخادم ويبلغ التطبيق عند إنشاء شركة أو تحديث إعداداتها.
 */
import { useEffect, useState, type FormEvent } from "react";
import { api, type Branch, type Company, type Currency, type ExchangeRate, type FiscalYear } from "./api";

type Props = {
  companies: Company[];
  companyId: number | null;
  onCompanyCreated: (company: Company) => void;
  onCompanyUpdated: (company: Company) => void;
  onStructureChanged: () => Promise<void>;
};

const activityLabels: Record<NonNullable<Company["activity"]>, string> = {
  commercial: "تجاري",
  service: "خدمي",
  medical: "طبي",
  restaurants: "مطاعم",
  industrial: "صناعي",
};

// شاشة تأسيس الشركة وإدارة الفروع والسنوات المالية والعملات والنشاط.
export default function CompanySetupPanel({ companies, companyId, onCompanyCreated, onCompanyUpdated, onStructureChanged }: Props) {
  const [currencies, setCurrencies] = useState<Currency[]>([]);
  const [currencyLoading, setCurrencyLoading] = useState(true);
  const [currencyError, setCurrencyError] = useState("");
  const [branches, setBranches] = useState<Branch[]>([]);
  const [years, setYears] = useState<FiscalYear[]>([]);
  const [exchangeRates, setExchangeRates] = useState<ExchangeRate[]>([]);
  const [fxCurrencyId, setFxCurrencyId] = useState("");
  const [fxRateDate, setFxRateDate] = useState(new Date().toISOString().slice(0, 10));
  const [fxRate, setFxRate] = useState("");
  const [fxSource, setFxSource] = useState("");
  const [fxNotes, setFxNotes] = useState("");
  const [fxBusy, setFxBusy] = useState(false);
  const [companyCode, setCompanyCode] = useState("");
  const [profileName, setProfileName] = useState("");
  const [profileNameEn, setProfileNameEn] = useState("");
  const [profileTaxNumber, setProfileTaxNumber] = useState("");
  const [profileAddress, setProfileAddress] = useState("");
  const [profilePhone, setProfilePhone] = useState("");
  const [profileEmail, setProfileEmail] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [companyNameEn, setCompanyNameEn] = useState("");
  const [managerUsername, setManagerUsername] = useState("");
  const [managerDisplayName, setManagerDisplayName] = useState("");
  const [managerEmail, setManagerEmail] = useState("");
  const [managerPassword, setManagerPassword] = useState("");
  const [country, setCountry] = useState<"EG" | "SA">("EG");
  const [activity, setActivity] = useState<NonNullable<Company["activity"]>>("commercial");
  const [enabledActivities, setEnabledActivities] = useState<NonNullable<Company["activity"]>[]>(["commercial"]);
  const [companyActivities, setCompanyActivities] = useState<NonNullable<Company["activity"]>[]>([]);
  const toggleEnabledActivity = (value: NonNullable<Company["activity"]>, checked: boolean) => {
    if (checked) {
      setEnabledActivities(current => current.includes(value) ? current : [...current, value]);
      return;
    }
    if (enabledActivities.length <= 1) return;
    if (activity === value) setActivity(enabledActivities.find(item => item !== value) || "commercial");
    setEnabledActivities(current => current.filter(item => item !== value));
  };
  const [currencyId, setCurrencyId] = useState("");
  const [taxNumber, setTaxNumber] = useState("");
  const [address, setAddress] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [branchCode, setBranchCode] = useState("BR-01");
  const [branchName, setBranchName] = useState("فرع جديد");
  const [branchAddress, setBranchAddress] = useState("");
  const [branchPhone, setBranchPhone] = useState("");
  const [editingBranchId, setEditingBranchId] = useState<number | null>(null);
  const [editBranchName, setEditBranchName] = useState("");
  const [editBranchAddress, setEditBranchAddress] = useState("");
  const [editBranchPhone, setEditBranchPhone] = useState("");
  const [yearName, setYearName] = useState(String(new Date().getFullYear()));
  const [yearStart, setYearStart] = useState(new Date().getFullYear() + "-01-01");
  const [yearEnd, setYearEnd] = useState(new Date().getFullYear() + "-12-31");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedCompany = companies.find(item => item.id === companyId) || null;

  useEffect(() => {
    if (!selectedCompany) return;
    setProfileName(selectedCompany.name_ar || "");
    setProfileNameEn(selectedCompany.name_en || "");
    setProfileTaxNumber(selectedCompany.tax_registration_number || "");
    setProfileAddress(selectedCompany.address || "");
    setProfilePhone(selectedCompany.phone || "");
    setProfileEmail(selectedCompany.email || "");
    setCompanyActivities(selectedCompany.activities?.length ? selectedCompany.activities : [selectedCompany.activity || "commercial"]);
  }, [selectedCompany?.id, selectedCompany?.updated_at]);

  // تحديث فروع وسنوات الشركة المحددة بعد الإنشاء أو التعديل.
const refreshStructure = async (id: number) => {
    const [branchRows, yearRows] = await Promise.all([api.allBranches(id), api.fiscalYears(id)]);
    setBranches(branchRows.filter(item => item.company === id));
    setYears(yearRows.filter(item => item.company === id));
  };

  const refreshExchangeRates = async (id: number) => {
    const rows = await api.exchangeRates(id);
    setExchangeRates(rows.filter(item => item.company === id));
  };

  const loadCurrencies = async () => {
    setCurrencyLoading(true);
    setCurrencyError("");
    try {
      const active = (await api.currencies()).filter(item => item.is_active);
      setCurrencies(active);
      const preferredCode = country === "EG" ? "EGP" : "SAR";
      const preferred = active.find(item => item.code === preferredCode) || active[0];
      setCurrencyId(preferred ? String(preferred.id) : "");
      if (!active.length) setCurrencyError("لا توجد عملات مفعّلة. أعد تحميل العملات أو راجع تهيئة قاعدة البيانات.");
    } catch (reason) {
      setCurrencies([]);
      setCurrencyId("");
      setCurrencyError(reason instanceof Error ? reason.message : "تعذر تحميل العملات المرجعية.");
    } finally { setCurrencyLoading(false); }
  };

  useEffect(() => { void loadCurrencies(); }, []);

  useEffect(() => {
    if (!companyId) { setBranches([]); setYears([]); setExchangeRates([]); return; }
    setError("");
    refreshStructure(companyId).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع والسنوات المالية."));
    refreshExchangeRates(companyId).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل أسعار الصرف."));
  }, [companyId]);

  useEffect(() => {
    const preferredCode = country === "EG" ? "EGP" : "SAR";
    const preferred = currencies.find(item => item.code === preferredCode);
    if (preferred) setCurrencyId(String(preferred.id));
  }, [country, currencies]);

  // إنشاء شركة جديدة وإرسال بيانات النشاط والعملة والبلد إلى الخادم.
const createCompany = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyCode.trim() || !companyName.trim() || !currencyId ||
        !managerUsername.trim() || !managerDisplayName.trim() || !managerEmail.trim() || managerPassword.length < 12) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api.createCompanyWithAdmin({
        company: {
          code: companyCode.trim().toUpperCase(),
          name_ar: companyName.trim(),
          name_en: companyNameEn.trim(),
          country,
          activity,
          activities: enabledActivities.includes(activity) ? enabledActivities : [...enabledActivities, activity],
          base_currency: Number(currencyId),
          tax_registration_number: taxNumber.trim(),
          address: address.trim(),
          phone: phone.trim(),
          email: email.trim(),
          is_active: true,
        },
        manager: {
          username: managerUsername.trim(),
          display_name: managerDisplayName.trim(),
          email: managerEmail.trim(),
          password: managerPassword,
        },
      });
      onCompanyCreated(result.company);
      setNotice("تم إنشاء الشركة والمركز الرئيسي وحساب مدير الشركة وربط صلاحياته بنجاح. أكمل إعداد السنة المالية.");
      setCompanyCode(""); setCompanyName(""); setCompanyNameEn(""); setTaxNumber(""); setAddress(""); setPhone(""); setEmail("");
      setManagerUsername(""); setManagerDisplayName(""); setManagerEmail(""); setManagerPassword("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر إنشاء الشركة ومديرها. تأكد من صلاحية مالك المنصة وصحة بيانات المدير.");
    } finally { setBusy(false); }
  };

  // حفظ بيانات الملف التعريفي للشركة الحالية دون إنشاء شركة جديدة.
  const saveCompanyProfile = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !profileName.trim()) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await api.updateCompany(companyId, {
        name_ar: profileName.trim(),
        name_en: profileNameEn.trim(),
        tax_registration_number: profileTaxNumber.trim(),
        address: profileAddress.trim(),
        phone: profilePhone.trim(),
        email: profileEmail.trim(),
      });
      onCompanyUpdated(updated);
      setNotice("تم حفظ بيانات الشركة بنجاح.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ بيانات الشركة.");
    } finally { setBusy(false); }
  };

  const saveCompanyActivities = async () => {
    if (!companyId || !selectedCompany) return;
    const primary = selectedCompany.activity || "commercial";
    const activities = companyActivities.includes(primary) ? companyActivities : [primary, ...companyActivities];
    setBusy(true); setError(""); setNotice("");
    try {
      const updated = await api.updateCompany(companyId, { activities });
      onCompanyUpdated(updated);
      setCompanyActivities(updated.activities?.length ? updated.activities : [primary]);
      setNotice("تم تحديث أنشطة الشركة مع الإبقاء على النشاط الأساسي.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحديث أنشطة الشركة.");
    } finally { setBusy(false); }
  };

  const toggleCompanyActivity = (value: NonNullable<Company["activity"]>, checked: boolean) => {
    if (value === (selectedCompany?.activity || "commercial")) return;
    setCompanyActivities(current => checked
      ? (current.includes(value) ? current : [...current, value])
      : current.filter(item => item !== value));
  };

  // إضافة فرع جديد إلى الشركة الحالية.
const createBranch = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createBranch({
        company: companyId, code: branchCode.trim().toUpperCase(),
        name_ar: branchName.trim(), address: branchAddress.trim(), phone: branchPhone.trim(),
        is_head_office: false, is_active: true,
      });
      await refreshStructure(companyId);
      await onStructureChanged();
      setNotice("تم إنشاء الفرع وحفظه.");
      setBranchCode(""); setBranchName("فرع جديد"); setBranchAddress(""); setBranchPhone("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء الفرع."); }
    finally { setBusy(false); }
  };

  const beginBranchEdit = (branch: Branch) => {
    setEditingBranchId(branch.id);
    setEditBranchName(branch.name_ar);
    setEditBranchAddress(branch.address || "");
    setEditBranchPhone(branch.phone || "");
    setError(""); setNotice("");
  };

  const saveBranchEdit = async (event: FormEvent, branchId: number) => {
    event.preventDefault();
    if (!editBranchName.trim() || !companyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateBranch(branchId, {
        name_ar: editBranchName.trim(),
        address: editBranchAddress.trim(),
        phone: editBranchPhone.trim(),
      });
      await refreshStructure(companyId);
      await onStructureChanged();
      setEditingBranchId(null);
      setNotice("تم تحديث بيانات الفرع دون تغيير كوده أو ارتباطه بالشركة.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تحديث بيانات الفرع.");
    } finally { setBusy(false); }
  };

  const transferHeadOffice = async (branch: Branch) => {
    if (!companyId || branch.is_head_office || !branch.is_active) return;
    if (!window.confirm(`هل تريد جعل "${branch.name_ar}" المركز الرئيسي للشركة؟ سيصبح المركز الحالي فرعًا عاديًا دون حذف أي بيانات.`)) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.makeHeadOffice(branch.id);
      await refreshStructure(companyId);
      await onStructureChanged();
      setNotice(`تم نقل صفة المركز الرئيسي إلى ${branch.name_ar} بنجاح.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر نقل صفة المركز الرئيسي.");
    } finally { setBusy(false); }
  };

  const toggleBranchStatus = async (branch: Branch) => {
    if (!companyId) return;
    if (branch.is_active && !window.confirm("هل تريد إيقاف هذا الفرع؟ لن يظهر في العمليات الجديدة، وستظل سجلاته السابقة محفوظة.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.updateBranch(branch.id, { is_active: !branch.is_active });
      await refreshStructure(companyId);
      await onStructureChanged();
      setNotice(branch.is_active ? "تم إيقاف الفرع مع الاحتفاظ بسجلاته." : "تمت إعادة تفعيل الفرع.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر تغيير حالة الفرع.");
    } finally { setBusy(false); }
  };

  // إنشاء سنة مالية للشركة مع تواريخ بدايتها ونهايتها.
const createYear = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || yearStart > yearEnd) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createFiscalYear({
        company: companyId, name: yearName.trim(),
        start_date: yearStart, end_date: yearEnd, status: "open",
      });
      await refreshStructure(companyId);
      await onStructureChanged();
      setNotice("تم إنشاء السنة المالية المفتوحة.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء السنة المالية."); }
    finally { setBusy(false); }
  };


  const createExchangeRate = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId || !fxCurrencyId || !fxRateDate || !fxRate.trim()) return;
    setFxBusy(true); setError(""); setNotice("");
    try {
      await api.createExchangeRate({
        company: companyId, currency: Number(fxCurrencyId), rate_date: fxRateDate,
        rate: fxRate.trim(), source: fxSource.trim(), notes: fxNotes.trim(),
      });
      await refreshExchangeRates(companyId);
      setFxRate(""); setFxSource(""); setFxNotes("");
      setNotice("تم حفظ سعر الصرف كسجل تاريخي جديد. لا يمكن تعديل السجل السابق.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر حفظ سعر الصرف.");
    } finally { setFxBusy(false); }
  };

  return <section className="work-panel company-setup-panel">
    <div className="work-panel-head">
      <div><h2>{selectedCompany ? "بيانات الشركة وإعداداتها" : "تأسيس بيانات الشركة"}</h2><p>{selectedCompany ? "هذه الشاشة لتعريف الشركة وإدارة فروعها وسنواتها المالية؛ أما القيود والفواتير والتقارير فتوجد في شاشة العمل اليومية المستقلة." : "إعداد أول شركة على قاعدة PostgreSQL الجديدة، دون استيراد بيانات من النظام القديم."}</p></div>
      <span className="coming-soon">{companies.length ? "إعداد الشركة" : "بدء الاستخدام"}</span>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}

    {!companies.length && <div className="setup-intro">
      <strong>مرحبًا بك في NEXUS ERP</strong>
      <p>أدخل بيانات الشركة وبيانات مديرها في نفس الخطوة. سيتم إنشاء الشركة والمركز الرئيسي وحساب المدير وربط صلاحياته معًا؛ ويمكن إضافة فروع أخرى لاحقًا.</p>
    </div>}

    {!companyId && <form className="journal-form" onSubmit={createCompany}>
      <h3 className="form-wide">بيانات الشركة الأساسية</h3>
      <label>كود الشركة<input required maxLength={30} value={companyCode} onChange={e => setCompanyCode(e.target.value)} placeholder="COMP-001" /></label>
      <label>اسم الشركة بالعربية<input required maxLength={180} value={companyName} onChange={e => setCompanyName(e.target.value)} /></label>
      <label>اسم الشركة بالإنجليزية<input maxLength={180} value={companyNameEn} onChange={e => setCompanyNameEn(e.target.value)} /></label>
      <label>الدولة<select value={country} onChange={e => setCountry(e.target.value as "EG" | "SA")}><option value="EG">مصر</option><option value="SA">السعودية</option></select></label>
      <label>النشاط الأساسي<select value={activity} onChange={e => { const value = e.target.value as NonNullable<Company["activity"]>; setActivity(value); setEnabledActivities(current => current.includes(value) ? current : [...current, value]); }}>{Object.entries(activityLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <fieldset className="form-wide activity-selection"><legend>الأنشطة المفعّلة للشركة</legend><p className="asset-report-note">يمكن للشركة ممارسة أكثر من نشاط في نفس الوقت. اختر كل الأنشطة المطلوبة، وحدد النشاط الأساسي من القائمة السابقة.</p><div className="activity-checkbox-grid">{Object.entries(activityLabels).map(([key, label]) => <label key={key}><input type="checkbox" checked={enabledActivities.includes(key as NonNullable<Company["activity"]>)} onChange={e => toggleEnabledActivity(key as NonNullable<Company["activity"]>, e.target.checked)} />{label}</label>)}</div></fieldset>
      <label>العملة الأساسية<select required value={currencyId} onChange={e => setCurrencyId(e.target.value)} disabled={currencyLoading || !currencies.length}><option value="">{currencyLoading ? "جارٍ تحميل العملات…" : "اختر العملة"}</option>{currencies.map(item => <option key={item.id} value={item.id}>{item.code} · {item.name_ar}</option>)}</select></label>
      {currencyError && <div className="asset-report-note form-wide" role="alert">{currencyError} <button type="button" className="text-action-button" onClick={() => void loadCurrencies()} disabled={currencyLoading}>إعادة تحميل العملات</button></div>}
      <label>رقم التسجيل الضريبي<input maxLength={80} value={taxNumber} onChange={e => setTaxNumber(e.target.value)} /></label>
      <label>الهاتف<input maxLength={40} value={phone} onChange={e => setPhone(e.target.value)} /></label>
      <label>البريد الإلكتروني<input type="email" maxLength={254} value={email} onChange={e => setEmail(e.target.value)} /></label>
      <label className="form-wide">العنوان<textarea rows={2} value={address} onChange={e => setAddress(e.target.value)} /></label>
      <h3 className="form-wide">بيانات مدير الشركة الأول</h3>
      <label>اسم المدير<input required maxLength={150} value={managerDisplayName} onChange={e => setManagerDisplayName(e.target.value)} placeholder="الاسم الكامل" /></label>
      <label>اسم المستخدم<input required maxLength={150} value={managerUsername} onChange={e => setManagerUsername(e.target.value)} autoComplete="username" /></label>
      <label>البريد الإلكتروني<input required type="email" maxLength={254} value={managerEmail} onChange={e => setManagerEmail(e.target.value)} autoComplete="email" /></label>
      <label>كلمة مرور المدير<input required type="password" minLength={12} maxLength={256} value={managerPassword} onChange={e => setManagerPassword(e.target.value)} autoComplete="new-password" /><small>12 حرفًا على الأقل وفق سياسة كلمات المرور.</small></label>
      {activity === "industrial" && <p className="asset-report-note form-wide">النشاط الصناعي هنا لتجهيز الحسابات والمخزون فقط؛ أوامر الإنتاج وقوائم المواد وتحويل الخام إلى منتج نهائي غير مفعّلة في هذه المرحلة.</p>}
      <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || currencyLoading || !currencies.length || !currencyId || !managerUsername.trim() || !managerDisplayName.trim() || !managerEmail.trim() || managerPassword.length < 12}>{busy ? "جارٍ إنشاء الشركة والمدير…" : "إنشاء الشركة ومديرها"}</button></div>
      <p className="asset-report-note form-wide">إنشاء الشركة ومديرها متاح لحساب مالك المنصة الأول. يتم إنشاء الشركة والفرع الرئيسي وحساب المدير وعضويته في عملية واحدة؛ لا يتم إنشاء نسب ضريبية قانونية تلقائيًا.</p>
    </form>}

    {selectedCompany && <>
      <div className="setup-section-title"><div><h3>الملف التعريفي للشركة</h3><p>تعديل بيانات الشركة الحالية مع الاحتفاظ بنفس رقمها وسجلاتها المحاسبية.</p></div><span className="coming-soon">تعديل آمن</span></div>
      <form className="journal-form company-profile-form" onSubmit={saveCompanyProfile}>
        <label>اسم الشركة بالعربية<input required maxLength={180} value={profileName} onChange={e => setProfileName(e.target.value)} /></label>
        <label>اسم الشركة بالإنجليزية<input maxLength={180} value={profileNameEn} onChange={e => setProfileNameEn(e.target.value)} /></label>
        <label>رقم التسجيل الضريبي<input maxLength={80} value={profileTaxNumber} onChange={e => setProfileTaxNumber(e.target.value)} /></label>
        <label>رقم الهاتف<input maxLength={40} value={profilePhone} onChange={e => setProfilePhone(e.target.value)} /></label>
        <label>البريد الإلكتروني<input type="email" maxLength={254} value={profileEmail} onChange={e => setProfileEmail(e.target.value)} /></label>
        <label className="form-wide">العنوان<textarea rows={2} value={profileAddress} onChange={e => setProfileAddress(e.target.value)} /></label>
        <p className="asset-report-note form-wide">حالة الشركة: {selectedCompany.is_active === false ? "موقوفة" : "نشطة"}. تغيير الحالة له أثر على صلاحية الوصول للعمليات، لذلك لا يتم من نموذج تعديل البيانات العادية.</p>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || !profileName.trim()}>{busy ? "جارٍ حفظ البيانات…" : "حفظ بيانات الشركة"}</button></div>
        <p className="asset-report-note form-wide">الدولة والنشاط والعملة الأساسية معروضة أدناه كإعدادات تأسيسية؛ لا نغيّرها من تعديل الملف التعريفي حتى لا تتأثر المعالجة الضريبية أو الأرصدة المحاسبية القائمة.</p>
      </form>
      <div className="setup-section-title"><div><h3>الأنشطة التشغيلية للشركة</h3><p>يمكن إضافة أو إيقاف الأنشطة الإضافية دون تغيير النشاط الأساسي أو التأثير على بيانات الشركة.</p></div></div>
      <fieldset className="activity-selection"><legend>الأنشطة المفعّلة</legend><div className="activity-checkbox-grid">{Object.entries(activityLabels).map(([key, label]) => { const value = key as NonNullable<Company["activity"]>; const primary = value === (selectedCompany?.activity || "commercial"); return <label key={key}><input type="checkbox" checked={primary || companyActivities.includes(value)} disabled={primary || busy} onChange={event => toggleCompanyActivity(value, event.target.checked)} />{label}{primary ? " · أساسي" : ""}</label>; })}</div><div className="form-actions"><button type="button" className="primary-button" disabled={busy || !companyActivities.length} onClick={() => void saveCompanyActivities()}>{busy ? "جارٍ حفظ الأنشطة…" : "حفظ الأنشطة"}</button></div></fieldset>

      <div className="setup-company-summary">
        <div><span>الشركة المحددة</span><strong>{selectedCompany.name_ar}</strong></div>
        <div><span>الكود</span><strong>{selectedCompany.code}</strong></div>
        <div><span>الدولة</span><strong>{selectedCompany.country === "EG" ? "مصر" : "السعودية"}</strong></div>
        <div><span>النشاط الأساسي</span><strong>{activityLabels[selectedCompany.activity || "commercial"]}</strong></div>
        <div><span>الأنشطة المفعّلة</span><strong>{(selectedCompany.activities?.length ? selectedCompany.activities : [selectedCompany.activity || "commercial"]).map(value => activityLabels[value]).join("، ")}</strong></div>
        <div><span>العملة الأساسية</span><strong>{selectedCompany.base_currency_code}</strong></div>
        <div><span>حالة الشركة</span><strong>{selectedCompany.is_active === false ? "موقوفة" : "نشطة"}</strong></div>
      </div>

      <div className="setup-step-heading"><span>1</span><div><strong>الفروع</strong><small>تم إنشاء المركز الرئيسي تلقائيًا؛ يمكنك تعديل بياناته أو إضافة فروع أخرى لاحقًا.</small></div></div>
      {branches.length > 0 && <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم الفرع</th><th>نوع الفرع</th><th>العنوان</th><th>الهاتف</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>{branches.map(item => <tr key={item.id}>
        <td>{item.code}</td>
        {editingBranchId === item.id ? <>
          <td colSpan={4}><form className="branch-inline-edit" onSubmit={event => saveBranchEdit(event, item.id)}>
            <input required maxLength={160} aria-label="اسم الفرع" value={editBranchName} onChange={event => setEditBranchName(event.target.value)} />
            <input maxLength={40} aria-label="هاتف الفرع" value={editBranchPhone} onChange={event => setEditBranchPhone(event.target.value)} placeholder="الهاتف" />
            <input aria-label="عنوان الفرع" value={editBranchAddress} onChange={event => setEditBranchAddress(event.target.value)} placeholder="العنوان" />
            <button className="primary-button" type="submit" disabled={busy}>حفظ</button>
            <button type="button" onClick={() => setEditingBranchId(null)} disabled={busy}>إلغاء</button>
          </form></td>
        </> : <>
          <td>{item.name_ar}</td><td>{item.is_head_office ? "رئيسي" : "فرعي"}</td><td>{item.address || "—"}</td><td>{item.phone || "—"}</td>
        </>}
        <td>{item.is_active ? "نشط" : "موقوف"}</td>
        <td><div className="table-actions">
          <button type="button" onClick={() => editingBranchId === item.id ? setEditingBranchId(null) : beginBranchEdit(item)} disabled={busy}>{editingBranchId === item.id ? "إغلاق التعديل" : "تعديل"}</button>
          {!item.is_head_office && item.is_active && <button type="button" onClick={() => transferHeadOffice(item)} disabled={busy}>جعله مركزًا رئيسيًا</button>}
          <button type="button" onClick={() => toggleBranchStatus(item)} disabled={busy || item.is_head_office}>{item.is_active ? (item.is_head_office ? "المركز الرئيسي" : "إيقاف") : "إعادة تفعيل"}</button>
        </div></td>
      </tr>)}</tbody></table></div>}
      <form className="journal-form setup-subform" onSubmit={createBranch}>
        <label>كود الفرع<input required maxLength={30} value={branchCode} onChange={e => setBranchCode(e.target.value)} placeholder="BR-01" /></label>
        <label>اسم الفرع<input required maxLength={160} value={branchName} onChange={e => setBranchName(e.target.value)} placeholder="فرع جديد" /></label>
        <label>الهاتف<input maxLength={40} value={branchPhone} onChange={e => setBranchPhone(e.target.value)} /></label>
        <label className="form-wide">العنوان<input value={branchAddress} onChange={e => setBranchAddress(e.target.value)} /></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy}>{busy ? "جارٍ الحفظ…" : branches.length ? "إضافة فرع" : "إنشاء الفرع الرئيسي"}</button></div>
      </form>

      <div className="setup-step-heading"><span>2</span><div><strong>السنة المالية</strong><small>حدد بداية ونهاية السنة المالية المعتمدة لشركتك.</small></div></div>
      {years.length > 0 && <div className="data-table-wrap"><table><thead><tr><th>السنة</th><th>من</th><th>إلى</th><th>الحالة</th></tr></thead><tbody>{years.map(item => <tr key={item.id}><td>{item.name}</td><td>{item.start_date}</td><td>{item.end_date}</td><td>{item.status === "open" ? "مفتوحة" : "مغلقة"}</td></tr>)}</tbody></table></div>}
      <form className="journal-form setup-subform" onSubmit={createYear}>
        <label>اسم السنة المالية<input required maxLength={80} value={yearName} onChange={e => setYearName(e.target.value)} /></label>
        <label>تاريخ البداية<input required type="date" value={yearStart} onChange={e => setYearStart(e.target.value)} /></label>
        <label>تاريخ النهاية<input required type="date" min={yearStart} value={yearEnd} onChange={e => setYearEnd(e.target.value)} /></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || yearStart > yearEnd}>{busy ? "جارٍ الحفظ…" : "إنشاء السنة المالية"}</button></div>
      </form>

      <div className="setup-step-heading"><span>3</span><div><strong>أسعار صرف العملات</strong><small>سعر وحدة واحدة من العملة الأجنبية مقابل العملة الأساسية للشركة ({selectedCompany.base_currency_code}). يُستخدم التاريخ الأقرب السابق لتاريخ العملية.</small></div></div>
      <form className="journal-form setup-subform" onSubmit={createExchangeRate}>
        <label>العملة الأجنبية<select required value={fxCurrencyId} onChange={event => setFxCurrencyId(event.target.value)}>
          <option value="">اختر العملة</option>
          {currencies.filter(item => item.is_active && item.id !== selectedCompany.base_currency).map(item => <option key={item.id} value={item.id}>{item.code} · {item.name_ar}</option>)}
        </select></label>
        <label>تاريخ السريان<input required type="date" value={fxRateDate} onChange={event => setFxRateDate(event.target.value)} /></label>
        <label>سعر الصرف<input required type="number" min="0.0000000001" step="0.0000000001" value={fxRate} onChange={event => setFxRate(event.target.value)} placeholder="مثال: 48.2500000000" /></label>
        <label>مصدر السعر<input maxLength={120} value={fxSource} onChange={event => setFxSource(event.target.value)} placeholder="مثال: مستند البنك أو نشرة موثقة" /></label>
        <label className="form-wide">ملاحظات<input maxLength={500} value={fxNotes} onChange={event => setFxNotes(event.target.value)} /></label>
        <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={fxBusy || !fxCurrencyId || !fxRate.trim()}>{fxBusy ? "جارٍ حفظ السعر…" : "إضافة سعر صرف بتاريخ السريان"}</button></div>
        <p className="asset-report-note form-wide">اتجاه السعر ثابت: قيمة العملة الأساسية مقابل وحدة واحدة من العملة الأجنبية. الأسعار لا تُجلب تلقائيًا من الإنترنت، ويجب توثيق مصدرها. إضافة الأسعار لا تفعّل بعد ترحيل الحركات الأجنبية؛ ما زال ربط القيود وفروق العملة قيد التنفيذ.</p>
      </form>
      {exchangeRates.length > 0 && <div className="data-table-wrap"><table><thead><tr><th>تاريخ السريان</th><th>العملة</th><th>سعر الوحدة</th><th>المصدر</th><th>ملاحظات</th></tr></thead><tbody>
        {exchangeRates.map(item => <tr key={item.id}><td>{item.rate_date}</td><td>{item.currency_code}</td><td>{Number(item.rate).toLocaleString("en-US", { maximumFractionDigits: 10 })} {item.base_currency_code}</td><td>{item.source || "—"}</td><td>{item.notes || "—"}</td></tr>)}
      </tbody></table></div>}
      {branches.length > 0 && years.some(item => item.status === "open") && <div className="success-banner setup-complete">تم حفظ بيانات الشركة والفرع والسنة المالية. قبل بدء القيود، يلزم إعداد دليل الحسابات المناسب للنشاط وربط أدوار الحسابات.</div>}
    </>}
  </section>;
}
