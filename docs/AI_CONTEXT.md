# Context for AI agents working on MadPlan

Read this before changing anything. Code paths are relative to `madplan/backend/` unless they say otherwise. It holds what the code cannot tell you: why things are the way
they are, what must not break, and how to know that a change works.

## What this is

A meal planner for Danish households, built around current supermarket offers, packaged as a
**Home Assistant app** (what used to be called an add-on). Python and FastAPI for the backend,
TypeScript and Next.js (a static export) for the frontend, SQLite in `/data`, all in one container in
`madplan/`. Home Assistant does the rest:

- **People** are Home Assistant's users. They open MadPlan from the sidebar through ingress, and the
  Supervisor tells the app who they are. Home Assistant's administrators are MadPlan's. There are no
  passwords in MadPlan. See `app/api/deps.py`.
- **Configuration** an administrator chooses is the app's configuration in Home Assistant
  (`/data/options.json`, read by `app/core/ha.py`). The admin page in the app only shows it and
  checks it.
- **AI** is whatever AI Task entity the administrator names. Its model, tokens and limits are managed
  in Home Assistant. See `app/ai/engine.py`.
- **Language** follows Home Assistant: Danish when it is Danish, English otherwise.
- **Mealie** is optional. Without its address and token, nothing about Mealie shows. With them,
  Mealie has the last word: see `app/services/mealie_pull.py`.

It needs Home Assistant OS or Supervised, as apps need the Supervisor. The repository is public, but
the owner's own installation is the main user, and the app is not advertised.

The original specification is summarised by the feature tables in [README.md](../README.md), and the
owner's plan is the phases at the top of them. **Update those tables in the same change as the
feature, and be honest in the "Tested" column.** It runs on real households' data, so a schema
change needs an Alembic migration that works on SQLite with real data (batch mode). Bump `version` in
`madplan/config.yaml` and add to `madplan/CHANGELOG.md` for every release, as that is what Home
Assistant offers as an update.

## Rules that must not break

1. **Read only what is allowed.** All crawling goes through `PoliteClient` in
   `app/crawlers/policy.py`. Never add another way to fetch from an offer site. robots.txt alone
   is not enough: read the site's terms too. minetilbud.dk forbids it, is not a source and stays on the
   block list. Never call Tjek's API (`squid-api.tjek.com`). See
   [crawling-policy.md](crawling-policy.md).
2. **Allergies are absolute.** Every path into a plan or a shopping list goes through
   `allergy_conflict` or `conflicting_allergy`. That includes AI output, which is untrusted. Fail towards
   excluding too much.
3. **AI only through Home Assistant.** Every model call goes through `run_agent` in
   `app/ai/engine.py`, which calls `ai_task.generate_data` on the chosen entity. Never call a model
   provider directly: the tokens and limits are set in Home Assistant, and that is the point.
4. **Everything works without AI, but nothing is made up to get there.** A call that is refused or fails
   falls back to deterministic code. Without AI, recipes come from Mealie or from links people paste.
5. **Do not make numbers up.** A price says where it came from. A saving against MadPlan's own estimate
   is labelled as one. The price index says "unknown" until it has real history. The first version of
   this project had `savings = total * 0.2`, and it must never come back in any form.
6. **Household data stays in its household.** Every query is scoped by `household_id` or `user_id`.
   Someone else's id answers 404. Add a test for it when you add a route.
7. **Assistant tools only read, or offer.** People approve what happens. Text from offers and web pages
   goes to models as data, so prompt injection must not be able to do anything. An offer is stored with
   the answer and spelled out on the button, and approving refers to that stored offer, in
   `services/proposals.py`. Keep what can be offered harmless.
8. **Secrets stay out of the repo and the browser.** The Mealie token is in the app's configuration
   and never sent to the browser. URLs that come from a model or a web page go through
   `is_public_http_url`, because of SSRF.
9. **Recipes are never made up.** The owner was emphatic. A new recipe comes from a real page with a
   title, picture, ingredients, steps and servings, read by `services/recipe_pages.py`. A model gives
   addresses and nothing else, and there is deliberately no way to write a recipe into Mealie from text.
10. **Nothing goes to Mealie from the plan before "Save and send to Mealie" ("Gem og Send til Mealie" in Danish).** `send_plan` in
    `services/mealie_sync.py` is the one place that queues meals. A vote only changes things here.
11. **Only the Supervisor may name a user.** The ingress headers are trusted only from
   `172.30.32.2`. Anything else needs a personal API token. Never relax this for convenience.
12. **Mealie has the last word.** When it is connected, what changed in Mealie's planner and shopping
   list is taken over by MadPlan, not the other way round.

## How to know that it works

```sh
pytest                  # from the repo root
ruff check madplan/backend --ignore E501
cd madplan/frontend && npm run typecheck && npm run build
```

Mocks only confirm your own idea of how a service behaves. This project has repeatedly found real bugs
only by running against real things, so prefer that when it is possible:

- **Mealie**: `madplan/backend/tests/test_mealie_live.py`, see [development.md](development.md).
- **Home Assistant**: the tests answer as Home Assistant would (`FakeHomeAssistant` in `conftest.py`),
  which only confirms our idea of it. Install the app on a real Home Assistant OS before trusting a
  change to ingress, users, AI Task or notify.
- **Real offers**: matching depends on real product names. Export the offers and read what each
  ingredient matches, cheapest first. Add each mistake you find to `tests/test_matching.py`.
- **The UI**: use it in a browser, and look at screenshots on desktop and phone, in both themes. Type
  checks say nothing about whether a page makes sense.
- **Upgrades**: test an upgrade, not only a fresh install. Keep a `/data` from the previous version
  and start the new image on it, as Home Assistant does when it updates the app.

Following a household through several weeks matters too. The pantry only ever filled up, as nothing
took out what had been eaten, so every plan after the first trip bought too little. Each step was
tested, and none of the tests went on to the following week.

Things found this way, as a warning about what tests with invented data hide:

| What real data or services showed | Where the fix lives |
|---|---|
| A plan bought "Kattefoder m/laks" as its cheapest salmon | `NOT_THE_PLAIN_FOOD` in `catalog.py` |
| Flavours passed for the product: "Smøreost Rød Peberfrugt" | `PriceBook._is_that_food` |
| "Squash" is also a soft drink | `PriceBook._plausible` |
| "pr. 1/2 kg" was read as 2 kg | `parse_pack_quantity`, and the unit price check in `sources.py` |
| Coffee was never crawled: the term limit cut off the end of the list | `build_terms` rotates, and a test covers it |
| One plan took 280 seconds with 5,000 offers | `WordIndex` in `text.py` |
| Mealie reports success for a page it could not read, with "Could not detect ingredients" | `is_usable_import` |
| A link found for lasagne gave a lentil soup | The title check in `is_usable_import` |
| Scraped Mealie recipes hold amounts as plain text | `split_ingredient` |
| `from __future__ import annotations` broke the SDK's tools when imported locally | Module-level import in `ai/assistant.py` |
| On the dashboard, tile names were cut off to "Husstanden E…" twice, and a count read "1 messages". Every test passed | Names on the tiles in `dashboard.yaml` in MadPlan-HA, and no unit on counts |
| Mealie shows no text on a plan entry with a recipe, seen by looking at its planner | Guests are a note entry of their own, `guest_note` |
| Mealie answers 201 for a page it could not read, or with no picture (`image` stays empty) | The checks in `_import_page` |
| Many real recipe pages have no structured data, or answer 403 on robots.txt | `recipe_pages.py` refuses them, and a search asks for twice what it needs |
| "1 x 500 g laksefilet" and "(i tynde skiver)" on real pages | `split_ingredient` |
| "Nøddesovs" was not seen as nuts | `_ALLERGEN_TERMS` |
| The ids of Mealie's plan entries are numbers, and stored as text, so "what we sent ourselves" never matched | `calendar_entries` compares them as text |
| The first real deploy failed with "no healthcheck configured": the Stack reused the old images, and every test here had used `up --build` | `pull_policy: build` in `docker-compose.yml` |
| The real model cut the price hunt off at 1,000 output tokens, as reasoning counts towards the limit | `max_tokens` in `ai/features.py` |
| A call cut off like that is billed, but the SDK reports no usage, so it was logged as free | `_failed_call_estimate` in `ai/engine.py` |
| The real model rightly left deposits (pant) out of a receipt's lines, so a correct receipt warned that it did not add up | `other` in `services/receipts.py` |
| In a browser, dragging a meal to a day off-screen did nothing in a headless test, but a real browser scrolls while dragging | Nothing to fix. Test drags with the target in view |

## Decisions, and why

| Decision | Why |
|---|---|
| Plans are chosen by deterministic scoring, and AI only finds pages to add | Cost, and an allergen cannot slip in through a model |
| A Home Assistant app, not a HACS integration | The owner wanted it installed by adding a repository under Settings, Apps. An integration can be added beside it later for entities |
| AI through an AI Task entity named in the configuration | Home Assistant then manages the provider, the model, tokens and limits. App configuration cannot offer a dropdown of entities, so it is a text field checked by a pattern, and the admin page lists the entities there are |
| AI answers are JSON, checked with pydantic, and the assistant gets its facts up front | AI Task has no tools for the model to call back into MadPlan. An answer that does not fit is a failed call, and the code falls back as before |
| SQLite in `/data`, WAL mode | `/data` is in Home Assistant's backups, and one household needs no database server |
| One process: the web server, and the jobs in a thread | One container, one port. `MADPLAN_RUN_JOBS=0` turns the jobs off for tests |
| No Celery or APScheduler | One worker loop reading its schedule from the database means no restart when an admin changes it |
| The frontend is exported to static files with a placeholder base path, which the backend swaps for `X-Ingress-Path` | The ingress path is different on every installation, and known only per request |
| The language is the person's own in Home Assistant, read by the frontend from the Home Assistant page around it and sent as `X-MadPlan-Language` | The owner asked for Danish when Home Assistant is. Each person's profile language can differ from the system's, which the first version used, and only the browser knows it. The system language is the fallback for someone new |
| Mealie's buttons and texts only show when its address and token are set | People without Mealie should not see what they cannot use |
| The ingress port can also be given a host port, for API tokens only | A future integration can read MadPlan. Requests there are not from the Supervisor, so they cannot name a user |
| minetilbud.dk is not a source, and stays on the block list | It does not allow crawling. The block list keeps it from coming back by mistake |
| A recipe missing from Mealie comes from a real page, and there is no fallback to text of our own | The owner's explicit requirement. If the import fails, the meal says why |
| MadPlan reads pages itself, from structured data, before anyone asks to import | People must see the site and the whole recipe before choosing, and Mealie's own check endpoint is for debugging |
| MadPlan's 59 own dishes are off unless a household switches them on, and only go to Mealie's calendar as a name | They come from no website. They remain for those with neither Mealie nor AI |
| Recipes a model wrote in earlier versions are no longer suggested, unless they are in Mealie | The owner asked to be rid of made-up recipes. A household that kept one in Mealie has chosen it |
| The send button sends what is accepted, and leaves undecided meals out | It must not overrule a family vote |
| More people than the household's usual on a meal count as guests | The specification has guests both per day and per meal |
| Found recipes are kept, and the search runs weekly at most | They can fit next week's offers for free, and leaflets change weekly |
| Allergies apply to a whole household, even when only one member set one | One plate is shared. A dislike is per ranking only |
| Offer images are not stored or linked | Tjek's terms forbid hotlinking API content, and the UI links back to the source instead |
| Crawling once a day by default | Leaflets change weekly |
| Accepted meals count as eaten once the day has passed, unless someone says otherwise | Nobody reports every dinner. Food not bought for a plan is simply not there to take. A meal nobody voted on counts only if its list was bought |
| Whole packs go into the pantry, not just what the recipe needed | That is what comes home, and the rest is what the waste agent is there for |
| A leftover only pays for its own share of a dish, and under a quarter of what is needed gets no credit | Otherwise 30 g of mince would make a whole dish look free |
| Food past its date does not count as being at home, and is bought anew | Failing towards buying, as with allergies. It stays on the pantry page for people to decide on |
| A finished shopping list is never redone | It is the record of a trip, and what it bought is in the pantry by now |
| Open lists are repriced after a crawl, unless something is ticked off | Someone may be standing in the store with it |
| Only what someone chose to watch goes out to the sites as a search: alerts, priorities, items with a minimum | Names typed on a list can be private |
| Habits are learned from what is added by hand | Plans take care of ingredients. Coffee and toilet paper are what nobody plans |
| In learned taste, each person counts once | Otherwise the one who votes on everything outweighs the one who only speaks up about fish |
| A dish put on a day by hand counts as that person's yes, and nothing more | It is a suggestion to the household like any other, so the voting rule still decides. `services/manual_meals.py` |
| A day with no plan gets an empty Monday–Sunday plan, which the weekly automatic plan fills in around | One plan per week stays the rule, and the shopping list belongs to the plan |
| "Bought" is set by people, per meal, and to "full" when the plan's list is shopped | The list is summed over the whole plan, so no item knows which dish it is for. Everything bought blocks a no, a swap and skipping the day, as the food is at home. Moving stays possible, within the plan whose list bought it |
| "Already planned" uses the household's `repeat_avoid_days` | One rule for what the planner avoids and what people are warned about |
| The `/ha` API only gains fields | An integration released apart from the app must keep working with an older one |
| Planned leftovers have no recipe of their own: the meal they come from is cooked for more | So the list buys once and the pantry loses it once. They follow that meal's vote, move with it and go with it |
| A receipt photo is read, shown and corrected, and only then saved, when the trip is finished | The model's reading is untrusted, and a wrong price would end up in the budget and the pantry |
| "Rarely, often, always" is averaged with each person counting once, and sits beside likes and dislikes | As with learned taste: one person's "always" should not outweigh another's "rarely" |
| A guest is refused every write in `current_user`, except a short list of their own things and the vote | A new route is then closed to guests unless someone opens it on purpose. Failing towards refusing |
| No service worker in the app | Ingress paths change, and Home Assistant's own app is what people carry. The last shopping list is still kept in the browser for a bad signal in the store |
| Home Assistant entity ids are fixed, as `sensor.madplan_*` | A dashboard or an automation can then be copied as it is, whatever the language and household name |

## Open questions for the owner

- The specification's sentence on repetition stops mid-way: "Sæt en husstandsinstilling at" ("Make a
  household setting that"). It is
  implemented as two household settings, days before a dish may return and the same protein per week.
  There may have been more to it.
- The spec says "hele ugen mandag til fredag" ("the whole week, Monday to Friday"). Both
  Monday–Sunday and Monday–Friday exist.
- The MIT license was chosen as the default for open source. It is the owner's call.

## Natural next steps

- Install the app on a real Home Assistant OS. Ingress, the user headers, the admin check over the
  websocket and AI Task have only met a stand-in written from Home Assistant's documentation.
- Check that AI Task passes photos (receipts, pantry) to the chosen entity as a media source, and
  whether the entity actually searches the web when asked to. Not every integration's AI Task does.
- The price hunt sometimes gives a shop's category page rather than the product page it was asked
  for. Consider checking the page before showing the price.
- Read a photo of a real receipt, not only the generated one the test used.
- After a few weeks of history, check that the "buy now" verdicts feel right.
- Look at how the "eaten" assumption works in practice, once the owner has used it for a few weeks.
- Translate descriptions of recipes the AI writes, if English users turn up. They follow the language
  of whoever asked for them.

## Style

- Comments explain why, and code explains what. Write the way the existing code reads.
- User-facing text in natural Danish and English, in `madplan/frontend/lib/i18n.tsx`. No machine-sounding phrases.
- A button that waits on AI, or on anything slow, uses `working` and `workingTexts` on `Button`. The
  texts follow the clock, as nothing reports progress, so they must not claim more than is known.
- Charts follow the rules in `frontend/components/charts.tsx`: thin marks, sparing labels, hover and
  keyboard readouts, and a table view for each.
- Keep it simple. A family's meal plan does not need microservices.
