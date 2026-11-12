# Changelog

## 1.0.6

- A change of language in a person's own Home Assistant profile reaches MadPlan within a couple of
  seconds, also while it is open, and MadPlan starts in that language without a Danish flash.
- When the AI agent comes back with no recipe pages, or only ones that do not exist, the "New
  recipes" page says that it must be allowed to search the web, and where to switch that on in
  Home Assistant. The log says how many addresses it gave.
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
