/*
 * إعداد الشركة: إنشاء الشركة وتحديد النشاط والعملة والفروع والسنوات المالية.
 * يرسل تغييرات الهيكل إلى الخادم ويبلغ التطبيق عند إنشاء شركة أو تحديث إعداداتها.
 */
import { useEffect, useState, type FormEvent } from "react";
import { api, type Branch, type Company, type Currency, type FiscalYear } from "./api";

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
  const [branches, setBranches] = useState<Branch[]>([]);
  const [years, setYears] = useState<FiscalYear[]>([]);
  const [companyCode, setCompanyCode] = useState("");
  const [profileName, setProfileName] = useState("");
  const [profileNameEn, setProfileNameEn] = useState("");
  const [profileTaxNumber, setProfileTaxNumber] = useState("");
  const [profileAddress, setProfileAddress] = useState("");
  const [profilePhone, setProfilePhone] = useState("");
  const [profileEmail, setProfileEmail] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [country, setCountry] = useState<"EG" | "SA">("EG");
  const [activity, setActivity] = useState<NonNullable<Company["activity"]>>("commercial");
  const [currencyId, setCurrencyId] = useState("");
  const [taxNumber, setTaxNumber] = useState("");
  const [address, setAddress] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [branchCode, setBranchCode] = useState("MAIN");
  const [branchName, setBranchName] = useState("الفرع الرئيسي");
  const [branchAddress, setBranchAddress] = useState("");
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
  }, [selectedCompany?.id, selectedCompany?.updated_at]);

  // تحديث فروع وسنوات الشركة المحددة بعد الإنشاء أو التعديل.
const refreshStructure = async (id: number) => {
    const [branchRows, yearRows] = await Promise.all([api.branches(id), api.fiscalYears(id)]);
    setBranches(branchRows.filter(item => item.company === id));
    setYears(yearRows.filter(item => item.company === id));
  };

  useEffect(() => {
    api.currencies().then(items => {
      const active = items.filter(item => item.is_active);
      setCurrencies(active);
      const preferred = active.find(item => item.code === "EGP") || active[0];
      if (preferred) setCurrencyId(String(preferred.id));
    }).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل العملات المرجعية."));
  }, []);

  useEffect(() => {
    if (!companyId) { setBranches([]); setYears([]); return; }
    setError("");
    refreshStructure(companyId).catch(reason => setError(reason instanceof Error ? reason.message : "تعذر تحميل الفروع والسنوات المالية."));
  }, [companyId]);

  useEffect(() => {
    const preferredCode = country === "EG" ? "EGP" : "SAR";
    const preferred = currencies.find(item => item.code === preferredCode);
    if (preferred) setCurrencyId(String(preferred.id));
  }, [country, currencies]);

  // إنشاء شركة جديدة وإرسال بيانات النشاط والعملة والبلد إلى الخادم.
const createCompany = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyCode.trim() || !companyName.trim() || !currencyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const company = await api.createCompany({
        code: companyCode.trim().toUpperCase(),
        name_ar: companyName.trim(),
        country,
        activity,
        base_currency: Number(currencyId),
        tax_registration_number: taxNumber.trim(),
        address: address.trim(),
        phone: phone.trim(),
        email: email.trim(),
        is_active: true,
      });
      onCompanyCreated(company);
      setNotice("تم إنشاء الشركة وربط حسابك بصفة مدير شركة. أكمل إعداد الفرع والسنة المالية.");
      setCompanyCode(""); setCompanyName(""); setTaxNumber(""); setAddress(""); setPhone(""); setEmail("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "تعذر إنشاء الشركة. تأكد من تسجيل الدخول بحساب مسؤول النظام.");
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

  // إضافة فرع جديد إلى الشركة الحالية.
const createBranch = async (event: FormEvent) => {
    event.preventDefault();
    if (!companyId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api.createBranch({
        company: companyId, code: branchCode.trim().toUpperCase(),
        name_ar: branchName.trim(), address: branchAddress.trim(),
        is_head_office: branches.length === 0, is_active: true,
      });
      await refreshStructure(companyId);
      await onStructureChanged();
      setNotice("تم إنشاء الفرع وحفظه.");
      setBranchCode(""); setBranchName(""); setBranchAddress("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "تعذر إنشاء الفرع."); }
    finally { setBusy(false); }
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

  return <section className="work-panel company-setup-panel">
    <div className="work-panel-head">
      <div><h2>{selectedCompany ? "بيانات الشركة وإعداداتها" : "تأسيس بيانات الشركة"}</h2><p>{selectedCompany ? "هذه الشاشة لتعريف الشركة وإدارة فروعها وسنواتها المالية؛ أما القيود والفواتير والتقارير فتوجد في شاشة العمل اليومية المستقلة." : "إعداد أول شركة على قاعدة PostgreSQL الجديدة، دون استيراد بيانات من النظام القديم."}</p></div>
      <span className="coming-soon">{companies.length ? "إعداد الشركة" : "بدء الاستخدام"}</span>
    </div>
    {error && <div className="error-banner">{error}</div>}
    {notice && <div className="success-banner">{notice}</div>}

    {!companies.length && <div className="setup-intro">
      <strong>مرحبًا بك في NEXUS ERP</strong>
      <p>ابدأ بإدخال بيانات شركتك لأول مرة. بعد الحفظ سيتم إنشاء عضوية مدير الشركة لحسابك، ثم يمكنك إضافة الفرع والسنة المالية.</p>
    </div>}

    {!companyId && <form className="journal-form" onSubmit={createCompany}>
      <h3 className="form-wide">بيانات الشركة الأساسية</h3>
      <label>كود الشركة<input required maxLength={30} value={companyCode} onChange={e => setCompanyCode(e.target.value)} placeholder="COMP-001" /></label>
      <label>اسم الشركة بالعربية<input required maxLength={180} value={companyName} onChange={e => setCompanyName(e.target.value)} /></label>
      <label>الدولة<select value={country} onChange={e => setCountry(e.target.value as "EG" | "SA")}><option value="EG">مصر</option><option value="SA">السعودية</option></select></label>
      <label>النشاط<select value={activity} onChange={e => setActivity(e.target.value as NonNullable<Company["activity"]>)}>{Object.entries(activityLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>العملة الأساسية<select required value={currencyId} onChange={e => setCurrencyId(e.target.value)}><option value="">اختر العملة</option>{currencies.map(item => <option key={item.id} value={item.id}>{item.code} · {item.name_ar}</option>)}</select></label>
      <label>رقم التسجيل الضريبي<input maxLength={80} value={taxNumber} onChange={e => setTaxNumber(e.target.value)} /></label>
      <label>الهاتف<input maxLength={40} value={phone} onChange={e => setPhone(e.target.value)} /></label>
      <label>البريد الإلكتروني<input type="email" maxLength={254} value={email} onChange={e => setEmail(e.target.value)} /></label>
      <label className="form-wide">العنوان<textarea rows={2} value={address} onChange={e => setAddress(e.target.value)} /></label>
      {activity === "industrial" && <p className="asset-report-note form-wide">النشاط الصناعي هنا لتجهيز الحسابات والمخزون فقط؛ أوامر الإنتاج وقوائم المواد وتحويل الخام إلى منتج نهائي غير مفعّلة في هذه المرحلة.</p>}
      <div className="form-actions form-wide"><button className="primary-button" type="submit" disabled={busy || !currencies.length}>{busy ? "جارٍ إنشاء الشركة…" : "إنشاء الشركة والانتقال للخطوة التالية"}</button></div>
      <p className="asset-report-note form-wide">إنشاء الشركة متاح لحساب مسؤول النظام الأول الذي تم إنشاؤه أثناء تشغيل البرنامج لأول مرة. لا يتم إنشاء نسب ضريبية قانونية تلقائيًا.</p>
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
      <div className="setup-company-summary">
        <div><span>الشركة المحددة</span><strong>{selectedCompany.name_ar}</strong></div>
        <div><span>الكود</span><strong>{selectedCompany.code}</strong></div>
        <div><span>الدولة</span><strong>{selectedCompany.country === "EG" ? "مصر" : "السعودية"}</strong></div>
        <div><span>النشاط</span><strong>{activityLabels[selectedCompany.activity || "commercial"]}</strong></div>
        <div><span>العملة الأساسية</span><strong>{selectedCompany.base_currency_code}</strong></div>
        <div><span>حالة الشركة</span><strong>{selectedCompany.is_active === false ? "موقوفة" : "نشطة"}</strong></div>
      </div>

      <div className="setup-step-heading"><span>1</span><div><strong>الفروع</strong><small>أنشئ الفرع الرئيسي أولًا، ويمكن إضافة فروع أخرى لاحقًا.</small></div></div>
      {branches.length > 0 && <div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>اسم الفرع</th><th>نوع الفرع</th><th>الحالة</th></tr></thead><tbody>{branches.map(item => <tr key={item.id}><td>{item.code}</td><td>{item.name_ar}</td><td>{item.is_head_office ? "رئيسي" : "فرعي"}</td><td>{item.is_active ? "نشط" : "موقوف"}</td></tr>)}</tbody></table></div>}
      <form className="journal-form setup-subform" onSubmit={createBranch}>
        <label>كود الفرع<input required maxLength={30} value={branchCode} onChange={e => setBranchCode(e.target.value)} placeholder="MAIN" /></label>
        <label>اسم الفرع<input required maxLength={160} value={branchName} onChange={e => setBranchName(e.target.value)} placeholder="الفرع الرئيسي" /></label>
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
      {branches.length > 0 && years.some(item => item.status === "open") && <div className="success-banner setup-complete">تم حفظ بيانات الشركة والفرع والسنة المالية. قبل بدء القيود، يلزم إعداد دليل الحسابات المناسب للنشاط وربط أدوار الحسابات.</div>}
    </>}
  </section>;
}
