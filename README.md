# MadPlan for Home Assistant

<img src="madplan/logo.png" alt="MadPlan" width="250">

MadPlan makes meal plans from this week's Danish supermarket offers. The household votes on the
dishes, the shopping list buys whole packs at the cheapest store, and the pantry keeps track of what
is at home. It can work with [Mealie](https://mealie.io) for recipes, its calendar and shopping
list, and with an AI agent in Home Assistant for finding new recipes, reading receipts and an
assistant. It works without either.

This is a **Home Assistant app** (formerly called an add-on). It needs **Home Assistant OS** or a
Supervised installation, as apps run under the Supervisor. It does not run on Home Assistant
Container or Core.

The app is in Danish for people who use Home Assistant in Danish, and in English otherwise. It
follows each person's own language in their profile, and Home Assistant's system language until it
knows it.

## Installing

1. In Home Assistant, go to **Settings → Apps → App store**, open the menu (⋮) and choose
   **Repositories**.
2. Add `https://github.com/olenoerby/ha-madplan-app`.
3. Find **MadPlan** in the store, install it and start it. Home Assistant builds it on the device,
   which takes a few minutes, longer on a Raspberry Pi.
4. Open it from the sidebar.

When installed, MadPlan is empty. An administrator sets it up on the app's **Configuration** tab in
Home Assistant, and everyone else just opens it from the sidebar.

## Configuration

Everything an administrator chooses is on the app's Configuration tab, not inside the app. The
app's own admin page shows what is set, checks that it works and lists the AI agents there are.

| Option | What it does |
|---|---|
| Mealie address, Mealie API token | Connects Mealie. Both are needed. Without them, nothing about Mealie shows in the app |
| Mealie address for links | The address people open Mealie on, if MadPlan reaches it on another one |
| Shopping list in Mealie | The list MadPlan sends to and reads ticks from. Made if it is missing |
| Read from Mealie every (minutes) | How often changes in Mealie are taken over |
| AI Task entity | Such as `ai_task.openai_ai_task`. Leave empty for no AI |
| Default notify service | Such as `notify.mobile_app_my_phone`. Each person can choose their own phone in MadPlan's settings, and this is used for those who have not |
| The assistant may read entity states | Lets the assistant answer "are we out of toilet paper?" from your sensors |
| Fetch offers, buy signals, weekly recipe search, automatic plans, how long chats and messages are kept | The schedule |

### People

Everyone who can open MadPlan in Home Assistant's sidebar is in the household. Home Assistant's
administrators are MadPlan's administrators. The others are ordinary users, and an administrator
can make them household managers or guests on the app's admin page. There are no passwords in
MadPlan.

### AI

All AI goes through an [AI Task](https://www.home-assistant.io/integrations/ai_task/) entity in
Home Assistant, such as one from the OpenAI, Google Gemini or Anthropic integrations. Choose its
model and limits there, and make one just for MadPlan if you want to keep its use apart. MadPlan
asks for answers as JSON and checks them, so a model that cannot keep to that will count as a
failed call. Finding new recipes and the price hunt need an agent that can search the web.

App configuration cannot offer a dropdown of entities, so the entity id is typed in. The admin page
in the app lists the AI Task entities Home Assistant has.

### Mealie has the last word

When Mealie is connected, nothing is sent to it until someone presses "Save and send to Mealie".
After that, Mealie decides: a dish moved, changed or removed in Mealie's meal planner is moved,
changed or removed in MadPlan, an entry added in Mealie shows up in MadPlan's plan, and what is
ticked off on Mealie's shopping list is ticked off in MadPlan.

### For an integration

The app's port is closed by default. An administrator can give it a port on the Network section, for
a Home Assistant integration that reads MadPlan with a personal API token, made under Settings in
the app. Such requests can never act as anyone but the token's owner.

### Data and backups

Everything is kept in a SQLite database in the app's `/data`, which Home Assistant's backups
include. Uninstalling the app deletes it.

## Offers

Offers are read from tilbudsugen.dk and etilbudsavis.dk, following their robots.txt and terms, at a
polite pace. minetilbud.dk does not allow crawling and is not used. See
[docs/crawling-policy.md](docs/crawling-policy.md).

## Features

"Tested" says how each was checked in this app. *Test* is the automated tests, *browser* is a run
in a browser against a stand-in for Home Assistant's ingress, and *earlier* means it was tried with
real services in the stand-alone version this app was made from, and the code did not change.
Nothing has yet been tried on a real Home Assistant installation.

### Home Assistant

| Feature | Status | Tested |
|---|---|---|
| Installed by adding the repository under Apps | Built | Not yet on a real Home Assistant. The image build runs in CI |
| Opens in the sidebar through ingress, under Home Assistant's own login | Works | Test and browser, with a stand-in for ingress |
| People are Home Assistant's users, and only the Supervisor may say who someone is | Works | Test, and a direct request is refused in the browser run |
| Home Assistant's administrators are MadPlan's, and the role follows changes there | Works | Test and browser |
| Household manager and guest roles, set in the app | Works | Test |
| Each person's own Home Assistant language: Danish when it is Danish, English otherwise | Works | Test. The system language also in the browser run. The profile language reported wrong on a real installation in 1.0.0 and fixed in 1.0.1, not yet confirmed there |
| Configuration on the app's tab in Home Assistant, shown and checked on the admin page | Works | Test and browser. Home Assistant's own form not seen yet |
| AI through a chosen AI Task entity | Works | Test and browser, with a stand-in answer. Not with a real entity yet |
| Photos (receipts, the cupboard) to the AI agent | Built | Test. Not with a real entity yet |
| Messages to each person's own phone, chosen from Home Assistant's notify services, or the default one | Works | Test |
| Mealie's buttons and texts are hidden until Mealie is connected | Works | Test and browser |
| Mealie has the last word: moves, changes, removals, new entries and ticks are taken over | Works | Test against a fake Mealie |
| SQLite in `/data`, migrated when the app starts | Works | Test, and on start in the browser run |

### Offers and prices

| Feature | Status | Tested |
|---|---|---|
| Fetch offers from tilbudsugen.dk and etilbudsavis.dk, respecting robots.txt, terms and rate limits | Works | Earlier, real. Test |
| Finding the right product for an ingredient | Works, but is a heuristic. "Wrong item" corrects single mistakes | Earlier, on 2,900 real offers |
| Shopping list with prices per store, whole packs, and stores weighed against the distance | Works | Earlier, real. Test |
| Price index and "buy now", price alerts | Works. Says "cannot tell yet" until there are two weeks of history | Test |
| The shopping list sorted by store section, with others' ticks shown within seconds | Works | Test. Earlier, browser |
| The receipt from a photo, checked by people before it is saved | Works | Test. Earlier, real |
| Price hunt on the web | Works, with an AI agent that can search | Test |

### Meal plan

| Feature | Status | Tested |
|---|---|---|
| A meal plan from offers, preferences, diet and what is at home, without AI | Works | Test and browser |
| Only recipes from Mealie or a real web page, never made up | Works | Test. Earlier, real |
| Allergies always exclude, also from AI and offers | Works. Reads only names, so check the packaging | Test |
| Votes per meal, decided by one vote, a majority or everyone | Works | Test |
| Pick a dish from an offer or a search and put it on a day, with a notice about repeats | Works | Test. Earlier, browser |
| "Everything bought" and "partly bought" | Works | Test |
| Move and swap dishes, also by dragging | Works | Test. Earlier, browser |
| Planned leftovers | Works | Test |
| Guests, skipped days, budget, "rarely, often, always" | Works | Test |
| Next week's plan made by itself, and voted on | Works. Off by default | Test |

### Mealie

| Feature | Status | Tested |
|---|---|---|
| "Save and send to Mealie" sends recipe, calendar entry and shopping list, and nothing goes before | Works | Earlier, real. Test |
| A recipe missing in Mealie is imported from its page, and checked | Works | Earlier, real |
| What MadPlan puts in Mealie is marked "Fra MadPlan" | Works | Test |
| What Mealie already has planned is shown on the plan | Works | Earlier, real |

### The household

| Feature | Status | Tested |
|---|---|---|
| Pantry that follows what is bought and eaten | Works | Test |
| "Buy more toilet paper?", food waste, budget, "less than last year" | Works | Test |
| Assistant, with a simple answer without AI. It can offer to do things, and nothing happens until someone presses | Works | Test, and browser with a stand-in answer |
| New recipes page, and a weekly search for recipes that use what is on offer | Works, the search with an AI agent | Test |
| Download your data, and delete your user | Works | Test |

## Documentation

| Guide | Contents |
|---|---|
| [madplan/DOCS.md](madplan/DOCS.md) | The app's documentation tab in Home Assistant |
| [docs/crawling-policy.md](docs/crawling-policy.md) | What may be read from which sites, and why |
| [docs/architecture.md](docs/architecture.md) | How the parts fit together |
| [docs/development.md](docs/development.md) | Running it locally, and tests |
| [docs/AI_CONTEXT.md](docs/AI_CONTEXT.md) | **For AI agents:** read it first |

## License

[MIT](LICENSE).
