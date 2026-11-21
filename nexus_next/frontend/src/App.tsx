import { useMemo, useState } from "react";

type Section = "overview" | "journals" | "accounts" | "reports" | "setup";
const navigation: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "نظرة عامة", icon: "◫", group: "الرئيسية" },
  { id: "journals", label: "قيود اليومية", icon: "⇄", group: "المحاسبة" },
  { id: "accounts", label: "دليل الحسابات", icon: "☷", group: "المحاسبة" },
  { id: "reports", label: "التقارير المالية", icon: "▤", group: "التقارير" },
  { id: "setup", label: "إعدادات الشركة", icon: "⚙", group: "الإدارة" },
];

const modules = [
  { title: "المحاسبة العامة", detail: "القيود، دليل الحسابات، مراكز التكلفة", icon: "◈", tone: "blue" },
  { title: "المبيعات والعملاء", detail: "دورة البيع والتحصيل والأرصدة", icon: "↗", tone: "violet" },
  { title: "المشتريات والموردون", detail: "الفواتير والاستحقاقات والمدفوعات", icon: "↙", tone: "cyan" },
  { title: "المخزون والمستودعات", detail: "الأصناف والحركات والتقييم", icon: "▦", tone: "amber" },
  { title: "المرتبات والموارد البشرية", detail: "الموظفون والاستحقاقات والحضور", icon: "♙", tone: "green" },
  { title: "الأصول الثابتة", detail: "الإهلاك والتصرفات والتسويات", icon: "▣", tone: "rose" },
];

export default function App() {
  const [active, setActive] = useState<Section>("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const current = useMemo(() => navigation.find((item) => item.id === active), [active]);

  return (
    <div className="app-shell" dir="rtl">
      {sidebarOpen && <button className="mobile-scrim" aria-label="إغلاق القائمة" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="brand-lockup">
          <div className="brand-symbol">N</div>
          <div><strong>NEXUS</strong><span>ENTERPRISE PLATFORM</span></div>
          <button className="sidebar-close" onClick={() => setSidebarOpen(false)} aria-label="إغلاق القائمة">×</button>
        </div>
        <div className="workspace">
          <div className="workspace-avatar">NX</div>
          <div><strong>مساحة العمل</strong><small>الشركة التجريبية</small></div>
          <span className="chevron">⌄</span>
        </div>
        <nav className="navigation">
          {["الرئيسية", "المحاسبة", "التقارير", "الإدارة"].map((group) => (
            <div className="nav-group" key={group}>
              <p>{group}</p>
              {navigation.filter((item) => item.group === group).map((item) => (
                <button key={item.id} className={`nav-item ${active === item.id ? "nav-active" : ""}`}
                  onClick={() => { setActive(item.id); setSidebarOpen(false); }}>
                  <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
                  {active === item.id && <i />}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="help-card"><span>؟</span><div><strong>تحتاج إلى مساعدة؟</strong><small>مركز المساعدة والدعم</small></div><b>←</b></div>
          <div className="user-profile"><div className="user-avatar">م</div><div><strong>مدير النظام</strong><small>مسؤول النظام</small></div><span>•••</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-start">
            <button className="menu-toggle" onClick={() => setSidebarOpen(true)} aria-label="فتح القائمة">☰</button>
            <div className="breadcrumb"><span>NEXUS</span><b>/</b>{current?.label}</div>
          </div>
          <div className="topbar-actions">
            <div className="period-select"><span className="period-dot" /> السنة المالية 2026 <span>⌄</span></div>
            <button className="icon-button" aria-label="البحث">⌕</button>
            <button className="icon-button notification" aria-label="الإشعارات">♧<i /></button>
            <div className="top-user"><div className="user-avatar">م</div><span>مدير النظام</span><b>⌄</b></div>
          </div>
        </header>

        <div className="page-content">
          <section className="welcome-row">
            <div><p className="eyebrow">السبت، 21 نوفمبر 2026</p><h1>{active === "overview" ? "أهلاً بك في NEXUS" : current?.label}</h1>
              <p className="welcome-copy">{active === "overview" ? "كل أعمالك في مكان واحد. إليك نظرة سريعة على مساحة العمل." : "واجهة العمل الجديدة — يتم توصيل البيانات الفعلية تدريجياً."}</p></div>
            <button className="primary-button" onClick={() => setActive("journals")}><span>＋</span> إنشاء قيد يومية</button>
          </section>

          {active === "overview" ? <>
            <div className="section-heading"><div><h2>ملخص الأداء</h2><p>مؤشرات الشركة للفترة الحالية</p></div><button className="subtle-button">آخر تحديث <span>⟳</span></button></div>
            <div className="metrics-grid">
              {[
                { label: "إجمالي الإيرادات", value: "—", note: "بانتظار البيانات الفعلية", icon: "↗", tone: "blue" },
                { label: "إجمالي المصروفات", value: "—", note: "بانتظار البيانات الفعلية", icon: "↘", tone: "orange" },
                { label: "صافي الربح", value: "—", note: "سيظهر بعد ترحيل القيود", icon: "◈", tone: "green" },
                { label: "أرصدة النقدية والبنوك", value: "—", note: "الحسابات النقدية المرتبطة", icon: "▣", tone: "purple" },
              ].map((metric) => <article className="metric-card" key={metric.label}>
                <div className="metric-top"><span>{metric.label}</span><i className={`metric-icon ${metric.tone}`}>{metric.icon}</i></div>
                <strong className="metric-value">{metric.value}</strong><small>{metric.note}</small>
              </article>)}
            </div>
            <div className="section-heading module-heading"><div><h2>وحدات النظام</h2><p>اختر الوحدة للبدء في إدارة أعمالك</p></div><span className="module-count">6 وحدات أساسية</span></div>
            <div className="modules-grid">{modules.map((module) => <button className="module-card" key={module.title} onClick={() => setActive(module.title.includes("المحاسبة") ? "accounts" : "setup")}>
              <i className={`module-icon ${module.tone}`}>{module.icon}</i><div><strong>{module.title}</strong><small>{module.detail}</small></div><span className="module-arrow">←</span>
            </button>)}</div>
            <section className="activity-panel"><div className="activity-title"><div><h2>آخر العمليات</h2><p>ملخص آخر الأنشطة المسجلة</p></div><span className="empty-indicator">لا توجد عمليات بعد</span></div>
              <div className="empty-state"><div className="empty-graphic">◷</div><strong>مساحة العمل جاهزة</strong><p>ستظهر هنا القيود والعمليات بمجرد ربط قاعدة البيانات وإدخال أول معاملة.</p></div>
            </section>
          </> : <section className="work-panel">
            <div className="work-panel-head"><div><h2>{current?.label}</h2><p>هذه الشاشة جزء من الواجهة الجديدة، وسيتم ربطها بخدمات النظام تباعاً.</p></div><span className="coming-soon">قيد التطوير</span></div>
            {active === "journals" && <div className="table-placeholder"><div className="table-toolbar"><span>سجل قيود اليومية</span><button className="primary-button small" disabled>＋ قيد جديد قريباً</button></div><div className="table-head"><span>رقم القيد</span><span>التاريخ</span><span>البيان</span><span>المصدر</span><span>الحالة</span></div><div className="table-empty">لا توجد قيود معروضة — سيتم جلب القيود من API المحاسبة.</div></div>}
            {active === "accounts" && <div className="table-placeholder"><div className="table-toolbar"><span>دليل الحسابات</span><span className="module-count">شجرة حسابات متعددة المستويات</span></div><div className="table-head"><span>كود الحساب</span><span>اسم الحساب</span><span>التصنيف</span><span>المستوى</span><span>الحالة</span></div><div className="table-empty">سيتم تحميل شجرة الحسابات من API المحاسبة.</div></div>}
          </section>}
          <footer className="page-footer"><span>© 2026 NEXUS ERP</span><span><i /> النظام يعمل في بيئة التطوير</span><span>الإصدار التأسيسي 0.1.0</span></footer>
        </div>
      </main>
    </div>
  );
}
