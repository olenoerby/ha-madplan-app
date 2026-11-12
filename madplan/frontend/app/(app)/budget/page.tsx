"use client";

import { useState } from "react";
import { ColumnChart, Meter, severityFor, StatTile } from "@/components/charts";
import { Button, Card, Field, Loading, NumberField, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { dayShort, kr, monthLong, monthShort, number, todayIso } from "@/lib/format";
import { useAction, useApi } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { BudgetData, BudgetPeriod, Expense, HouseholdSettings } from "@/lib/types";

function PeriodCard({ title, period }: { title: string; period: BudgetPeriod }) {
  const { t, locale } = useI18n();
  const ratio = period.budget ? period.expected / period.budget : 0;
  const severity = severityFor(ratio);
  return (
    <Card title={title}>
      <div className="grid-3">
        <StatTile label={t("budget.expected")} value={kr(period.expected, locale)} sub={period.budget ? t("budget.of", { budget: kr(period.budget, locale) }) : t("budget.noBudget")} />
        <StatTile label={t("budget.spent")} value={kr(period.spent, locale)} sub={`${t("budget.planned")}: ${kr(period.planned, locale)}`} />
        <StatTile label={t("budget.savings")} value={kr(period.savings, locale)} />
      </div>
      {period.budget ? (
        <Meter
          ratio={ratio}
          severity={severity}
          label={period.remaining != null && period.remaining >= 0 ? t("budget.left", { amount: kr(period.remaining, locale) }) : t("budget.over", { amount: kr(-(period.remaining ?? 0), locale) })}
          statusLabel={severity === "critical" ? t("budget.overShort") : severity === "warning" ? t("budget.close") : t("budget.ok")}
        />
      ) : null}
    </Card>
  );
}

export default function BudgetPage() {
  const { t, locale } = useI18n();
  const { data, reload } = useApi<BudgetData>("/budget");
  const { data: household } = useApi<{ settings: HouseholdSettings }>("/household");
  const { data: expenses, reload: reloadExpenses } = useApi<Expense[]>("/expenses");
  const { busy, run } = useAction();
  const [day, setDay] = useState(todayIso());
  const [amount, setAmount] = useState<number | null>(null);
  const [note, setNote] = useState("");

  if (!data) return <Loading />;
  const economy = data.economy;
  const change = economy.change_percent;
  const money = (value: number) => kr(value, locale);

  const add = () =>
    run(async () => {
      await api("/expenses", { body: { day, amount, note: note.trim() || null } });
      setAmount(null);
      setNote("");
      await Promise.all([reload(), reloadExpenses()]);
    });

  return (
    <>
      <PageHead title={t("budget.title")} subtitle={t("budget.subtitle")} />
      <div className="grid-2">
        <PeriodCard title={t("budget.week")} period={data.week} />
        <PeriodCard title={t("budget.month")} period={data.month} />
      </div>

      <Card title={t("budget.economy")}>
        {change === null ? (
          <p className="muted">{t("budget.needLastYear")}</p>
        ) : (
          <p className="strong">
            {change < 0
              ? t("budget.lessThan", { percent: number(-change, locale, 1) })
              : change > 0
                ? t("budget.moreThan", { percent: number(change, locale, 1) })
                : t("budget.sameAs")}
          </p>
        )}
        <div className="grid-4">
          <StatTile
            label={t("budget.yearToDate")}
            value={money(economy.this_year_to_date)}
            delta={change === null || change === 0 ? undefined : { text: `${number(Math.abs(change), locale, 1)} %`, up: change > 0, good: change < 0 }}
          />
          <StatTile label={t("budget.lastYear")} value={money(economy.last_year_same_period)} />
          <StatTile label={t("budget.savedYear")} value={money(economy.saved_this_year)} />
        </div>
      </Card>

      <Card>
        <ColumnChart
          title={t("budget.monthly")}
          subtitle={t("budget.monthlySub")}
          format={money}
          axisFormat={(value) => number(value, locale)}
          reference={household?.settings.budget_monthly ? { value: household.settings.budget_monthly, label: `${t("budget.refLabel")} ${money(household.settings.budget_monthly)}` } : null}
          data={economy.months.map((row) => ({
            key: row.month,
            label: monthShort(row.month, locale),
            fullLabel: monthLong(row.month, locale),
            value: row.spent,
            detail: row.saved > 0 ? t("budget.savedTooltip", { amount: money(row.saved) }) : undefined,
          }))}
          table={
            <table>
              <thead>
                <tr>
                  <th>{t("budget.tableMonth")}</th>
                  <th className="num">{t("budget.tableSpent")}</th>
                  <th className="num">{t("budget.tableSaved")}</th>
                </tr>
              </thead>
              <tbody>
                {economy.months.map((row) => (
                  <tr key={row.month}>
                    <td>{monthLong(row.month, locale)}</td>
                    <td className="num">{money(row.spent)}</td>
                    <td className="num">{money(row.saved)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          }
        />
      </Card>

      <Card title={t("budget.expenses")}>
        <div className="form-grid">
          <Field label={t("budget.date")}>
            <input type="date" value={day} onChange={(e) => setDay(e.target.value)} />
          </Field>
          <NumberField label={t("budget.amount")} value={amount} onChange={setAmount} />
          <Field label={t("budget.note")}>
            <input value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} />
          </Field>
        </div>
        <div>
          <Button variant="primary" disabled={busy || !amount || !day} onClick={() => void add()}>
            {t("budget.addExpense")}
          </Button>
        </div>
        {expenses && expenses.length === 0 && <p className="muted">{t("budget.expensesEmpty")}</p>}
        {expenses && expenses.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("budget.date")}</th>
                  <th>{t("budget.note")}</th>
                  <th className="num">{t("budget.amount")}</th>
                  <th className="num">{t("budget.savings")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {expenses.map((expense) => (
                  <tr key={expense.id}>
                    <td className="nowrap">{dayShort(expense.day, locale)}</td>
                    <td>{expense.note === "receipt" || expense.note === "estimate" ? t(`budget.note.${expense.note}` as TranslationKey) : expense.note}</td>
                    <td className="num">{money(expense.amount)}</td>
                    <td className="num">{money(expense.savings)}</td>
                    <td className="right">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="danger"
                        onClick={() =>
                          void run(async () => {
                            await api(`/expenses/${expense.id}`, { method: "DELETE" });
                            await Promise.all([reload(), reloadExpenses()]);
                          })
                        }
                      >
                        {t("common.delete")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
