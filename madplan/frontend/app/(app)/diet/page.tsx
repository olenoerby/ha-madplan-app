"use client";

import { useState } from "react";
import { StatTile } from "@/components/charts";
import { Button, Card, Empty, Icon, Loading, Notice, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { number } from "@/lib/format";
import { useAction, useApi } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { DietReport } from "@/lib/types";

export default function DietPage() {
  const { t, locale } = useI18n();
  const { data: report } = useApi<DietReport>("/diet/report");
  const { busy, run } = useAction();
  const [advice, setAdvice] = useState<string | null>(null);

  if (!report) return <Loading />;
  const proteins = Object.entries(report.proteins).sort(([, a], [, b]) => b - a);
  const totalProtein = proteins.reduce((sum, [, count]) => sum + count, 0);

  return (
    <>
      <PageHead title={t("diet.title")} subtitle={t("diet.subtitle")} />
      {!report.coach_enabled && <Notice>{t("diet.off")}</Notice>}
      {report.meals === 0 ? (
        <Card>
          <Empty>{t("diet.noData")}</Empty>
        </Card>
      ) : (
        <>
          <div className="grid-4">
            <StatTile label={t("diet.meals")} value={report.meals} sub={report.diet_mode ? t(`diet.mode.${report.diet_mode}` as TranslationKey) : undefined} />
            <StatTile label={t("diet.vegetarian")} value={report.vegetarian_meals} />
            <StatTile label={t("diet.fish")} value={report.fish_meals} />
            <StatTile label={t("diet.redMeat")} value={report.red_meat_meals} />
            {report.diet_mode && <StatTile label={t("diet.onDiet")} value={`${report.on_diet_meals}/${report.meals}`} />}
            <StatTile label={t("diet.bought")} value={report.items_bought} />
          </div>
          {proteins.length > 0 && (
            <Card>
              <div className="table-wrap">
                <table>
                  <tbody>
                    {proteins.map(([protein, count]) => (
                      <tr key={protein}>
                        <td>{t(`protein.${protein}` as TranslationKey)}</td>
                        <td className="num">{count}</td>
                        <td className="num muted">{number((count / totalProtein) * 100, locale)} %</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}
          {report.suggestions.length > 0 && (
            <div className="stack-sm">
              {report.suggestions.map((s) => (
                <Notice key={s.code}>{t(`diet.suggest.${s.code}` as TranslationKey, { count: s.count })}</Notice>
              ))}
            </div>
          )}
          {report.coach_enabled && (
            <Card>
              <div>
                <Button
                  working={busy}
                  workingTexts={[t("working.diet1"), t("working.diet2"), t("working.longer")]}
                  onClick={() =>
                    void run(async () => {
                      const result = await api<{ advice: string }>("/diet/advice", { method: "POST" });
                      setAdvice(result.advice);
                    })
                  }
                >
                  <Icon name="sparkle" />
                  {t("diet.advice")}
                </Button>
              </div>
              {advice && <p style={{ whiteSpace: "pre-wrap" }}>{advice}</p>}
              <p className="small muted">{t("diet.adviceHint")}</p>
            </Card>
          )}
        </>
      )}
    </>
  );
}
