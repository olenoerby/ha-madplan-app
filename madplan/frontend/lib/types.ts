/** admin runs the site, household_manager their household, guest may look and vote. */
export type Role = "admin" | "household_manager" | "user" | "guest";

export type MealType = "breakfast" | "lunch" | "dinner" | "snack" | "drink";
export type DietMode = "mediterranean" | "vegetarian" | "vegan" | "keto" | "low_carb" | "pescetarian";

export interface FeatureToggles {
  assistant: boolean;
  price_alerts: boolean;
  voting: boolean;
  waste_agent: boolean;
  diet_coach: boolean;
  pantry_photo: boolean;
  ha_notifications: boolean;
  buy_signals: boolean;
  restock: boolean;
  plan_updates: boolean;
  new_recipes: boolean;
}

export interface UserSettings {
  allergies: string[];
  dislikes: string[];
  likes: string[];
  frequencies: Record<string, "rarely" | "often" | "always">;
  meat_preferences: string[];
  diet_mode: DietMode | null;
  diet_coach_enabled: boolean | null;
  budget_weekly: number | null;
  max_price_per_meal: number | null;
  use_phone_location: boolean;
  pantry_photo_auto_apply: boolean;
  notify_service: string | null;
  features: FeatureToggles;
}

export interface HouseholdSettings {
  builtin_dishes: boolean;
  default_adults: number;
  default_children: number;
  child_portion: number;
  address: string | null;
  lat: number | null;
  lon: number | null;
  budget_weekly: number | null;
  budget_monthly: number | null;
  default_meal_types: MealType[];
  skip_weekdays: number[];
  repeat_avoid_days: number;
  repeat_protein_per_week: number;
  voting_rule: "single" | "majority" | "unanimous";
  allergies: string[];
  dislikes: string[];
  likes: string[];
  frequencies: Record<string, "rarely" | "often" | "always">;
  meat_preferences: string[];
  diet_mode: DietMode | null;
  diet_coach_enabled: boolean;
  assume_staples: boolean;
  max_stores: number;
  cost_per_km: number;
  cost_per_extra_store: number;
  allow_osm_lookup: boolean;
  waste_alert_days: number;
  track_consumption: boolean;
  estimate_expiry: boolean;
  restock_snoozed: Record<string, string>;
  blocked_matches: string[];
}

export type LoginProvider = "google" | "microsoft";
export const LOGIN_PROVIDERS: LoginProvider[] = ["google", "microsoft"];

/** Which ways of logging in this installation has set up. */
export type LoginMethods = Record<"local" | LoginProvider, boolean>;

export interface AuthMethod {
  id: number;
  /** "cloudflare" is an account from before Google and Microsoft were told apart, until it is next used. */
  provider: "local" | LoginProvider | "cloudflare";
  subject: string;
  last_used_at?: string | null;
}

export interface Me {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: Role;
  locale: "da" | "en";
  household: { id: number; name: string; default_adults: number; default_children: number };
  settings: UserSettings;
  /** What the administrator has set up in the app's configuration in Home Assistant. */
  setup: { mealie: boolean; ai: boolean };
}

export interface Reason {
  code: string;
  items?: string[];
  saving?: number;
  term?: string;
  diet?: string;
  days?: number;
  replaced?: string;
}

export interface RecipeLink {
  kind: "mealie" | "web";
  url: string;
  label: string;
}

export type Bought = "none" | "partial" | "full";

/** The same dish planned on another day. */
export interface NearbyMeal {
  meal_id: number;
  plan_id: number;
  date: string;
  meal_type: MealType;
  status: "pending" | "accepted" | "rejected";
  title: string;
}

/** What someone choosing a day for a dish is told. */
export interface DishPlanning {
  window_days: number;
  planned: NearbyMeal[];
  too_close: NearbyMeal[];
  slot: { meal_id: number; title: string; status: string; bought: Bought } | null;
  allergy: string | null;
}

/** A dish that uses an offer. */
export interface OfferDish {
  id: number;
  title: string;
  title_en: string | null;
  meal_types: string[];
  cost: number;
  people: number;
  image_url: string | null;
  link: RecipeLink | null;
}

export interface Meal {
  id: number;
  date: string;
  meal_type: MealType;
  title: string;
  title_en: string | null;
  recipe_id: number | null;
  recipe_source: string | null;
  adults: number;
  children: number;
  status: "pending" | "accepted" | "rejected";
  bought: Bought;
  added_by: { id: number; name: string } | null;
  also_planned: NearbyMeal[];
  leftover_portions: number;
  leftover_of: { id: number; title: string; date: string } | null;
  leftovers: { id: number; date: string; meal_type: MealType }[];
  cooked: boolean | null;
  consumed: boolean;
  estimated_cost: number;
  normal_cost: number | null;
  saving: number;
  reasons: Reason[];
  sync_status: "none" | "pending" | "synced" | "failed";
  sync_error: string | null;
  mealie_slug: string | null;
  link: RecipeLink | null;
  voters: number;
  votes: { user_id: number; name: string; vote: "yes" | "no"; comment: string | null }[];
  alternatives: { id: number; title: string; source: string; recipe_id: number | null; link: RecipeLink | null }[];
}

export interface ShoppingItem {
  id: number;
  name: string;
  quantity: number | null;
  unit: string | null;
  packs: number;
  store: string | null;
  offer_title: string | null;
  price_source: "offer" | "shelf" | "reference" | "unknown" | "receipt";
  cost: number | null;
  normal_cost: number | null;
  normal_source: "before" | "shelf" | "estimate" | null;
  pack_quantity: number | null;
  saving: number;
  covered_by_pantry: boolean;
  checked: boolean;
  manual: boolean;
  /** Where in the store it usually is, for walking the list in order. */
  section: string;
}

export interface StoreOption {
  stores: string[];
  goods: number;
  travel: number;
  extra_stop_cost: number;
  total: number;
  missing_distance: boolean;
  lines_with_price: number;
}

export interface ShoppingSummary {
  total: number;
  normal_total: number;
  savings: number;
  savings_measured: number;
  budget: number | null;
  within_budget: boolean | null;
  real_prices: number;
  estimated_prices: number;
  unknown_prices: number;
  by_store: { store: string; cost: number; items: number }[];
  store_plan: StoreOption | null;
  best_single_store: StoreOption | null;
  extra_store_saves: number;
  store_alternatives: StoreOption[];
}

export interface ShoppingListData {
  id: number;
  plan_id: number | null;
  name: string;
  completed_at: string | null;
  summary: ShoppingSummary;
  items: ShoppingItem[];
  allergy_warning?: string | null;
}

export interface Plan {
  id: number;
  start_date: string;
  end_date: string;
  meal_types: MealType[];
  budget: number | null;
  ai_used: boolean;
  ai_note?: string | null;
  is_member: boolean;
  can_vote: boolean;
  mealie: MealieState | null;
  missing_slots: number;
  days: { date: string; skipped: boolean; guest_adults: number; guest_children: number }[];
  meals: Meal[];
  shopping_list: ShoppingListData | null;
}

export interface MealieState {
  connected: boolean;
  sync_enabled: boolean;
  to_send: number;
  sending: number;
  failed: number;
  sent: number;
  undecided: number;
}

export interface MealieEntry {
  id?: string;
  /** A note has no recipe, and may say anything, such as "eating out". */
  kind?: "recipe" | "note";
  date: string;
  meal_type: string;
  title: string;
  mealie_slug: string | null;
  url: string | null;
  /** Put there by MadPlan, from a plan that no longer exists here. */
  from_madplan: boolean;
}

export interface PlanPreview {
  start: string;
  end: string;
  days: { date: string; always_skipped: boolean }[];
  mealie: { connected: boolean; entries: MealieEntry[] };
}

export interface PlanSummary {
  id: number;
  start_date: string;
  end_date: string;
  meals: number;
  accepted: number;
  total: number | null;
  shared: boolean;
}

export interface Recipe {
  id: number;
  title: string;
  title_en: string | null;
  description: string | null;
  meal_types: MealType[];
  servings: number;
  ingredients: { name: string; quantity: number | null; unit: string | null; text?: string }[];
  steps: string[];
  steps_en: string[] | null;
  tags: string[];
  source: string;
  source_url: string | null;
  image_url: string | null;
  link: RecipeLink | null;
  mealie_slug: string | null;
  allergy_conflict?: string | null;
}

export interface NewRecipeCard {
  id: number;
  title: string;
  description: string | null;
  image_url: string | null;
  meal_types: MealType[];
  servings: number;
  link: RecipeLink | null;
  cost: number;
  people: number;
  offers: { ingredient: string; offer: string; store: string; price: number; before: number | null; membership: boolean }[];
  more_offers: number;
  disliked: string[];
}

export interface NewRecipes {
  recipes: NewRecipeCard[];
  waiting_for_offers: number;
  offers_known: boolean;
  search_blocked: string | null;
  mealie_connected: boolean;
  mealie_sync_enabled: boolean;
}

export interface RecipeSearchResult {
  found: number;
  refused: number;
  refused_why: Record<string, number>;
  no_offers: boolean;
  /** Addresses the AI agent gave. None usually means it may not search the web. */
  links?: number;
}

export interface Offer {
  id: number;
  title: string;
  description: string | null;
  store: string;
  kind: "offer" | "shelf";
  price: number;
  pre_price: number | null;
  pack_quantity: number | null;
  pack_unit: string | null;
  unit_price: number | null;
  unit_basis: string | null;
  membership_required: boolean;
  valid_from: string | null;
  valid_to: string | null;
  source: string;
  url: string | null;
}

export interface PriceIndex {
  query: string;
  basis: string;
  history_points: number;
  history_days: number;
  enough_history: boolean;
  average: number | null;
  typical_low: number | null;
  lowest: number | null;
  lowest_30_days: number | null;
  current_lowest: number | null;
  current_pack_price: number | null;
  current_store: string | null;
  current_title: string | null;
  current_kind: "offer" | "shelf" | null;
  current_valid_to: string | null;
  current_url: string | null;
  discount: number | null;
  priority: number;
  buy_threshold: number;
  verdict: "buy" | "wait" | "unknown";
  history?: { week: string; lowest: number }[];
}

export interface AlertMatch {
  title?: string;
  store?: string;
  price?: number;
  value?: number;
  basis?: string;
  valid_to?: string | null;
  url?: string | null;
  source?: string;
}

export interface Alert {
  id: number;
  query: string;
  max_price: number;
  basis: "pack" | "kg" | "l" | "stk";
  active: boolean;
  use_web_search: boolean;
  last_triggered_at: string | null;
  last_match: AlertMatch | null;
}

export interface Notification {
  id: number;
  kind: string;
  data: Record<string, string | number | null>;
  created_at: string;
  read: boolean;
}

export interface PantryItem {
  id: number;
  name: string;
  quantity: number | null;
  unit: string | null;
  location: string | null;
  expires_on: string | null;
  expires_estimated: boolean;
  min_quantity: number | null;
  source: string;
}

export interface RestockSuggestion {
  key: string;
  name: string;
  reason: "below_minimum" | "usually_bought";
  quantity?: number | null;
  unit?: string | null;
  min_quantity?: number;
  every_days?: number;
  last_bought?: string;
  offer?: { title: string; store: string; price: number; valid_to: string | null; url: string | null } | null;
}

export interface Proposal {
  type: "add_to_shopping_list" | "create_price_alert" | "make_plan";
  status: "open" | "done" | "declined";
  items?: string[];
  query?: string;
  max_price?: number;
  basis?: string;
  mode?: string;
  budget?: number | null;
  result?: { added?: string[]; alert_id?: number; plan_id?: number };
}

export interface DishSuggestion {
  recipe_id: number;
  title: string;
  title_en: string | null;
  have: string[];
  missing: string[];
  coverage: number;
  steps: string[];
}

export interface WasteData {
  items: { id: number; name: string; quantity: number | null; unit: string | null; estimated: boolean; expires_on: string; days_left: number }[];
  dishes: { recipe_id: number; title: string; title_en: string | null; uses: string[] }[];
}

export interface BudgetPeriod {
  start: string;
  end: string;
  budget: number | null;
  spent: number;
  planned: number;
  expected: number;
  savings: number;
  over_budget: boolean;
  remaining: number | null;
}

export interface BudgetData {
  week: BudgetPeriod;
  month: BudgetPeriod;
  economy: {
    this_year_to_date: number;
    last_year_same_period: number;
    change_percent: number | null;
    saved_this_year: number;
    months: { month: string; spent: number; saved: number }[];
  };
}

export interface Expense {
  id: number;
  day: string;
  amount: number;
  savings: number;
  note: string | null;
  list_id: number | null;
}

export interface StoreRow {
  id: number;
  slug: string;
  name: string;
  enabled: boolean;
  favorite: boolean;
  distance_km: number | null;
  branch_name: string | null;
  lat: number | null;
  lon: number | null;
  known_grocery: boolean;
}

export interface DietReport {
  days: number;
  diet_mode: DietMode | null;
  meals: number;
  items_bought: number;
  proteins: Record<string, number>;
  vegetarian_meals: number;
  fish_meals: number;
  red_meat_meals: number;
  on_diet_meals: number;
  suggestions: { code: string; share?: number; count?: number; diet?: string }[];
  coach_enabled: boolean;
}

export interface AiFeatures {
  assistant: boolean;
  recipe_finder: boolean;
  vision: boolean;
  diet_coach: boolean;
  waste_agent: boolean;
  price_hunt: boolean;
}

export interface AiSettings {
  enabled: boolean;
  model: string;
  vision_model: string;
  reasoning_effort: "none" | "minimal" | "low" | "medium" | "high";
  monthly_budget_dkk: number;
  usd_to_dkk: number;
  price_input_per_m: number;
  price_cached_per_m: number;
  price_output_per_m: number;
  cache_write_multiplier: number;
  price_web_search_per_call: number;
  max_turns: number;
  max_output_tokens: number;
  chat_history_messages: number;
  plan_ai_candidates: number;
  features: AiFeatures;
}

export interface AiAdmin {
  settings: AiSettings;
  budget: { monthly_budget_dkk: number; spent_dkk: number; remaining_dkk: number; forecast_dkk: number; exhausted: boolean };
  usage: { feature: string; calls: number; cost_dkk: number; input_tokens: number; output_tokens: number; web_searches: number }[];
}

export interface Schedule {
  crawl_enabled: boolean;
  crawl_interval_hours: number;
  crawl_request_delay_seconds: number;
  crawl_max_terms: number;
  alert_interval_hours: number;
  waste_check_hours: number;
  mealie_sync_minutes: number;
  price_hunt_interval_hours: number;
  auto_plan_enabled: boolean;
  auto_plan_weekday: number;
  auto_plan_hour: number;
  restock_check_hours: number;
  buy_signals_enabled: boolean;
  reprice_after_crawl: boolean;
  recipe_search_enabled: boolean;
  recipe_search_interval_hours: number;
  chat_retention_days: number;
  notification_retention_days: number;
}

export interface IntegrationStatus {
  mealie_url: string | null;
  mealie_public_url: string | null;
  mealie_token_set: boolean;
  mealie_shopping_list_id: string | null;
  mealie_sync_enabled: boolean;
  ha_url: string | null;
  ha_token_set: boolean;
  ha_notify_service: string | null;
  ha_read_states: boolean;
  openai_key_set: boolean;
}

export interface SourceRow {
  slug: string;
  name: string;
  homepage: string;
  policy: "allowed" | "blocked";
  note: string;
  terms_checked_on: string;
  enabled: boolean;
  can_enable: boolean;
  last_run_at: string | null;
  last_status: string | null;
  offers: number;
}

export interface CrawlRun {
  id: number;
  source: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  terms: number;
  requests: number;
  denied_by_policy: number;
  offers_seen: number;
  error: string | null;
}

export interface AdminUser {
  id: number;
  username: string;
  display_name: string;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
  /** Administrators are Home Assistant's, and are changed there. */
  role_from_home_assistant: boolean;
}

/** The app's configuration in Home Assistant, as the admin page shows it. Never holds a secret. */
export interface AppSetup {
  mealie: { configured: boolean; url: string | null; public_url: string | null; token_set: boolean; shopping_list: string; sync_minutes: number };
  ai: { entity: string | null; configured: boolean; found: boolean | null; available: { entity_id: string; name: string }[] };
  notify_service: string | null;
  assistant_reads_states: boolean;
  language: "da" | "en";
  schedule: Schedule;
}
