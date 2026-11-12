"use client";

import Link from "next/link";
import { Meter, severityFor, StatTile } from "@/components/charts";
import { Button, Card, Empty, Icon, Notice, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { dayLong, dayShort, kr, quantity, todayIso } from "@/lib/format";
import { useApi, useMe } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { BudgetData, Notification, Plan, PriceIndex, SourceRow, WasteData } from "@/lib/types";

const MESSAGE_PAGES: Record<string, string> = {
  price_alert: "/alerts",
  buy_now: "/offers",
  waste: "/pantry",
  restock: "/pantry",
  plan_ready: "/plan",
  list_changed: "/shopping",
  meal_added: "/plan",
  mealie_unsent: "/plan",
};

export default function HomePage() {
  const { t, locale } = useI18n();
  const me = useMe();
  const today = todayIso();
  const { data: plan, loading: planLoading } = useApi<Plan | null>("/plans/current");
  const { data: budget } = useApi<BudgetData>("/budget");
  const { data: waste } = useApi<WasteData>(me.settings.features.waste_agent ? "/waste" : null);
  const { data: buyNow } = useApi<PriceIndex[]>("/offers/buy-now");
  const { data: notifications, reload: reloadNotifications } = useApi<Notification[]>("/notifications?unread_only=true");
  const messageText = (n: Notification) => {
    const data = n.data as Record<string, string | number | undefined>;
    if (n.kind === "waste" && data.dish) {
      return t("notification.waste_dish", { dish: (locale === "en" && data.dish_en) || data.dish, items: data.items });
    }
    if (n.kind === "plan_ready") {
      return t("notification.plan_ready", { start: dayShort(String(data.start), locale), end: dayShort(String(data.end), locale) });
    }
    if (n.kind === "meal_added") {
      return t("notification.meal_added", { by: data.by, title: data.title, day: dayLong(String(data.date), locale) });
    }
    if (n.kind === "mealie_unsent") {
      return t("notification.mealie_unsent", { count: data.count, title: data.title, day: dayLong(String(data.first), locale) });
    }
    if (n.kind === "list_changed") {
      return t("notification.list_changed", { old: kr(Number(data.old), locale), new: kr(Number(data.new), locale) });
    }
    return t(`notification.${n.kind}` as TranslationKey, data);
  };
  const { data: sources } = useApi<SourceRow[]>(me.role === "admin" ? "/admin/sources" : null);

  const todays = plan?.meals.filter((m) => m.date === today && m.status !== "rejected") ?? [];
  const goodDeals = buyNow?.filter((row) => row.verdict === "buy") ?? [];
  const week = budget?.week;
  const ratio = week?.budget ? week.expected / week.budget : 0;
  const severity = severityFor(ratio);
  const noOffers = sources && !sources.some((s) => s.offers > 0);
  // Mealie is optional, so only offers and AI are worth a word, and only to an administrator.
  const setupNeeded = me.role === "admin" && sources && (noOffers || !me.setup.ai);

  return (
    <>
      <PageHead title={t("home.greeting", { name: me.display_name })} subtitle={dayLong(today, locale)} />

      {setupNeeded && (
        <Card title={t("home.setup")}>
          <div className="stack-sm">
            {noOffers && <Notice>{t("home.setupOffers")}</Notice>}
            {!me.setup.mealie && <Notice>{t("home.setupMealie")}</Notice>}
            {!me.setup.ai && <Notice>{t("home.setupAi")}</Notice>}
          </div>
          <div>
            <Link className="btn" href="/admin">
              {t("nav.admin")}
            </Link>
          </div>
        </Card>
      )}

      <div className="grid-2">
        <Card title={t("home.todayMeals")} actions={<Link className="btn ghost sm" href="/plan">{t("nav.plan")}</Link>}>
          {todays.length > 0 ? (
            <div>
              {todays.map((meal) => (
                <div className="list-row" key={meal.id}>
                  <div className="grow">
                    <div className="strong">{locale === "en" && meal.title_en ? meal.title_en : meal.title}</div>
                    <div className="small muted">{t(`meal.${meal.meal_type}` as TranslationKey)}</div>
                  </div>
                  <span className={`badge ${meal.status === "accepted" ? "good" : ""}`}>{t(`plan.status.${meal.status}` as TranslationKey)}</span>
                </div>
              ))}
            </div>
          ) : (
            !planLoading && (
              <Empty action={<Link className="btn primary" href="/plan">{t("home.makePlan")}</Link>}>{t("home.noMealsToday")}</Empty>
            )
          )}
        </Card>

        {week && (
          <Card title={t("budget.week")} actions={<Link className="btn ghost sm" href="/budget">{t("nav.budget")}</Link>}>
            <div className="grid-4">
              <StatTile label={t("budget.expected")} value={kr(week.expected, locale)} sub={week.budget ? t("budget.of", { budget: kr(week.budget, locale) }) : t("budget.noBudget")} />
              <StatTile label={t("budget.savings")} value={kr(week.savings, locale)} />
            </div>
            {week.budget ? (
              <Meter
                ratio={ratio}
                severity={severity}
                label={week.remaining != null && week.remaining >= 0 ? t("budget.left", { amount: kr(week.remaining, locale) }) : t("budget.over", { amount: kr(-(week.remaining ?? 0), locale) })}
                statusLabel={severity === "critical" ? t("budget.overShort") : severity === "warning" ? t("budget.close") : t("budget.ok")}
              />
            ) : null}
          </Card>
        )}

        {waste && (
          <Card title={t("home.expiring")} actions={<Link className="btn ghost sm" href="/pantry">{t("nav.pantry")}</Link>}>
            {waste.items.length === 0 ? (
              <p className="muted">{t("home.nothingExpiring")}</p>
            ) : (
              <div>
                {waste.items.slice(0, 5).map((item) => (
                  <div className="list-row" key={item.id}>
                    <span className="grow">
                      {item.name} <span className="muted small">{quantity(item.quantity, item.unit, locale)}</span>
                    </span>
                    <span className={`badge ${item.days_left <= 1 ? "critical" : "warning"}`}>
                      {item.days_left < 0 ? t("pantry.expired") : item.days_left === 0 ? t("pantry.expiresToday") : item.days_left === 1 ? t("pantry.dayLeft") : t("pantry.daysLeft", { days: item.days_left })}
                    </span>
                  </div>
                ))}
                {waste.dishes[0] && (
                  <p className="small muted" style={{ marginTop: 8 }}>
                    {t("waste.dishes")}: {waste.dishes[0].title}
                  </p>
                )}
              </div>
            )}
          </Card>
        )}

        <Card title={t("home.buyNow")} actions={<Link className="btn ghost sm" href="/offers">{t("nav.offers")}</Link>}>
          {goodDeals.length > 0 ? (
            <div>
              {goodDeals.map((deal) => (
                <div className="list-row" key={deal.query}>
                  <span className="badge good">
                    <Icon name="check" size={14} />
                    {t("index.verdict.buy")}
                  </span>
                  <span className="grow strong">{deal.query}</span>
                  <span className="num">{kr(deal.current_pack_price, locale)}</span>
                  <span className="small muted">{deal.current_store}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="stack-sm">
              <p className="muted">{buyNow && buyNow.length > 0 ? t("home.buyNowEmpty") : t("home.noItems")}</p>
              {buyNow && buyNow.length === 0 && (
                <div>
                  <Link className="btn sm" href="/offers">
                    {t("home.addItems")}
                  </Link>
                </div>
              )}
            </div>
          )}
        </Card>

        <Card
          title={t("home.notifications")}
          actions={
            notifications && notifications.length > 0 ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void api("/notifications/read-all", { method: "POST" }).then(reloadNotifications)}
              >
                {t("home.readAll")}
              </Button>
            ) : undefined
          }
        >
          {notifications && notifications.length > 0 ? (
            <div>
              {notifications.slice(0, 8).map((n) => (
                <div className="list-row small" key={n.id}>
                  <Link href={MESSAGE_PAGES[n.kind] ?? "/"}>{messageText(n)}</Link>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">{t("home.noNotifications")}</p>
          )}
        </Card>
      </div>
    </>
  );
}
