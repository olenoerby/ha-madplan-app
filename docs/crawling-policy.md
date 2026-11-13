# What MadPlan reads, and why

The requirement is that MadPlan reads **only what crawling allows**. A robots.txt check alone
does not meet that. This document records what was checked for each source, so the decisions
can be reviewed and redone. It is a technical record and not legal advice.

Checked on 2026-11-03.

## Sources

| Source | robots.txt | Terms | Decision |
|---|---|---|---|
| **tilbudsugen.dk** | Only a `Sitemap` line, no `Disallow` | The terms page (`/page/personal-data-4`) has no ban on automated reading | **Allowed.** Reads `/offer/<term>` |
| **etilbudsavis.dk** | `User-agent: *` / `Allow: /` | No consumer terms with a ban found. Tjek's B2B terms (tjek.com/terms) restrict their API | **Allowed, public pages only.** Reads `/soeg/<term>` |
| **minetilbud.dk** | None (404) | **Forbids it**, see below | **Not a source.** On the blocklist |
| **123tilbudsaviser.dk** | Allows all but `/api/`, `/*/exit/*` and query strings | **Forbids it**: "Brug ud over almindelig gennemsyn kræver samtykke fra rettighedshaveren" (use beyond ordinary browsing needs the rights holder's consent), brugsbetingelser section 5. Run by Business Animals s.r.o., Prague | **Not a source.** On the blocklist. Checked 2026-11-13 |
| **naestetilbudsavis.dk** | Allows all but query strings | No ban on reading. Its code, layout and presentation may not be copied commercially. Run by ICMPFA, Bucharest | **Not a source.** Allowed to read, but it holds only pictures of the leaflets, with no products or prices as text. Checked 2026-11-13 |
| **tilbudsoverblik.dk** | Cannot be read: Cloudflare answers programs, robots.txt included, with a challenge (403) | Could not be read either | **Not a source.** A site that will not show its rules to programs is not saying yes. Checked 2026-11-13 |

`tilbudsavis.dk`, which the first version of this repository listed, is not a separate site. It
redirects to `etilbudsavis.dk`.

### minetilbud.dk

It has no robots.txt, so a crawler that only checks robots.txt would read it. Its privacy policy
forbids automated use of its data without a written agreement. It is not a source in MadPlan, and
its hosts (with `publisher.dk` and `dayli.dk`) stay on the blocklist in `app/crawlers/policy.py`,
so it cannot come back by mistake.

### etilbudsavis.dk and Tjek's API

The website gets its data from Tjek's API (`squid-api.tjek.com`). Tjek's terms say that "any use
of Tjek's APIs or API-based content outside Tjek's or the Customer's own platforms" needs prior
written approval (8.3), and those terms bind Tjek's business customers. MadPlan has no such
agreement, so it never calls that API and `tjek.com` is on the blocklist. It only reads the
public pages that robots.txt allows, using the data embedded in them. The same terms forbid
using their data to train AI models (8.6). MadPlan does not train anything.

Should you want a sturdier source, asking Tjek for API access is the proper route.

## How it is enforced

Every crawler request goes through `PoliteClient` in `app/crawlers/policy.py`, and an
adapter has no other way to reach the network. In order:

1. **Legal blocklist**, including subdomains. Refused without even asking for robots.txt.
2. **Adapter allowlist**: exact host and path prefix, HTTPS only.
3. **robots.txt**, cached for 24 hours. A 5xx or network failure counts as "everything
   disallowed" (RFC 9309). 401, 403 and 429 also count as no, which is stricter than the RFC.
4. **Rate limit** per host: the admin's delay (never under 1 second), or `Crawl-delay` if longer.

Redirects are not followed automatically, because each hop has to pass the same checks. A
denial stops that source's whole run, and shows on the admin page as "Stopped by rules".

Recipe pages found by the AI get the same checks before Mealie is asked to fetch them, along
with a check that the address is not an internal one.

## What is stored

Title, price, unit, store, validity and a link back to the source. Images are neither stored nor
hotlinked. The UI names the source of each offer and links to it. MadPlan identifies itself with
the user agent `MadPlanBot/1.0 (+https://github.com/olenoerby/MadPlan)`.

## What gets searched for

The catalog's ingredients, which give nothing away, and what someone chose to watch: price alerts,
priorities and pantry items with a minimum. Those do go to the sites as search terms. Other names
from pantries and shopping lists are never sent, as what people type there can be private.

## Recipe pages

A recipe has to come from a real page, so MadPlan reads such pages too, and any site can come up. They
go through the same `PoliteClient`: the legal blocklist, robots.txt and the rate limit. A site whose
robots.txt answers 401, 403 or 429 counts as saying no. Addresses come from a model or a person, so
each one, and every redirect, must be on the public internet. A redirect may only go to the same site.

Only single pages are read, a few a week per household, never a whole site. Terms cannot be read
automatically for sites unknown in advance, so a site that turns out to forbid it belongs on the
blocklist. The text is kept for the household's own use and always shown with a link back to the site.
The picture is linked to with no referrer and not stored by MadPlan. Mealie keeps its own copy, when
someone asks to import the recipe.

## If you run your own copy

You are the one crawling, so the decision is yours. Terms change, so look at them again now and
then, update `terms_checked_on` in `app/crawlers/sources.py`, and switch a source off on
the admin page if you are not comfortable with it.

## Adding a source

1. Read its robots.txt **and** its terms. Record what you find here.
2. Add a class with `info` and `search()` in `app/crawlers/sources.py`.
3. Keep `rules` as narrow as possible, and test parsing against a synthetic page that has the
   real structure. Do not commit the site's actual content.
