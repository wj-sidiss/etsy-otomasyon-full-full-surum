## What this changes

<!-- One or two sentences. If it fixes an issue, write "Fixes #123". -->

## Why

<!-- What breaks or is missing today. If Etsy's documented behaviour and its actual
behaviour differ, say which one you tested against and how. -->

## How it was tested

<!-- `pytest` alone is fine for most changes. If you tested against a live shop, say
what you ran and what happened — and never paste a credential. -->

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` is clean
- [ ] A test fails without this change, and is named after the failure it prevents
- [ ] README updated if a command, flag or CSV column changed
- [ ] No credential, token, shop id or personal data in the diff
- [ ] No scraping or browser automation added — official API only
- [ ] If retry logic changed: non-idempotent writes are still never repeated
