import type { ReactNode } from "react";

type Props = { onClose?: () => void };

const details = [
  { label: "Product", value: "Admrail ERP" },
  { label: "Major Version", value: "Version 2 · V2" },
  { label: "Build", value: "0.1.0" },
  { label: "Year", value: "2026" },
];

function InfoCard({ title, children }: { title: string; children: ReactNode }) {
  return <article className="metric-card about-info-card"><div className="metric-top"><span>{title}</span><i className="metric-icon blue">A</i></div>{children}</article>;
}

export default function AboutPanel({ onClose }: Props) {
  return <section className="work-panel about-program-panel" aria-labelledby="about-program-title">
    <div className="work-panel-head">
      <div><p className="eyebrow">ADMRAIL ERP · PRODUCT INFORMATION</p><h2 id="about-program-title">About & Copyright</h2><p>Official product information, version details, and ownership.</p></div>
      {onClose && <button type="button" className="subtle-button" onClick={onClose}>Close</button>}
    </div>
    <div className="metrics-grid about-info-grid">
      {details.map(item => <InfoCard key={item.label} title={item.label}><strong className="metric-value about-info-value">{item.value}</strong></InfoCard>)}
    </div>
    <div className="about-ownership-grid">
      <section className="about-detail-card">
        <span className="about-detail-kicker">PROGRAMMING & DESIGN</span>
        <h3>Programming & Design</h3>
        <strong>Ahmed elshaer</strong>
      </section>
      <section className="about-detail-card">
        <span className="about-detail-kicker">TECHNOLOGY</span>
        <h3>Core Technologies</h3>
        <strong>Django · React · TypeScript · PostgreSQL</strong>
        <p>An internal business application designed to run on the company's local network, with a separate user interface, backend services, and database.</p>
      </section>
    </div>
    <div className="about-copyright">
      <strong>© 2026 Ahmed elshaer — Admrail ERP. All rights reserved.</strong>
      <span>Product ownership information is independent of the accounting company data managed within the system.</span>
    </div>
  </section>;
}
