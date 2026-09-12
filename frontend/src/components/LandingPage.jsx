import { useCallback, useEffect, useState } from "react";
import LandingSplash from "./LandingSplash.jsx";
import "../styles/landing.css";

const FEATURES = [
  {
    icon: "◎",
    title: "Prompt-driven search",
    text: "Describe your ideal prospect in plain language. Mark turns it into targeted LinkedIn discovery.",
  },
  {
    icon: "◈",
    title: "Rich lead profiles",
    text: "Name, role, company, location, email hints, activity signals, and AI-generated summaries.",
  },
  {
    icon: "⬡",
    title: "Smart filters",
    text: "Narrow by industry, geography, seniority, and more so every result fits your ICP.",
  },
  {
    icon: "◫",
    title: "Built for teams",
    text: "Individual accounts or organizations with shared searches and member management.",
  },
  {
    icon: "↗",
    title: "Export anywhere",
    text: "Download leads as CSV or XML and plug them straight into your CRM or outreach stack.",
  },
  {
    icon: "⚡",
    title: "No LinkedIn login",
    text: "Public web discovery only — no cookies, sessions, or account risk on your side.",
  },
];

const STEPS = [
  { num: "01", title: "Describe your audience", text: "Tell Mark who you want to reach — role, industry, region, or company type." },
  { num: "02", title: "Apply filters", text: "Fine-tune with structured filters so results match your ideal customer profile." },
  { num: "03", title: "Review & export", text: "Browse enriched leads, save searches, and export when you're ready to act." },
];

const LEAD_FIELDS = [
  "Name", "Email", "Phone", "Role", "Company", "Location",
  "LinkedIn", "Activity", "AI summary", "Relevance score",
];

export default function LandingPage({ onSignIn, onGetStarted }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [splashDone, setSplashDone] = useState(false);

  const finishSplash = useCallback(() => setSplashDone(true), []);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    document.body.classList.add("landing-active");
    return () => document.body.classList.remove("landing-active");
  }, []);

  const closeMenu = () => setMenuOpen(false);

  return (
    <>
    <div className={`landing${splashDone ? "" : " landing--under-splash"}`} aria-hidden={!splashDone}>
      <div className="landing-bg" aria-hidden="true">
        <div className="landing-orb landing-orb-a" />
        <div className="landing-orb landing-orb-b" />
        <div className="landing-grid" />
      </div>

      <header className={`landing-nav ${scrolled ? "is-scrolled" : ""}`}>
        <div className="landing-nav-inner">
          <a href="#top" className="landing-logo" onClick={closeMenu}>
            Mark
          </a>

          <nav className={`landing-links ${menuOpen ? "is-open" : ""}`} aria-label="Main">
            <a href="#features" onClick={closeMenu}>Features</a>
            <a href="#how-it-works" onClick={closeMenu}>How it works</a>
            <a href="#pricing" onClick={closeMenu}>Plans</a>
            <div className="landing-links-mobile-cta">
              <button type="button" className="btn-ghost" onClick={() => { closeMenu(); onSignIn(); }}>
                Sign in
              </button>
              <button type="button" className="btn-primary" onClick={() => { closeMenu(); onGetStarted(); }}>
                Get started
              </button>
            </div>
          </nav>

          <div className="landing-nav-actions">
            <button type="button" className="btn-ghost btn-nav-desktop" onClick={onSignIn}>
              Sign in
            </button>
            <button type="button" className="btn-primary btn-nav-desktop" onClick={onGetStarted}>
              Get started
            </button>
            <button
              type="button"
              className="landing-menu-toggle"
              aria-expanded={menuOpen}
              aria-label={menuOpen ? "Close menu" : "Open menu"}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <span />
              <span />
              <span />
            </button>
          </div>
        </div>
      </header>

      <main id="top">
        <section className="landing-hero">
          <div className="landing-container landing-hero-grid">
            <div className="landing-hero-copy">
              <p className="landing-eyebrow">Lead discovery platform</p>
              <h1>
                Find the right people
                <span className="landing-accent"> before your competitors do</span>
              </h1>
              <p className="landing-hero-lede">
                Mark helps marketers and sales teams discover LinkedIn professionals,
                enrich profiles with AI, and export qualified leads — without logging into LinkedIn.
              </p>
              <div className="landing-hero-cta">
                <button type="button" className="btn-primary btn-lg" onClick={onGetStarted}>
                  Start free
                </button>
                <button type="button" className="btn-outline btn-lg" onClick={onSignIn}>
                  Sign in
                </button>
              </div>
              <ul className="landing-trust">
                <li>No LinkedIn credentials</li>
                <li>Individual &amp; team accounts</li>
                <li>CSV &amp; XML export</li>
              </ul>
            </div>

            <div className="landing-hero-visual" aria-hidden="true">
              <div className="landing-mock">
                <div className="landing-mock-bar">
                  <span /><span /><span />
                  <em>Mark · Lead search</em>
                </div>
                <div className="landing-mock-body">
                  <div className="landing-mock-prompt">
                    <label>Search prompt</label>
                    <p>Marketing directors at SaaS companies in Austin, active on LinkedIn</p>
                  </div>
                  <div className="landing-mock-results">
                    {[
                      { name: "Sarah Chen", role: "VP Marketing · CloudStack", score: 94 },
                      { name: "Marcus Webb", role: "Director of Growth · NovaPay", score: 89 },
                      { name: "Elena Ruiz", role: "Head of Demand Gen · DataPulse", score: 87 },
                    ].map((lead) => (
                      <div key={lead.name} className="landing-mock-lead">
                        <div>
                          <strong>{lead.name}</strong>
                          <span>{lead.role}</span>
                        </div>
                        <b>{lead.score}%</b>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="landing-stats">
          <div className="landing-container landing-stats-grid">
            <div>
              <strong>12+</strong>
              <span>Data points per lead</span>
            </div>
            <div>
              <strong>AI</strong>
              <span>Profile summaries</span>
            </div>
            <div>
              <strong>Teams</strong>
              <span>Org workspaces</span>
            </div>
            <div>
              <strong>Instant</strong>
              <span>CSV &amp; XML export</span>
            </div>
          </div>
        </section>

        <section id="features" className="landing-section">
          <div className="landing-container">
            <header className="landing-section-head">
              <p className="landing-eyebrow">Features</p>
              <h2>Everything you need to go from prompt to pipeline</h2>
              <p>
                A focused toolkit for discovery, enrichment, and handoff — designed for
                marketers who care about quality over volume.
              </p>
            </header>
            <div className="landing-features">
              {FEATURES.map((f) => (
                <article key={f.title} className="landing-feature-card">
                  <span className="landing-feature-icon">{f.icon}</span>
                  <h3>{f.title}</h3>
                  <p>{f.text}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section id="how-it-works" className="landing-section landing-section-alt">
          <div className="landing-container">
            <header className="landing-section-head">
              <p className="landing-eyebrow">How it works</p>
              <h2>Three steps to qualified leads</h2>
            </header>
            <ol className="landing-steps">
              {STEPS.map((step) => (
                <li key={step.num}>
                  <span className="landing-step-num">{step.num}</span>
                  <div>
                    <h3>{step.title}</h3>
                    <p>{step.text}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="landing-section">
          <div className="landing-container landing-split">
            <div className="landing-split-copy">
              <p className="landing-eyebrow">Lead intelligence</p>
              <h2>Profiles that tell you who to reach out to — and why</h2>
              <p>
                Every lead includes structured fields plus an AI-written summary so you
                can prioritize outreach without opening a dozen tabs.
              </p>
              <button type="button" className="btn-primary" onClick={onGetStarted}>
                Try Mark free
              </button>
            </div>
            <div className="landing-field-cloud">
              {LEAD_FIELDS.map((field) => (
                <span key={field}>{field}</span>
              ))}
            </div>
          </div>
        </section>

        <section id="pricing" className="landing-section landing-section-alt">
          <div className="landing-container">
            <header className="landing-section-head">
              <p className="landing-eyebrow">Plans</p>
              <h2>Start free, scale with your team</h2>
            </header>
            <div className="landing-pricing">
              <article className="landing-price-card">
                <h3>Individual</h3>
                <p className="landing-price">Free to start</p>
                <ul>
                  <li>Personal workspace</li>
                  <li>Saved searches &amp; history</li>
                  <li>Lead export (CSV / XML)</li>
                  <li>AI profile summaries</li>
                </ul>
                <button type="button" className="btn-outline" onClick={onGetStarted}>
                  Create account
                </button>
              </article>
              <article className="landing-price-card landing-price-card-featured">
                <span className="landing-badge">For teams</span>
                <h3>Organization</h3>
                <p className="landing-price">Built for collaboration</p>
                <ul>
                  <li>Shared org workspace</li>
                  <li>Member invites &amp; roles</li>
                  <li>Centralized lead library</li>
                  <li>Everything in Individual</li>
                </ul>
                <button type="button" className="btn-primary" onClick={onGetStarted}>
                  Register organization
                </button>
              </article>
            </div>
          </div>
        </section>

        <section className="landing-cta-band">
          <div className="landing-container landing-cta-inner">
            <h2>Ready to discover your next customers?</h2>
            <p>Join Mark and turn a simple prompt into actionable leads in minutes.</p>
            <button type="button" className="btn-primary btn-lg btn-on-dark" onClick={onGetStarted}>
              Get started — it&apos;s free
            </button>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-container landing-footer-inner">
          <span className="landing-logo">Mark</span>
          <p>Lead discovery for modern marketing teams.</p>
          <nav aria-label="Footer">
            <button type="button" onClick={onSignIn}>Sign in</button>
            <button type="button" onClick={onGetStarted}>Register</button>
          </nav>
          <small>© {new Date().getFullYear()} Mark. All rights reserved.</small>
        </div>
      </footer>
    </div>
    {!splashDone && <LandingSplash onComplete={finishSplash} />}
    </>
  );
}
