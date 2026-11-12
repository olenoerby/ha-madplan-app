"use client";

import { Card, Empty, Icon, Loading, PageHead } from "@/components/ui";
import { dayLong } from "@/lib/format";
import { useApi, useMe } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";

interface CalendarData {
  meals: { id: number; date: string; meal_type: string; title: string; status: string; sync_status: string }[];
  mealie: { date: string; meal_type: string; title: string; mealie_slug: string | null; url: string | null; from_madplan: boolean }[];
  /** Mealie's own meal planner. Null without Mealie. */
  mealie_planner_url: string | null;
}

// Addresses come from the server's configuration, but only ordinary web links become links.
const isWebLink = (url: string | null | undefined): url is string => Boolean(url && /^https?:\/\//i.test(url));

const KNOWN_TYPES = new Set(["breakfast", "lunch", "dinner", "snack", "drink"]);

export default function CalendarPage() {
  const me = useMe();
  const { t, locale } = useI18n();
  const { data } = useApi<CalendarData>("/calendar");
  if (!data) return <Loading />;

  type Row = { key: string; type: string; title: string; note?: string; status?: string; synced?: boolean; url?: string | null };
  const days = new Map<string, Row[]>();
  const add = (date: string, row: Row) =>
    days.set(date, [...(days.get(date) ?? []), row]);
  for (const meal of data.meals) {
    add(meal.date, { key: `m${meal.id}`, type: meal.meal_type, title: meal.title, status: meal.status, synced: meal.sync_status === "synced" });
  }
  data.mealie.forEach((entry, index) => {
    add(entry.date, { key: `e${index}`, type: entry.meal_type, title: entry.title, url: entry.url, note: entry.from_madplan ? t("calendar.fromOldPlan") : t("calendar.fromMealie") });
  });
  const ordered = [...days.entries()].sort(([a], [b]) => a.localeCompare(b));

  return (
    <>
      <PageHead
        title={t("calendar.title")}
        subtitle={t(me.setup.mealie ? "calendar.subtitle" : "calendar.subtitleNoMealie")}
        actions={
          me.setup.mealie && isWebLink(data.mealie_planner_url) ? (
            <a className="btn" href={data.mealie_planner_url} target="_blank" rel="noopener noreferrer">
              <Icon name="external" size={16} />
              {t("calendar.openMealie")}
            </a>
          ) : undefined
        }
      />
      {ordered.length === 0 && (
        <Card>
          <Empty>{t("calendar.empty")}</Empty>
        </Card>
      )}
      {ordered.map(([date, rows]) => (
        <Card key={date} title={dayLong(date, locale)}>
          <div>
            {rows.map((row) => (
              <div className="list-row" key={row.key}>
                <span className="small muted" style={{ minWidth: 76 }}>
                  {KNOWN_TYPES.has(row.type) ? t(`meal.${row.type}` as TranslationKey) : row.type}
                </span>
                <span className="grow strong">
                  {isWebLink(row.url) ? (
                    <a href={row.url} target="_blank" rel="noopener noreferrer" title={t("recipe.openInMealie")}>
                      {row.title}
                    </a>
                  ) : (
                    row.title
                  )}
                </span>
                {row.note && <span className="badge">{row.note}</span>}
                {row.status && <span className={`badge ${row.status === "accepted" ? "good" : ""}`}>{t(`plan.status.${row.status}` as TranslationKey)}</span>}
                {row.synced && <span className="badge good">{t("plan.sync.synced")}</span>}
              </div>
            ))}
          </div>
        </Card>
      ))}
    </>
  );
}
