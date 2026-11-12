"use client";

import { useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { type TranslationKey, useI18n } from "@/lib/i18n";

const ICONS: Record<string, ReactNode> = {
  home: <path d="M3 11.5 12 4l9 7.5M5.5 10v9.5h5v-5h3v5h5V10" />,
  plan: <path d="M4 5h16v15H4zM4 10h16M9 3v4M15 3v4M8 14h3M8 17h6" />,
  cart: <path d="M3 4h2.5l2.2 11h10.1l2-8H6.3M9 19.5a1 1 0 1 0 0 .01M17 19.5a1 1 0 1 0 0 .01" />,
  tag: <path d="M4 4h7.5L20 12.5 12.5 20 4 11.5zM8 8h.01" />,
  bell: <path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 2h-15zM10 21h4" />,
  pantry: <path d="M5 3h14v18H5zM5 12h14M9 7.5h6M9 16.5h6" />,
  chat: <path d="M4 5h16v11H9l-5 4zM8 9h8M8 12.5h5" />,
  budget: <path d="M4 20V10M10 20V4M16 20v-7M21 20H3" />,
  calendar: <path d="M4 5h16v15H4zM4 10h16M9 3v4M15 3v4" />,
  book: <path d="M12 6.5C10 5 7.5 4.5 4 4.5v14c3.5 0 6 .5 8 2 2-1.5 4.5-2 8-2v-14c-3.5 0-6 .5-8 2zM12 6.5v14" />,
  leaf: <path d="M5 19C5 10 11 5 20 4c0 9-5 15-14 15zM5 21c1-5 4-8 8-10" />,
  settings: <path d="M4 7h10M18 7h2M4 17h2M10 17h10M14 4.5v5h4v-5zM6 14.5v5h4v-5z" />,
  shield: <path d="M12 3l8 3v6c0 4.5-3.2 7.5-8 9-4.8-1.5-8-4.5-8-9V6z" />,
  more: <path d="M5 12h.01M12 12h.01M19 12h.01" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  alert: <path d="M12 4 2.5 20h19zM12 10v5M12 17.5h.01" />,
  info: <path d="M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 11v5M12 8h.01" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  up: <path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" />,
  down: <path d="M12 5v14M5.5 12.5 12 19l6.5-6.5" />,
  camera: <path d="M4 7h4l2-3h4l2 3h4v13H4zM12 10.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z" />,
  external: <path d="M14 4h6v6M20 4l-9 9M10 5H4v15h15v-6" />,
  sparkle: <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z" />,
};

export function Icon({ name, size = 18 }: { name: keyof typeof ICONS | string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

// Long enough to read, and short enough that something visibly happens.
const WORKING_TEXT_SECONDS = 5;

/**
 * Shows that something is under way: a spinner, a text and the seconds so far. Nothing reports how
 * far a model has come, so the texts follow the clock. They must not claim more than is known, and
 * the last one stays for as long as it takes.
 */
export function WorkingText({ texts }: { texts: string[] }) {
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <span className="working-text">
      <span className="spinner" aria-hidden="true" />
      <span aria-hidden="true">{texts[Math.min(Math.floor(seconds / WORKING_TEXT_SECONDS), texts.length - 1)]}</span>
      {seconds >= 2 && (
        <span className="num working-seconds" aria-hidden="true">
          {seconds} s
        </span>
      )}
      {/* Once only, as a text changing every few seconds would keep interrupting a screen reader. */}
      <span className="sr-only" role="status">
        {texts[0]}
      </span>
    </span>
  );
}

export function Button({
  variant,
  size,
  className,
  working,
  workingTexts,
  disabled,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger"; size?: "sm"; working?: boolean; workingTexts?: string[] }) {
  const classes = ["btn", variant, size, working && "working", className].filter(Boolean).join(" ");
  return (
    <button type="button" {...props} className={classes} disabled={disabled || working} aria-busy={working || undefined}>
      {working && workingTexts?.length ? <WorkingText texts={workingTexts} /> : children}
    </button>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={["card", className].filter(Boolean).join(" ")}>
      {(title || actions) && (
        <div className="card-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function PageHead({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions && <div className="row no-print">{actions}</div>}
    </header>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      {label}
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Check({ label, hint, checked, onChange, disabled }: { label: string; hint?: string; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>
        {label}
        {hint && <small>{hint}</small>}
      </span>
    </label>
  );
}

export function Notice({ kind, children }: { kind?: "warning" | "critical" | "good"; children: ReactNode }) {
  const icon = kind === "warning" || kind === "critical" ? "alert" : kind === "good" ? "check" : "info";
  return (
    <div className={`notice ${kind ?? ""}`} role={kind === "critical" ? "alert" : undefined}>
      <Icon name={icon} />
      <div>{children}</div>
    </div>
  );
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <p>{children}</p>
      {action}
    </div>
  );
}

export function Loading() {
  const { t } = useI18n();
  return <p className="muted">{t("common.loading")}</p>;
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} ref={dialog}>
        <div className="card-head">
          <h2 id={titleId}>{title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (value: T) => void; label?: string }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button type="button" key={option.value} aria-pressed={option.value === value} onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Stepper({ value, min = 0, max = 50, onChange, label }: { value: number; min?: number; max?: number; onChange: (value: number) => void; label: string }) {
  return (
    <span className="stepper" role="group" aria-label={label}>
      <button type="button" disabled={value <= min} onClick={() => onChange(value - 1)} aria-label={`${label} −`}>
        −
      </button>
      <output>{value > 0 && min < 0 ? `+${value}` : value}</output>
      <button type="button" disabled={value >= max} onClick={() => onChange(value + 1)} aria-label={`${label} +`}>
        +
      </button>
    </span>
  );
}

/** Edits a list of words, such as allergies. */
export function TagInput({ label, hint, values, onChange }: { label: string; hint?: string; values: string[]; onChange: (values: string[]) => void }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (value && !values.some((v) => v.toLowerCase() === value.toLowerCase())) onChange([...values, value]);
    setDraft("");
  };
  return (
    <div className="stack-sm">
      <label className="field">
        {label}
        <input
          value={draft}
          placeholder={t("settings.addTerm")}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={add}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
        />
        {hint && <span className="field-hint">{hint}</span>}
      </label>
      {values.length > 0 && (
        <div className="tags">
          {values.map((value) => (
            <span className="badge" key={value}>
              {value}
              <button type="button" onClick={() => onChange(values.filter((v) => v !== value))} aria-label={`${t("common.delete")} ${value}`}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export type Frequency = "rarely" | "often" | "always";
const FREQUENCIES: Frequency[] = ["rarely", "often", "always"];

/** Foods with how often they should come up: rarely, often or always. */
export function FrequencyInput({ values, onChange }: { values: Record<string, Frequency>; onChange: (values: Record<string, Frequency>) => void }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const [choice, setChoice] = useState<Frequency>("often");
  const add = () => {
    const value = draft.trim();
    if (!value) return;
    const rest = Object.fromEntries(Object.entries(values).filter(([term]) => term.toLowerCase() !== value.toLowerCase()));
    onChange({ ...rest, [value]: choice });
    setDraft("");
  };
  const label = (f: Frequency) => t(`frequency.${f}` as TranslationKey);
  return (
    <div className="stack-sm">
      <span className="field">
        {t("settings.frequencies")}
        <span className="row" style={{ flexWrap: "nowrap" }}>
          <input
            className="grow"
            aria-label={t("settings.frequencies")}
            value={draft}
            placeholder={t("settings.addTerm")}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <select aria-label={t("settings.frequency")} style={{ width: "auto" }} value={choice} onChange={(e) => setChoice(e.target.value as Frequency)}>
            {FREQUENCIES.map((f) => (
              <option key={f} value={f}>
                {label(f)}
              </option>
            ))}
          </select>
          <Button onClick={add} disabled={!draft.trim()}>
            {t("common.add")}
          </Button>
        </span>
        <span className="field-hint">{t("settings.frequenciesHint")}</span>
      </span>
      {Object.keys(values).length > 0 && (
        <div className="tags">
          {Object.entries(values).map(([term, frequency]) => (
            <span className={`badge ${frequency === "rarely" ? "warning" : "good"}`} key={term}>
              {term}: {label(frequency).toLowerCase()}
              <button
                type="button"
                onClick={() => onChange(Object.fromEntries(Object.entries(values).filter(([other]) => other !== term)))}
                aria-label={`${t("common.delete")} ${term}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function SaveBar({ busy, onSave, saved }: { busy?: boolean; onSave: () => void; saved?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="row">
      <Button variant="primary" disabled={busy} onClick={onSave}>
        {t("common.save")}
      </Button>
      {saved && (
        <span className="badge good">
          <Icon name="check" size={14} />
          {t("common.saved")}
        </span>
      )}
    </div>
  );
}

/** A number field where empty means "not set" rather than zero. */
export function NumberField({ label, hint, value, onChange, step, min }: { label: string; hint?: string; value: number | null; onChange: (value: number | null) => void; step?: number; min?: number }) {
  return (
    <Field label={label} hint={hint}>
      <input
        type="number"
        inputMode="decimal"
        value={value ?? ""}
        step={step ?? "any"}
        min={min ?? 0}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    </Field>
  );
}
