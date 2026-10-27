# Contributing to etsyotomasyon

Thanks for wanting to help. This is a small, deliberately readable project — you can
read the whole thing in an afternoon, and that is a feature worth protecting.

## Getting set up

```bash
git clone https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum.git
cd etsy-otomasyon-full-full-surum
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
etsyotomasyon desktop --port 3100     # the app, on a port of your choice
```

Python 3.9 to 3.13 are supported, and CI runs 3.9 and 3.13 on Ubuntu and Windows plus
3.10–3.12 on Ubuntu. On Windows without activating the venv, use
`.venv\Scripts\python -m pytest` and `.venv\Scripts\etsyotomasyon desktop`. Set
`ETSYOTOMASYON_HOME` to a scratch folder to try the app without touching your own
`~/.etsyotomasyon`.

**You do not need an Etsy account, an API key, or a network connection to contribute.**
The whole test suite is offline by design. If a change can only be tested against the
live API, say so in the pull request and describe what you ran manually.

## The rules that matter

**1. Official API only. No scraping.**
Every fact etsyotomasyon reports comes from `openapi.etsy.com`. No HTML fetching, no
browser automation, no third-party keyword services behind a login. This is not
squeamishness: driving Etsy's seller UI violates its Terms of Use and gets shops
suspended, and the people using this tool have real shops.

**2. Never invent numbers.**
Etsy does not publish search volume. If a metric cannot be measured from an API
response, etsyotomasyon does not show it. `seo keywords` reports what the returned listings
actually contain — nothing more.

**3. Writes are not retried.**
Etsy has no idempotency key, so a repeated `POST` is a real duplicate: a second draft,
a second shipment, a second "your order shipped" email to a buyer. `client.request()`
only retries a write when the request provably never arrived or was provably refused.
If you touch the retry logic, keep that property and keep its tests.

**4. Validate locally before you call.**
A 400 from Etsy costs a round trip and names no field. `listings.build_payload` checks
titles, tags, enums and required fields on this machine first, so `--dry-run` works with
no key at all. New fields should be validated the same way.

**5. Never widen scopes casually.**
`DEFAULT_SCOPES` is the minimum the tool needs, and there is deliberately no delete
scope. Adding one needs a very good reason in the pull request.

**6. Credentials stay out of the repo and out of the output.**
No key, token, secret or shop id in a tracked file, a test fixture, a log line or an
error message. Tests use obvious dummies like `KEY123`.

## The web app

The app is a local web server (`etsyotomasyon/web`, standard library only) and a frontend
in plain ES modules (`etsyotomasyon/web/static`). There is no build step, no npm package and
no CDN. Node is useful to check syntax (`node --check`, on a copy with a `.mjs`
extension), but it is not a dependency.

- **Every visible string goes through i18n.** Page strings live in
  `etsyotomasyon/web/static/i18n/<page>.json`, with the same keys under `tr` and `en`
  (Turkish first). `tests/test_web_i18n.py` and `tests/test_web_static.py` fail
  otherwise, including when an error code the server sends has no words.
- **Anything that changes the live shop or reaches a buyer asks first:** publishing,
  editing a live listing, an SEO fix, tracking numbers, Pins. The page shows a confirm
  dialog, and the server refuses the request without `confirm: true`.
- **Long or bulk Etsy work runs as a job** (`ctx.jobs.start`), never in a request
  handler, and uses the shop's shared client so one rate limiter covers everything.
- **Python 3.9 still counts.** Every module starts with `from __future__ import
  annotations`; no `match`, no `zip(strict=)`, no `tomllib`.

## When Etsy's docs and Etsy's behaviour disagree

Several defects in this project came from trusting the prose documentation over what
the API and the app settings screen actually do. Two examples now recorded in the code:

- `x-api-key` needs `keystring:shared_secret`, not the keystring alone.
- Callbacks may be `http://`, and `localhost` is fine — but an IP address is not.

If you find another, **write the evidence into a comment next to the code**, and add a
test that names the failure it prevents. That comment is the most valuable part of the
change; the next person will otherwise re-derive it the hard way.

## Pull requests

- One concern per pull request.
- Add a test that fails without your change. Name it after the failure it prevents.
- Run `pytest` and `ruff check .` before pushing. CI runs both on Ubuntu and Windows.
- Windows matters: status markers must survive a redirected, non-UTF-8 console.
- Update the README or [CLI.md](CLI.md) in the same commit if you change a screen, a
  command, a flag, or a CSV column.
- Screenshots in issues and pull requests must show invented data, never a real shop.
  **Ayarlar → Mağaza adını gizle** (or `--anonymise` on the command line) helps.

## Things that would genuinely help

- Digital downloads: `uploadListingFile` and the digital listing flow.
- Editing variations on existing listings (new drafts already copy them from the template).
- Shop sections and listing translations.
- A renewal helper for listings about to expire.
- Better taxonomy search: the current match is a plain substring.

## Reporting bugs

Open an issue with what you did (the screen or the command), what happened, your OS,
and your etsyotomasyon and Python versions (**Ayarlar → Hakkında**, or `etsyotomasyon --version`).
The app's log is in `~/.etsyotomasyon/logs/`.
**Redact your keystring, shared secret and tokens** — and if you have already pasted
one anywhere, rotate it on your Etsy app page.

## Licence

By contributing you agree that your work is licensed under the [MIT Licence](LICENSE).
