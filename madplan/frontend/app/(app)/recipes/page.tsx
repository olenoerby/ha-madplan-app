"use client";

import { type FormEvent, useState } from "react";
import { PlanDishButton } from "@/components/PlanDish";
import { RecipeDialog, RecipeImage, RecipeLinkButton } from "@/components/RecipeDialog";
import { Button, Card, Empty, Icon, Loading, Notice, PageHead, Segmented } from "@/components/ui";
import { api } from "@/lib/api";
import { kr } from "@/lib/format";
import { useAction, useApi, useMe, useToast } from "@/lib/hooks";
import { errorText, useI18n, type TranslationKey } from "@/lib/i18n";
import type { DishSearchResult, MealType, NewRecipeCard, NewRecipes, Recipe, RecipeLink, RecipeSearchResult } from "@/lib/types";

const MEALS: MealType[] = ["dinner", "lunch", "breakfast", "snack", "drink"];
type Filter = MealType | "all";

export default function NewRecipesPage() {
  const me = useMe();
  const { t } = useI18n();
  const [filter, setFilter] = useState<Filter>("all");
  const { data, reload, error } = useApi<NewRecipes>(filter === "all" ? "/new-recipes" : `/new-recipes?meal_type=${filter}`);
  const [result, setResult] = useState<RecipeSearchResult | null>(null);
  const [wish, setWish] = useState("");
  const { busy: searching, run: runSearch } = useAction();

  const search = (event?: FormEvent) => {
    event?.preventDefault();
    void runSearch(async () => {
      setResult(await api<RecipeSearchResult>("/new-recipes/search", { body: { meal_types: filter === "all" ? null : [filter], wish: wish.trim() || null } }));
      await reload();
    });
  };

  return (
    <>
      <PageHead
        title={t("recipes.title")}
        subtitle={t(me.setup.mealie ? "recipes.subtitle" : "recipes.subtitleNoMealie")}
      />

      {data?.search_blocked && <Notice>{t(`recipes.blocked.${data.search_blocked}` as TranslationKey)}</Notice>}
      {result && <SearchOutcome result={result} />}
      {error && <Notice kind="critical">{t("common.error")}</Notice>}

      <FindDish />

      <Segmented
        value={filter}
        label={t("recipes.filter")}
        onChange={setFilter}
        options={[{ value: "all" as Filter, label: t("recipes.all") }, ...MEALS.map((value) => ({ value: value as Filter, label: t(`meal.${value}` as TranslationKey) }))]}
      />

      {/* A search for the tab that is open, with room to say what to look for. */}
      <form className="row tab-search" onSubmit={search}>
        <input
          className="grow"
          style={{ flexBasis: 220 }}
          maxLength={120}
          aria-label={t("recipes.wishLabel")}
          placeholder={t(`recipes.wish.${filter}` as TranslationKey)}
          value={wish}
          onChange={(e) => setWish(e.target.value)}
        />
        <Button
          type="submit"
          variant="primary"
          working={searching}
          workingTexts={[t("working.recipes1"), t("working.reading"), t("working.minute")]}
          disabled={!data || Boolean(data.search_blocked)}
        >
          <Icon name="sparkle" />
          {t(`recipes.searchFor.${filter}` as TranslationKey)}
        </Button>
      </form>

      {!data && !error && <Loading />}
      {data && data.recipes.length === 0 && (
        <Card>
          <Empty>{data.offers_known ? t("recipes.empty") : t("recipes.emptyNoOffers")}</Empty>
        </Card>
      )}
      {data && data.recipes.length > 0 && (
        <div className="recipe-grid">
          {data.recipes.map((recipe) => (
            <RecipeCard key={recipe.id} recipe={recipe} canAdd={data.mealie_connected && data.mealie_sync_enabled} />
          ))}
        </div>
      )}
      {data && (data.others?.length ?? 0) > 0 && (
        <>
          <h2 className="section-title">{t("recipes.others")}</h2>
          <p className="small muted">{t("recipes.othersHint")}</p>
          <div className="recipe-grid">
            {data.others?.map((recipe) => (
              <RecipeCard key={recipe.id} recipe={recipe} canAdd={data.mealie_connected && data.mealie_sync_enabled} />
            ))}
          </div>
        </>
      )}

      <AddLink onAdded={reload} />
    </>
  );
}

function SearchOutcome({ result }: { result: RecipeSearchResult }) {
  const { t } = useI18n();
  if (result.no_offers) return <Notice>{t("recipes.result.noOffers")}</Notice>;
  // No address at all, or only addresses that led nowhere: the agent did not search the web.
  const guessed = result.found === 0 && result.refused > 0 && Object.keys(result.refused_why).every((why) => why === "unreachable" || why === "no_recipe");
  if (result.links === 0 || guessed) return <Notice kind="warning">{t("recipes.result.noSearch")}</Notice>;
  return (
    <Notice kind={result.found > 0 ? "good" : undefined}>
      {t("recipes.result.found", { count: result.found })}
      {result.refused > 0 && ` ${t("recipes.result.refused", { count: result.refused })}`}
      <RefusedWhy why={result.refused_why} />
    </Notice>
  );
}

/** Why pages were refused, one line each, so a pattern can be seen. */
function RefusedWhy({ why }: { why: Record<string, number> }) {
  const { t } = useI18n();
  const rows = Object.entries(why).sort(([, a], [, b]) => b - a);
  if (rows.length === 0) return null;
  return (
    <span className="small" style={{ display: "block", marginTop: 6 }}>
      {t("recipes.why.title")}{" "}
      {rows
        .map(([code, count]) => {
          const key = `recipes.why.${code}` as TranslationKey;
          return t(key, { count }) === key ? `${count} ${code}` : t(key, { count });
        })
        .join(", ")}
      .
    </span>
  );
}

function RecipeCard({ recipe, canAdd }: { recipe: NewRecipeCard; canAdd: boolean }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const { busy: adding, run: runAdd } = useAction();
  const [open, setOpen] = useState(false);
  const [added, setAdded] = useState<RecipeLink | null>(null);
  const [hidden, setHidden] = useState(false);

  if (hidden) {
    return (
      <Card className="recipe-card">
        <p className="muted small">{t("recipes.hidden", { title: recipe.title })}</p>
        <div className="row">
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await api(`/new-recipes/${recipe.id}/dismiss`, { method: "DELETE" });
                setHidden(false);
              })
            }
          >
            {t("recipes.undo")}
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <Card className="recipe-card">
      <RecipeImage url={recipe.image_url} />
      <button type="button" className="meal-title" onClick={() => setOpen(true)}>
        {recipe.title}
      </button>
      {recipe.description && <p className="small muted recipe-description">{recipe.description}</p>}
      <ul className="recipe-offers">
        {recipe.offers.map((offer) => (
          <li key={`${offer.offer}-${offer.store}`}>
            <span className="grow">
              {offer.offer}
              <span className="muted"> · {offer.store}</span>
              {offer.membership && <span className="muted"> · {t("recipes.membership")}</span>}
            </span>
            <span className="num strong">{kr(offer.price, locale)}</span>
            {offer.before != null && offer.before > offer.price && <s className="small muted num">{kr(offer.before, locale)}</s>}
          </li>
        ))}
        {recipe.more_offers > 0 && <li className="muted small">{t("recipes.moreOffers", { count: recipe.more_offers })}</li>}
      </ul>
      <p className="small muted">{t("recipes.cost", { cost: kr(recipe.cost, locale), people: recipe.people })}</p>
      {recipe.disliked.length > 0 && <p className="small muted">{t("recipes.disliked", { items: recipe.disliked.join(", ") })}</p>}
      <div className="row recipe-actions">
        {recipe.in_mealie && !added ? (
          <>
            <span className="badge good">
              <Icon name="check" size={13} />
              {t("recipes.inMealie")}
            </span>
            <RecipeLinkButton link={recipe.link} />
            <PlanDishButton recipeId={recipe.id} title={recipe.title} mealTypes={recipe.meal_types} variant="primary" />
          </>
        ) : added ? (
          <>
            <span className="badge good">
              <Icon name="check" size={13} />
              {t("recipes.added")}
            </span>
            <RecipeLinkButton link={added} />
            <PlanDishButton recipeId={recipe.id} title={recipe.title} mealTypes={recipe.meal_types} />
          </>
        ) : (
          <>
            <RecipeLinkButton link={recipe.link} />
            <PlanDishButton recipeId={recipe.id} title={recipe.title} mealTypes={recipe.meal_types} variant="primary" />
            {canAdd && (
            <Button
              size="sm"
              working={adding}
              workingTexts={[t("working.add1"), t("working.add2")]}
              disabled={busy}
              onClick={() =>
                void runAdd(async () => {
                  const card = await api<{ link: RecipeLink | null }>(`/new-recipes/${recipe.id}/to-mealie`, { method: "POST" });
                  setAdded(card.link ?? { kind: "mealie", url: "", label: "Mealie" });
                })
              }
            >
              {t("recipes.add")}
            </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              disabled={busy || adding}
              onClick={() =>
                void run(async () => {
                  await api(`/new-recipes/${recipe.id}/dismiss`, { method: "POST" });
                  setHidden(true);
                })
              }
            >
              {t("recipes.dismiss")}
            </Button>
          </>
        )}
      </div>
      {open && <RecipeDialog id={recipe.id} onClose={() => setOpen(false)} />}
    </Card>
  );
}

function AddLink({ onAdded }: { onAdded: () => Promise<void> }) {
  const { t } = useI18n();
  const { show } = useToast();
  const { busy, run } = useAction();
  const [url, setUrl] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const card = await api<{ title: string }>("/new-recipes/from-link", { body: { url: url.trim() } });
      show(t("recipes.link.done", { title: card.title }));
      setUrl("");
      await onAdded();
    });
  };

  return (
    <Card title={t("recipes.link.title")}>
      <p className="small muted">{t("recipes.link.hint")}</p>
      <form className="row" onSubmit={submit}>
        <input className="grow" style={{ flexBasis: 240 }} type="url" required aria-label={t("recipes.link.title")} placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)} />
        <Button type="submit" working={busy} workingTexts={[t("working.link1"), t("working.link2")]} disabled={url.trim().length < 8}>
          {t("recipes.link.add")}
        </Button>
      </form>
    </Card>
  );
}

/** Any of the household's dishes, from Mealie or found before, to put on a day, or versions of it found online. */
function FindDish() {
  const { t, locale } = useI18n();
  const me = useMe();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [online, setOnline] = useState<DishSearchResult | null>(null);
  const [onlineError, setOnlineError] = useState<string | null>(null);
  const { busy: searching, run } = useAction();
  const searchOnline = () =>
    void run(async () => {
      setOnline(null);
      setOnlineError(null);
      try {
        setOnline(await api<DishSearchResult>("/new-recipes/dish", { body: { dish: query } }));
      } catch (caught) {
        setOnlineError(errorText(t, caught));
      }
    });
  const { data: found, loading } = useApi<Recipe[]>(query ? `/recipes?q=${encodeURIComponent(query)}` : null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setQuery(draft.trim());
    setOnline(null);
    setOnlineError(null);
  };

  return (
    <Card title={t("pick.findTitle")}>
      <p className="small muted">{t("pick.findHint")}</p>
      <form className="row" onSubmit={submit}>
        <input className="grow" style={{ flexBasis: 220 }} aria-label={t("pick.findTitle")} placeholder={t("pick.findPlaceholder")} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <Button type="submit" disabled={draft.trim().length < 2}>
          {t("offers.searchButton")}
        </Button>
      </form>
      {query && loading && !found && <Loading />}
      {query && found && found.length === 0 && <p className="muted small">{t("pick.findNone")}</p>}
      {query &&
        found?.slice(0, 12).map((recipe) => (
          <div className="list-row" key={recipe.id}>
            <span className="grow">{locale === "en" && recipe.title_en ? recipe.title_en : recipe.title}</span>
            <RecipeLinkButton link={recipe.link} />
            {recipe.allergy_conflict ? (
              <span className="badge critical">{t("pick.allergyBadge", { item: recipe.allergy_conflict })}</span>
            ) : (
              <PlanDishButton recipeId={recipe.id} title={recipe.title} mealTypes={recipe.meal_types} />
            )}
          </div>
        ))}
      {query && me.setup.ai && (
        <div className="stack-sm" style={{ marginTop: 8 }}>
          <div className="row">
            <Button working={searching} workingTexts={[t("working.recipes1"), t("working.reading"), t("working.minute")]} onClick={searchOnline}>
              <Icon name="sparkle" />
              {t("pick.findOnline", { dish: query })}
            </Button>
          </div>
          {onlineError && <Notice kind="critical">{onlineError}</Notice>}
          {online && online.recipes.length === 0 && (
            <Notice kind={online.links === 0 ? "warning" : undefined}>
              {online.links === 0 ? t("recipes.result.noSearch") : t("pick.findOnlineNone")}
              <RefusedWhy why={online.refused_why} />
            </Notice>
          )}
          {online && online.recipes.length > 0 && (
            <>
              <p className="small muted">{t("pick.findOnlineHint")}</p>
              <div className="recipe-grid">
                {online.recipes.map((recipe) => (
                  <RecipeCard key={recipe.id} recipe={recipe} canAdd={online.mealie_connected} />
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}
