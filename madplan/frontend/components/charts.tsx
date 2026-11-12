"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Icon } from "@/components/ui";
import { kr } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import type { ShoppingSummary } from "@/lib/types";

/* Figures and charts, following the data-viz method: thin marks, recessive axes, sparing direct
   labels, one tooltip on hover and keyboard focus, and a table view as the accessible twin. */

export function StatTile({ label, value, sub, delta }: { label: string; value: ReactNode; sub?: ReactNode; delta?: { text: string; good: boolean; up: boolean } }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {delta && (
        <span className={`small row ${delta.good ? "delta-good" : "delta-bad"}`} style={{ gap: 3 }}>
          <Icon name={delta.up ? "up" : "down"} size={14} />
          {delta.text}
        </span>
      )}
      {sub && <span className="tile-sub">{sub}</span>}
    </div>
  );
}

/** What a trip saves, saying plainly how much of that rests on our own estimate. */
export function SavingsTile({ summary }: { summary: ShoppingSummary }) {
  const { t, locale } = useI18n();
  if (summary.real_prices === 0) return <StatTile label={t("plan.savings")} value="–" sub={t("plan.noSavingsYet")} />;
  const allMeasured = summary.savings - summary.savings_measured < 0.5;
  return (
    <StatTile
      label={allMeasured ? t("plan.savings") : t("plan.savingsApprox")}
      value={kr(summary.savings, locale)}
      sub={
        allMeasured
          ? t("plan.normalTotal", { amount: kr(summary.normal_total, locale) })
          : summary.savings_measured > 0
            ? t("plan.savingsBasis", { measured: kr(summary.savings_measured, locale) })
            : t("plan.savingsAllEstimate")
      }
    />
  );
}

/** One ratio against a limit. Severity is carried by the fill, and always stated in words too. */
export function Meter({ ratio, label, statusLabel, severity }: { ratio: number; label: string; statusLabel?: string; severity: "ok" | "warning" | "critical" }) {
  const percent = Math.round(Math.max(0, Math.min(1, ratio)) * 100);
  return (
    <div className={`meter ${severity === "ok" ? "" : severity}`}>
      <div className="meter-track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={label}>
        <div className="meter-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="row between small">
        <span className="muted">{label}</span>
        {statusLabel && (
          <span className="row" style={{ gap: 4 }}>
            {severity !== "ok" && <Icon name="alert" size={14} />}
            {statusLabel}
          </span>
        )}
      </div>
    </div>
  );
}

export function severityFor(ratio: number): "ok" | "warning" | "critical" {
  if (ratio > 1) return "critical";
  return ratio >= 0.85 ? "warning" : "ok";
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    setWidth(element.clientWidth);
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/** Clean axis numbers: 1, 2 or 5 times a power of ten. */
export function niceTicks(low: number, high: number, count = 4): number[] {
  if (!(high > low)) return [low, low + 1];
  const rough = (high - low) / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const unit = [1, 2, 5, 10].map((m) => m * power).find((step) => step >= rough) ?? rough;
  const ticks: number[] = [];
  for (let tick = Math.floor(low / unit) * unit; tick < high + unit * 0.999; tick += unit) {
    ticks.push(Number(tick.toFixed(6)));
  }
  return ticks;
}

function Figure({ title, subtitle, table, children }: { title: string; subtitle?: string; table: ReactNode; children: ReactNode }) {
  const { t } = useI18n();
  const [asTable, setAsTable] = useState(false);
  return (
    <figure className="chart">
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="muted small">{subtitle}</p>}
        </div>
        <Button variant="ghost" size="sm" className="no-print" onClick={() => setAsTable((v) => !v)}>
          {asTable ? t("chart.chart") : t("chart.table")}
        </Button>
      </div>
      {asTable ? <div className="table-wrap">{table}</div> : children}
    </figure>
  );
}

const MARGIN = { top: 22, right: 8, bottom: 26, left: 46 };

function Tooltip({ x, y, value, label, detail }: { x: number; y: number; value: string; label: string; detail?: string }) {
  return (
    <div className="chart-tooltip" style={{ left: x, top: y - 8 }}>
      <strong>{value}</strong>
      <span>{label}</span>
      {detail && <span>{detail}</span>}
    </div>
  );
}

function clampTooltip(x: number, width: number) {
  return Math.max(60, Math.min(width - 60, x));
}

export interface Column {
  key: string;
  label: string;
  fullLabel: string;
  value: number;
  detail?: string;
}

/** One series of columns from a single baseline, one colour, with a hover readout per column. */
export function ColumnChart({
  title,
  subtitle,
  data,
  format,
  axisFormat,
  reference,
  table,
}: {
  title: string;
  subtitle?: string;
  data: Column[];
  format: (value: number) => string;
  axisFormat: (value: number) => string;
  reference?: { value: number; label: string } | null;
  table: ReactNode;
}) {
  const { ref, width } = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const height = 240;
  const plotHeight = height - MARGIN.top - MARGIN.bottom;
  const plotWidth = Math.max(0, width - MARGIN.left - MARGIN.right);
  const highest = Math.max(...data.map((d) => d.value), reference?.value ?? 0, 1);
  const ticks = niceTicks(0, highest);
  const top = ticks[ticks.length - 1];
  const y = (value: number) => MARGIN.top + plotHeight * (1 - value / top);
  const band = data.length ? plotWidth / data.length : 0;
  const barWidth = Math.min(24, Math.max(4, band - 4));
  const labelEvery = Math.max(1, Math.ceil(30 / Math.max(band, 1)));
  const center = (index: number) => MARGIN.left + band * index + band / 2;

  // Direct labels only where the story is: the highest column and the newest one with data.
  let maxIndex = -1;
  let lastIndex = -1;
  data.forEach((d, i) => {
    if (d.value > 0) lastIndex = i;
    if (d.value > 0 && (maxIndex < 0 || d.value > data[maxIndex].value)) maxIndex = i;
  });
  const directLabels = new Set([maxIndex, lastIndex].filter((i) => i >= 0));

  return (
    <Figure title={title} subtitle={subtitle} table={table}>
      <div ref={ref} style={{ position: "relative" }}>
        {width > 0 && (
          <svg width={width} height={height} role="img" aria-label={title}>
            {ticks.map((tick) => (
              <g key={tick}>
                <line x1={MARGIN.left} x2={width - MARGIN.right} y1={y(tick)} y2={y(tick)} stroke={tick === 0 ? "var(--axis)" : "var(--grid)"} />
                <text x={MARGIN.left - 8} y={y(tick) + 4} textAnchor="end">
                  {axisFormat(tick)}
                </text>
              </g>
            ))}
            {data.map((d, i) => {
              const x = center(i) - barWidth / 2;
              const barTop = y(d.value);
              const radius = Math.min(4, barWidth / 2, Math.max(0, y(0) - barTop));
              return (
                <g
                  key={d.key}
                  className="bar-group"
                  tabIndex={0}
                  aria-label={`${d.fullLabel}: ${format(d.value)}${d.detail ? `, ${d.detail}` : ""}`}
                  onPointerEnter={() => setActive(i)}
                  onPointerLeave={() => setActive(null)}
                  onFocus={() => setActive(i)}
                  onBlur={() => setActive(null)}
                >
                  <rect className="bar-hit" x={MARGIN.left + band * i} y={MARGIN.top} width={band} height={plotHeight} fill="transparent" />
                  {d.value > 0 && (
                    <path
                      className="bar-mark"
                      fill="var(--series-1)"
                      d={`M${x},${y(0)}V${barTop + radius}Q${x},${barTop} ${x + radius},${barTop}H${x + barWidth - radius}Q${x + barWidth},${barTop} ${x + barWidth},${barTop + radius}V${y(0)}Z`}
                    />
                  )}
                  {directLabels.has(i) && band >= 16 && (
                    <text className="direct" x={center(i)} y={barTop - 6} textAnchor="middle">
                      {axisFormat(d.value)}
                    </text>
                  )}
                  {(data.length - 1 - i) % labelEvery === 0 && (
                    <text x={center(i)} y={height - 8} textAnchor="middle">
                      {d.label}
                    </text>
                  )}
                </g>
              );
            })}
            {reference && (
              <g pointerEvents="none">
                <line x1={MARGIN.left} x2={width - MARGIN.right} y1={y(reference.value)} y2={y(reference.value)} stroke="var(--ink-2)" />
                <text x={MARGIN.left + 4} y={y(reference.value) - 5}>
                  {reference.label}
                </text>
              </g>
            )}
          </svg>
        )}
        {active !== null && data[active] && (
          <Tooltip x={clampTooltip(center(active), width)} y={y(data[active].value)} value={format(data[active].value)} label={data[active].fullLabel} detail={data[active].detail} />
        )}
      </div>
    </Figure>
  );
}

export interface LinePoint {
  key: string;
  label: string;
  fullLabel: string;
  value: number;
}

/** One series over time. The crosshair snaps to the nearest point, by pointer or arrow keys. */
export function LineChart({
  title,
  subtitle,
  data,
  format,
  axisFormat,
  table,
}: {
  title: string;
  subtitle?: string;
  data: LinePoint[];
  format: (value: number) => string;
  axisFormat: (value: number) => string;
  table: ReactNode;
}) {
  const { ref, width } = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const height = 220;
  const margin = { ...MARGIN, right: 46 };
  const plotHeight = height - margin.top - margin.bottom;
  const plotWidth = Math.max(0, width - margin.left - margin.right);
  const values = data.map((d) => d.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const pad = high === low ? Math.max(1, high * 0.1) : 0;
  const ticks = niceTicks(Math.max(0, low - pad), high + pad);
  const bottom = ticks[0];
  const top = ticks[ticks.length - 1];
  const y = (value: number) => margin.top + plotHeight * (1 - (value - bottom) / (top - bottom || 1));
  const x = (index: number) => margin.left + (data.length > 1 ? (plotWidth * index) / (data.length - 1) : plotWidth / 2);
  const line = data.map((d, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(d.value)}`).join("");
  const area = data.length > 1 ? `${line}L${x(data.length - 1)},${y(bottom)}L${x(0)},${y(bottom)}Z` : "";
  const labelEvery = Math.max(1, Math.ceil(56 / Math.max(plotWidth / Math.max(data.length - 1, 1), 1)));
  const last = data.length - 1;

  const nearest = (clientX: number, element: Element) => {
    const offset = clientX - element.getBoundingClientRect().left;
    const step = data.length > 1 ? plotWidth / (data.length - 1) : plotWidth;
    return Math.max(0, Math.min(last, Math.round((offset - margin.left) / (step || 1))));
  };

  return (
    <Figure title={title} subtitle={subtitle} table={table}>
      <div ref={ref} style={{ position: "relative" }}>
        {width > 0 && data.length > 0 && (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={title}
            tabIndex={0}
            onPointerMove={(event) => setActive(nearest(event.clientX, event.currentTarget))}
            onPointerLeave={() => setActive(null)}
            onFocus={() => setActive(last)}
            onBlur={() => setActive(null)}
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft") setActive((a) => Math.max(0, (a ?? last) - 1));
              if (event.key === "ArrowRight") setActive((a) => Math.min(last, (a ?? last) + 1));
            }}
          >
            {ticks.map((tick) => (
              <g key={tick}>
                <line x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} stroke={tick === bottom ? "var(--axis)" : "var(--grid)"} />
                <text x={margin.left - 8} y={y(tick) + 4} textAnchor="end">
                  {axisFormat(tick)}
                </text>
              </g>
            ))}
            {data.map(
              (d, i) =>
                (last - i) % labelEvery === 0 && (
                  <text key={d.key} x={x(i)} y={height - 8} textAnchor="middle">
                    {d.label}
                  </text>
                ),
            )}
            {area && <path d={area} fill="var(--series-1)" opacity={0.1} />}
            {data.length > 1 && <path d={line} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
            {active !== null && <line x1={x(active)} x2={x(active)} y1={margin.top} y2={y(bottom)} stroke="var(--axis)" />}
            <circle cx={x(active ?? last)} cy={y(data[active ?? last].value)} r={5} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
            <text className="direct" x={x(last) + 8} y={y(data[last].value) + 4}>
              {axisFormat(data[last].value)}
            </text>
          </svg>
        )}
        {active !== null && data[active] && (
          <Tooltip x={clampTooltip(x(active), width)} y={y(data[active].value)} value={format(data[active].value)} label={data[active].fullLabel} />
        )}
      </div>
    </Figure>
  );
}
