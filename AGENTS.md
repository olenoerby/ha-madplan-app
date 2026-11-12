# Notes for AI coding agents

Read [docs/AI_CONTEXT.md](docs/AI_CONTEXT.md) first. The feature status is in [README.md](README.md),
and it must be updated in the same change as the feature.

This repository is a Home Assistant app. The app is in `madplan/`, with the backend in
`madplan/backend` and the frontend in `madplan/frontend`. A release raises `version` in
`madplan/config.yaml` and adds to `madplan/CHANGELOG.md`. A new option goes in `config.yaml`
(`options` and `schema`), in `madplan/translations/en.yaml` and `da.yaml`, and in `AppOptions` in
`madplan/backend/app/core/ha.py`.

Documentation, READMEs, code comments, commit messages and tests are written in English. The app
itself is in Danish and English: every text a person sees goes into both languages in
`madplan/frontend/lib/i18n.tsx` (and in `madplan/backend/app/services/notifications.py` for
messages). Danish words in the code that are data, such as product names the matching works on, stay
Danish.
