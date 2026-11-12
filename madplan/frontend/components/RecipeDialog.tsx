"use client";

import { Icon, Modal } from "@/components/ui";
import { quantity } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useI18n } from "@/lib/i18n";
import type { Recipe, RecipeLink } from "@/lib/types";

/** Named after the site a recipe comes from, or leading to it in Mealie if it is there already. */
export function RecipeLinkButton({ link }: { link: RecipeLink | null | undefined }) {
  const { t } = useI18n();
  // The address comes from outside, so only ordinary web links become links.
  if (!link || !/^https?:\/\//i.test(link.url)) return null;
  return (
    <a className="btn sm" href={link.url} target="_blank" rel="noopener noreferrer nofollow">
      <Icon name="external" size={14} />
      {link.kind === "mealie" ? t("recipe.openInMealie") : link.label}
    </a>
  );
}

export function RecipeImage({ url }: { url: string | null | undefined }) {
  if (!url || !/^https?:\/\//i.test(url)) return null;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      className="recipe-image"
      src={url}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={(event) => {
        event.currentTarget.style.display = "none";
      }}
    />
  );
}

export function RecipeDialog({ id, onClose }: { id: number; onClose: () => void }) {
  const { t, locale } = useI18n();
  const { data: recipe } = useApi<Recipe>(`/recipes/${id}`);
  return (
    <Modal title={recipe ? (locale === "en" && recipe.title_en ? recipe.title_en : recipe.title) : t("common.loading")} onClose={onClose}>
      {recipe && (
        <>
          <RecipeImage url={recipe.image_url} />
          {recipe.description && <p>{recipe.description}</p>}
          <div className="row small">
            <span className="muted">{t("recipe.servings", { count: recipe.servings })}</span>
            <RecipeLinkButton link={recipe.link} />
          </div>
          <div>
            <h3>{t("recipe.ingredients")}</h3>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {recipe.ingredients.map((line, index) => (
                <li key={index}>
                  {line.text ?? (
                    <>
                      {line.quantity != null ? `${quantity(line.quantity, line.unit, locale)} ` : ""}
                      {line.name}
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h3>{t("recipe.steps")}</h3>
            <ol style={{ margin: "6px 0 0", paddingLeft: 20 }}>
              {(locale === "en" && recipe.steps_en?.length ? recipe.steps_en : recipe.steps).map((step, index) => (
                <li key={index} style={{ marginBottom: 4 }}>
                  {step}
                </li>
              ))}
            </ol>
          </div>
          {!recipe.link && <p className="small muted">{t("recipe.noSource")}</p>}
          <p className="small muted">{t("recipe.allergyNote")}</p>
        </>
      )}
    </Modal>
  );
}
