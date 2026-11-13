# Changelog

## 1.0.10

- The documentation links to the optional integration, olenoerby/ha-madplan-integration, and says how
  it reaches the app inside Home Assistant with no port opened. A test checks every field the
  integration reads, so it keeps working as the app is updated.

## 1.0.9

- 123tilbudsaviser.dk is on the block list, as its terms allow no use beyond ordinary browsing.
  tilbudsoverblik.dk and naestetilbudsavis.dk were checked too, and are not sources: see
  docs/crawling-policy.md.

## 1.0.8

- When pages are refused, a recipe search asks the AI agent for more, round by round, until it has
  kept enough (6, or 3 versions of a named dish), has read 50 pages, or has spent 2½ minutes. Each
  round tells the agent the addresses already tried and the sites that cannot be read, so it looks
  elsewhere. At most 4 rounds, to keep the tokens down.

## 1.0.7

- A search for recipes says why each page was refused, such as "may not be read by programs" or
  "did not say how many they serve", so a pattern can be seen and acted on.
- "New recipes" has a search for the open tab, such as "Search for more drinks", with a field to
  say what to look for, such as "alcohol-free" or "something with chicken". A search with a wish
  does not wait for offers.

## 1.0.6

- A change of language in a person's own Home Assistant profile reaches MadPlan within a couple of
  seconds, also while it is open, and MadPlan starts in that language without a Danish flash.
- When the AI agent comes back with no recipe pages, or only ones that do not exist, the "New
  recipes" page says that it must be allowed to search the web, and where to switch that on in
  Home Assistant. The log says how many addresses it gave.
- A warning on the admin page, and in the documentation, to check that the AI agent's model supports
  web search, such as gpt-4o-mini.
- "Plan a particular dish" can search online: type a dish, such as "tomatsuppe", and choose between
  3 real recipes for it from different sites. Each can be put on a day, or added to Mealie at once.
  Pages that are another dish are left out.
- "New recipes" shows every found recipe, also those with nothing on offer this week, so any of
  them can be planned or added to Mealie. Recipes already in Mealie say so.
- A recipe MadPlan adds to Mealie is put in Mealie's categories and tags for its meals, such as
  "Aftensmad", "Dinner", "Morgenmad" or "Drinks", when Mealie has them. None are made.
- The calendar has a button to Mealie's own meal planner, and dishes from Mealie link to their
  recipe there. Only with Mealie connected.

## 1.0.5

- Mealie's recipes are read when the first plan is made, and when the connection is tested on the
  admin page, which says how many there are. They no longer wait for the first crawl of offers.
- The crawl of offers runs apart from the other jobs, so sending to Mealie and reading from it never
  wait for it.
- One recipe in Mealie that cannot be read no longer keeps the others out.
- When Mealie is connected and no recipe fits, the message says so, and what to check.

## 1.0.4

- "New meal plan" shows the days the plan covers, as a small calendar, and each can be skipped.
- With Mealie connected, the calendar shows what Mealie already has. A dish there keeps its place
  and comes onto the plan. For a note, such as "Eating out", choose between a dish beside it and
  skipping the day. Notes can be written to Mealie from the dialog.
- A note in Mealie no longer becomes a meal on a skipped day, or beside a dish planned for the same
  meal.

## 1.0.3

- The logo says MadPlan.

## 1.0.2

- Each person chooses where their messages go, such as their own phone, under Settings, Features.
  The notify service in the app's configuration is the default for those who have not chosen.

## 1.0.1

- The app follows each person's own language in their Home Assistant profile, not only the
  system language.

## 1.0.0

- MadPlan as a Home Assistant app: opens in the sidebar, with Home Assistant's users, administrators
  and language.
- Configuration on the app's tab in Home Assistant: Mealie, the AI Task entity, notify service and
  the schedule.
- All AI through a Home Assistant AI Task entity.
- Mealie is optional, and has the last word when connected: changes to its meal planner and ticks
  on its shopping list are taken over.
- SQLite in the app's storage, included in backups.
- minetilbud.dk is not used, as it does not allow crawling.
