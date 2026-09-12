import { useEffect, useState } from "react";
import "../styles/landing-splash.css";

const WORD_STEPS = ["M", "Ma", "Mar", "Mark"];
const LETTER_MS = 304;
const START_MS = 336;
const DOT_IN_MS = 440;
const HOLD_MS = 384;
const FADE_MS = 360;

export default function LandingSplash({ onComplete }) {
  const [stepIndex, setStepIndex] = useState(-1);
  const [showEndDot, setShowEndDot] = useState(false);
  const [exiting, setExiting] = useState(false);

  useEffect(() => {
    document.body.classList.add("landing-splash-active");
    return () => document.body.classList.remove("landing-splash-active");
  }, []);

  useEffect(() => {
    const timers = [];

    timers.push(setTimeout(() => setStepIndex(0), START_MS));

    WORD_STEPS.slice(1).forEach((_, i) => {
      timers.push(setTimeout(() => setStepIndex(i + 1), START_MS + LETTER_MS * (i + 1)));
    });

    const typingEnd = START_MS + LETTER_MS * WORD_STEPS.length;
    timers.push(setTimeout(() => setShowEndDot(true), typingEnd));
    timers.push(setTimeout(() => setExiting(true), typingEnd + DOT_IN_MS + HOLD_MS));
    timers.push(setTimeout(() => onComplete(), typingEnd + DOT_IN_MS + HOLD_MS + FADE_MS));

    return () => timers.forEach(clearTimeout);
  }, [onComplete]);

  const text = stepIndex >= 0 ? WORD_STEPS[stepIndex] : "";

  return (
    <div
      className={`landing-splash${exiting ? " landing-splash--exit" : ""}`}
      aria-hidden={exiting}
      role="presentation"
    >
      <div className="landing-splash-bg" aria-hidden="true">
        <div className="landing-splash-orb landing-splash-orb-a" />
        <div className="landing-splash-orb landing-splash-orb-b" />
      </div>

      <div className="mark-loader" aria-label="Loading Mark">
        <div className="mark-loader-track">
          <span className="mark-text">{text}</span>
          {showEndDot && (
            <span className="mark-dot mark-dot--bounce mark-dot--at-end" />
          )}
        </div>
      </div>
    </div>
  );
}
