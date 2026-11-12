# Architecture

```
Home Assistant (OS or Supervised)
  sidebar ─► Supervisor ingress ─► MadPlan app container (one process, port 8099)
             (adds X-Ingress-Path,      FastAPI: /api/v1/*, and the static frontend
              X-Remote-User-*)          jobs thread: crawling, Mealie, plans, messages
                                        SQLite in /data/madplan.db
                                             │
     ┌──────────────────┬───────────────────┼──────────────────────────┬────────────────┐
     ▼                  ▼                   ▼                          ▼                ▼
allowed offer sites   Mealie (optional,   Home Assistant Core via      AI Task entity   OpenStreetMap
(policy layer)        last word)          the Supervisor: language,    (optional,       (opt-in)
                                          users, notify, states        via Core)
```

The whole thing works without AI and without Mealie. Each one adds to it.

## How it sits in Home Assistant

- `repository.yaml` makes this a repository of apps, and `madplan/` is the one app in it.
  `madplan/config.yaml` declares ingress, the Home Assistant API, `/media` (for photos to AI Task) and
  the options an administrator sets.
- `madplan/Dockerfile` builds the frontend to static files in a Node stage, then installs the backend
  and those files in a Python image. `run.sh` migrates the database and starts uvicorn.
- The frontend is built with the base path `/__madplan_base__`. `app/main.py` serves the files and
  replaces it with the request's `X-Ingress-Path`, after checking that it looks like one.
- `app/core/ha.py` is everything asked of Home Assistant: the options file, `/api/config`, the
  `config/auth/list` websocket command for administrators, `ai_task.generate_data`, notify and states.
- `app/api/deps.py` makes a MadPlan user from the ingress headers the first time someone opens the
  app. Only requests from the Supervisor (`172.30.32.2`) may do that. A personal API token is the
  other way in, for an integration on the optional host port.

## Principles

- **Plans are deterministic. AI only adds candidates.** `services/planner.py` chooses among recipes
  by scoring them. AI suggestions join the same pool and face the same filters, so a suggestion
  from a model cannot get past an allergy filter.
- **Prices say where they come from.** Every price is `offer`, `shelf`, `reference` (our own estimate)
  or `unknown`, and the UI shows which. A saving is only reported against a known normal price.
- **Say so when unsure.** The price index answers "unknown" until it has real history. The economy
  figure stays empty until there is spending in both years.
- **Settings cascade.** The household holds the defaults, and a user's `null` inherits them. Allergies
  add up across everyone in the household, and always win.
- **One way out.** All crawling goes through `PoliteClient`. All model calls go through `run_agent`.

## Backend

| Path | Purpose |
|---|---|
| `app/models.py` | Database schema (SQLAlchemy 2) |
| `app/settings_schema.py` | Typed settings for household, user, AI and schedule, and how they combine |
| `app/core/ha.py` | **Home Assistant**: options, language, administrators, AI Task, notify |
| `app/api/` | HTTP routes: `auth` (who am I), `account`, `plans`, `household_data`, `admin`, `home_assistant` |
| `app/services/planner.py` | Choosing meals, budget fit, guests, skipped days |
| `app/services/shopping.py` | Adding up quantities, whole packs, store optimisation, finishing a trip |
| `app/services/pricing.py` | `PriceBook`: matching ingredients to offers, and pricing them |
| `app/services/catalog.py` | Known ingredients: search terms, exclusions, estimated prices |
| `app/services/builtin_recipes.py` | The starter recipes |
| `app/services/allergens.py` | Allergy tables and matching |
| `app/services/text.py` | Danish-aware matching (compounds and inflection), pack sizes, `WordIndex` |
| `app/services/pantry.py` | **What is at home**: what can be counted on, whole packs coming in, eaten meals going out |
| `app/services/restock.py` | What is running low: below a minimum, or a habit that is nearly due |
| `app/services/proposals.py` | What the assistant may offer to do, and carrying it out once someone approves |
| `app/services/privacy.py` | Taking your data along, and leaving |
| `app/services/mealie_pull.py` | **Mealie's last word**: moves, changes, removals and new entries in its planner, and ticks on its list |
| `app/services/mealie_sync.py` | **Sending to Mealie**, only on request: importing pages, calendar, list, guest notes, later changes. Recipes back from Mealie |
| `app/services/recipe_pages.py` | **Reading a recipe from a real page**, and refusing one that does not hold all of it |
| `app/services/new_recipes.py` | The "New recipes" page: searching, keeping what is found, ranking by current offers |
| `app/services/price_index.py`, `alerts.py`, `budget.py`, `voting.py`, `favorites.py`, `household_care.py` | Each feature |
| `app/crawlers/policy.py` | **The rules for what may be read** |
| `app/crawlers/sources.py`, `runner.py` | Parsers per site, and storing offers and price history |
| `app/ai/engine.py` | **The gateway to AI**: one AI Task call, its answer checked against a schema, and a log of calls |
| `app/ai/assistant.py` | The assistant: facts gathered up front, offers in its answer, and a rule-based fallback |
| `app/ai/features.py` | Structured tasks: finding addresses of recipe pages, photos, price hunting |
| `app/integrations/` | Clients for Mealie and Home Assistant |
| `app/worker.py` | Background jobs, in a thread of the web process. Reads its schedule on every tick |

### The main flow

1. The worker crawls allowed sources for the terms people watch and for what recipes need.
2. A user makes a plan, from recipes in Mealie and recipes read from real pages. If there are too few,
   and AI is available, more pages are searched for first. Candidates are filtered (allergies, diet), scored (offers, pantry, what is
   expiring, likes, learned taste, repetition) and chosen. If over budget, the costliest are swapped.
3. The shopping list adds up quantities across the week, buys whole packs, and weighs store
   combinations against the driving.
4. People vote per meal. The household's rule decides when a meal is accepted.
5. Nothing has reached Mealie so far. "Save and send to Mealie" ("Gem og Send til Mealie" in Danish) queues what differs from what the plan
   says. The worker makes sure the recipe exists there (found, or imported from its page and checked),
   then updates the calendar and the shopping list, and adds a note about any guests.
6. Finishing a shopping trip puts whole packs in the pantry, with an estimated date on what spoils.
   Once the day has passed, the meal's ingredients leave the pantry again.
7. After each crawl, open lists are priced again, and people hear about low prices on what they watch
   or keep buying. The worker also says what is running low and what to use up.
8. With automatic plans on, next week's plan makes itself, and the household is asked to vote.

### Extending it with other household systems

- **New integration**: add a client in `app/integrations/`, an option in `madplan/config.yaml`, its
  translations and `AppOptions`, and optionally a job in `worker.py` and facts in `ai/assistant.py`.
- **New module**: a router in `app/api/`, added in `app/main.py`, and a nav entry in
  `frontend/components/Shell.tsx`. Per-user switches go in `FeatureToggles`.

## Frontend

Next.js App Router with client components. Styling is in `app/globals.css`, with no CSS framework.
All text lives in `lib/i18n.tsx`, and the English dictionary is typed against the Danish one, so a
missing translation breaks the build. `components/charts.tsx` follows the project's chart rules:
thin marks, sparing labels, hover and keyboard readouts, and a table view for every chart.

## AI

There is no model, key or price in MadPlan. An administrator names an AI Task entity in the app's
configuration, and every call is `ai_task.generate_data` on it, through the Supervisor. The provider,
the model, the tokens and any limit are set up in that integration in Home Assistant, so a
household can make an entity just for MadPlan, or share one.

AI Task returns text, so MadPlan asks for JSON matching a schema it includes in the instructions,
and checks the answer with pydantic. An answer that does not fit counts as a failed call, and the
feature falls back as it would without AI. Photos go along as attachments: they are written to
`/media/madplan/` for the call and deleted after. Web search is asked for in words, so it only
happens if the chosen entity can search.
