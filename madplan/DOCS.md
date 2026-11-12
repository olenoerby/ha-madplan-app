# MadPlan

MadPlan makes meal plans from this week's Danish supermarket offers. The household votes on the
dishes, the shopping list buys at the cheapest store, and the pantry keeps track of what is at home.

Open it from the sidebar. Everyone who can see it there is in the household, and Home Assistant's
administrators are its administrators. It speaks Danish when Home Assistant does, and English
otherwise.

## Setting it up

Everything is optional. With nothing set, MadPlan plans from offers and the recipes people add by
pasting links to recipe pages.

### Mealie

Set **Mealie address** (such as `http://192.168.1.10:9925`) and **Mealie API token** (made in Mealie
under your profile, API tokens). MadPlan then uses Mealie's recipes, and "Save and send to Mealie"
on a plan puts the dishes in Mealie's meal planner and the shopping on the list named in **Shopping
list in Mealie**.

Mealie has the last word: what is moved, changed, added or removed in Mealie's meal planner, and
what is ticked off on its list, is taken over by MadPlan every **Read from Mealie every** minutes.

If people open Mealie on another address than the one MadPlan uses, such as a public one, put that
in **Mealie address for links**.

### AI

Set **AI Task entity** to one of Home Assistant's AI Task entities, such as
`ai_task.openai_ai_task`. The admin page in MadPlan lists the ones you have. Set up the model and its
limits in that integration. For finding new recipes and hunting prices, it must be able to search
the web.

With **The assistant may read entity states** on, the assistant can answer from your sensors. Their
names and values are then sent to the AI agent.

### Messages

Set **Notify service**, such as `notify.mobile_app_my_phone`, to get MadPlan's messages there as
well as in the app.

### The schedule

How often offers are fetched, whether to tell when it is a good time to buy, the weekly recipe
search, a plan for next week made by itself, and how long chats and messages are kept.

## Roles

On MadPlan's admin page, an administrator can make someone a **household manager**, who runs the
household's settings and stores, or a **guest**, who can see everything and vote but only changes
their own things.

## Network

The port is closed unless you give it one. It is only for a Home Assistant integration that reads
MadPlan with a personal API token, made in MadPlan under Settings.

## Data

Everything is in the app's own storage, included in Home Assistant's backups. Uninstalling the app
deletes it. Each person can download their data, or delete themselves, under Settings.
