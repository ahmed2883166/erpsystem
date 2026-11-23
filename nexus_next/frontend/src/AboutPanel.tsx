import type { ReactNode } from "react";

type Props = { onClose?: () => void };

const details = [
  { label: "اسم البرنامج", value: "Admrail ERP" },
  { label: "الإصدار الرئيسي", value: "الإصدار الثاني · V2" },
  { label: "رقم البناء", value: "0.1.0" },
  { label: "سنة الإصدار", value: "2026" },
];

function InfoCard({ title, children }: { title: string; children: ReactNode }) {
  return <article className="metric-card about-info-card"><div className="metric-top"><span>{title}</span><i className="metric-icon blue">A</i></div>{children}</article>;
}

export default function AboutPanel({ onClose }: Props) {
  return <section className="work-panel about-program-panel" aria-labelledby="about-program-title">
    <div className="work-panel-head">
      <div><p className="eyebrow">ADMRAIL ERP · PRODUCT INFORMATION</p><h2 id="about-program-title">حول البرنامج وحقوق الملكية</h2><p>البيانات التعريفية الرسمية للمنتج وإصدار البرنامج وحقوقه.</p></div>
      {onClose && <button type="button" className="subtle-button" onClick={onClose}>إغلاق</button>}
    </div>
    <div className="metrics-grid about-info-grid">
      {details.map(item => <InfoCard key={item.label} title={item.label}><strong className="metric-value about-info-value">{item.value}</strong></InfoCard>)}
    </div>
    <div className="about-ownership-grid">
      <section className="about-detail-card">
        <span className="about-detail-kicker">PRODUCT OWNER & COPYRIGHT</span>
        <h3>مالك البرنامج وصاحب الحقوق</h3>
        <strong>أحمد مصطفى الشاعر</strong>
        <p>جميع حقوق الملكية الفكرية للمنتج واسم Admrail ERP والنسخة البرمجية محفوظة لصاحب الحقوق، ولا يجوز نسخ البرنامج أو إعادة توزيعه أو استغلاله تجارياً دون إذن كتابي.</p>
      </section>
      <section className="about-detail-card">
        <span className="about-detail-kicker">PRODUCT DESIGN</span>
        <h3>تصميم المنتج والإشراف</h3>
        <strong>أحمد مصطفى الشاعر</strong>
        <p>تصميم فكرة المنتج، وتحديد المتطلبات الوظيفية والمحاسبية، والإشراف على تجربة الاستخدام وتطوير الوحدات.</p>
      </section>
      <section className="about-detail-card">
        <span className="about-detail-kicker">SOFTWARE DEVELOPMENT</span>
        <h3>التطوير البرمجي</h3>
        <strong>فريق تطوير Admrail ERP</strong>
        <p>تطوير الواجهة والخدمات الخلفية وقاعدة البيانات والاختبارات. لا تُعرض بيانات اتصال شخصية أو اسم مبرمج فردي غير معتمد.</p>
      </section>
      <section className="about-detail-card">
        <span className="about-detail-kicker">TECHNOLOGY</span>
        <h3>التقنيات الأساسية</h3>
        <strong>Django · React · TypeScript · PostgreSQL</strong>
        <p>تطبيق ويب داخلي مصمم للتشغيل على شبكة الشركة المحلية، مع فصل واجهة الاستخدام عن الخدمات الخلفية وقاعدة البيانات.</p>
      </section>
    </div>
    <div className="about-copyright">
      <strong>© 2026 أحمد مصطفى الشاعر — Admrail ERP. جميع الحقوق محفوظة.</strong>
      <span>هذه بيانات ملكية البرنامج، وهي مستقلة عن بيانات الشركة المحاسبية التي يتم إعدادها داخل النظام.</span>
    </div>
  </section>;
}
