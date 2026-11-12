import type { RecipeLink } from "@/lib/types";

export type { Bought, MealieEntry, Meal, MealType, Plan, PlanPreview, PlanSummary, Reason, Recipe } from "@/lib/types";

export interface DishSuggestionOption {
  recipe_id: number;
  title: string;
  title_en: string | null;
  cost: number;
  normal_cost: number | null;
  link: RecipeLink | null;
}

export interface ShareTarget {
  id: number;
  display_name: string;
  shared: boolean;
  same_household: boolean;
}
