# Development

The app is in `madplan/`: the backend in `madplan/backend`, the frontend in `madplan/frontend`.
Home Assistant builds `madplan/Dockerfile` when the app is installed.

## Running it locally, as if inside Home Assistant

Requires Python 3.12 and Node 22. Build the frontend once (again after changing it):

```sh
cd madplan/frontend
npm ci
npm run build          # static files in madplan/frontend/out
```

Then, from the repository root, start a stand-in for Home Assistant, the app, and a stand-in for
the Supervisor's ingress, each in its own terminal:

```sh
pip install -r madplan/backend/requirements-dev.txt
uvicorn --app-dir tools/dev fake_ha:app --port 8125
```

```sh
mkdir -p .local/data .local/media
echo '{}' > .local/options.json      # the app's configuration, as Home Assistant would write it
cd madplan/backend
MADPLAN_DATA_DIR=../../.local/data MADPLAN_OPTIONS_FILE=../../.local/options.json \
MADPLAN_MEDIA_DIR=../../.local/media MADPLAN_STATIC_DIR=../frontend/out \
MADPLAN_HA_CORE_URL=http://127.0.0.1:8125/core MADPLAN_SUPERVISOR_TOKEN=dev \
MADPLAN_INGRESS_PROXY_IP=127.0.0.1 \
sh -c 'alembic upgrade head && uvicorn app.main:app --port 8099'
```

```sh
INGRESS_USER="ha-owner:owner:Owner" uvicorn --app-dir tools/dev ingress:app --port 8124
```

Open <http://localhost:8124/api/hassio_ingress/abc123/>. You are the owner, an administrator. A
second ingress on another port with `INGRESS_USER="ha-family:family:Family"` is an ordinary user.
Put options such as `"ai_task_entity": "ai_task.openai_ai_task"` or Mealie's address and token in
`.local/options.json` to try those parts. `.local/` is not committed.

After changing the frontend, run `npm run build` again. The app serves the new files at once.

## Tests

From the repository root:

```sh
pytest                  # a temporary SQLite database and a fake Home Assistant
ruff check madplan/backend --ignore E501
cd madplan/frontend && npm run typecheck && npm run build
```

Against a real Mealie, as CI does:

```sh
docker run -d --name mp-mealie -p 19925:9000 -e ALLOW_SIGNUP=false ghcr.io/mealie-recipes/mealie:v3.30.0
export MADPLAN_TEST_MEALIE_URL=http://localhost:19925
export MADPLAN_TEST_MEALIE_TOKEN=<an API token from that Mealie>
export MADPLAN_TEST_RECIPE_PAGE=<optional: a real recipe page, for the one test that goes out on the internet>
pytest
```

What the tests do and do not cover:

- **Home Assistant**: `FakeHomeAssistant` in `conftest.py` answers as Home Assistant's documentation
  says it does. Real ingress, the real users list and a real AI Task entity are not covered.
- **AI**: the tests script what the AI Task entity answers, and check what MadPlan sends and how it
  handles good, bad and missing answers. A real model's answers are not covered.
- **Crawlers**: parsers run on synthetic pages with the real structure, as the sites' content should
  not be committed. A redesign of a site shows on the admin page, as runs ending in "failed" or with
  0 offers.
- **Mealie**: `test_mealie_live.py` runs the whole flow against a real server. Skipped without the
  variables above.

## Trying it in Home Assistant

Add your fork as a repository under Settings, Apps, the three dots, Repositories, and install
MadPlan from it. Home Assistant builds the image on the device, which takes a while on a Raspberry
Pi. Raise `version` in `madplan/config.yaml` to have it offer the change as an update.

## When matching picks the wrong product

Matching ingredients to offers is the part most likely to go subtly wrong, as it depends on how
real products are named. Unit tests with invented names will not show it. To audit it, export the
current offers and look through what each ingredient matches. The cheapest ones matter most, as
those are what a plan ends up buying. Add what you find to `excludes` in
`madplan/backend/app/services/catalog.py`, and to the table in
`madplan/backend/tests/test_matching.py`. People can also correct single cases themselves with
"Wrong item" on the shopping list.

## Changing the database

```sh
cd madplan/backend
MADPLAN_DATA_DIR=../../.local/data alembic revision --autogenerate -m "what changed"
```

Read the generated file, use batch operations for SQLite, then add it to the commit. Migrations run
when the app starts.

## Adding text

Add the Danish string to `da` in `madplan/frontend/lib/i18n.tsx`. TypeScript then points out that
`en` needs it as well. New text should sound natural in both.
