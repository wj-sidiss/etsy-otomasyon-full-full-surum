# Notice

etsyotomasyon is licensed under the [MIT License](LICENSE).

## Trademarks

etsyotomasyon is an independent open-source project. It is **not affiliated with, endorsed
by, or sponsored by Etsy, Inc.**

"Etsy" is a trademark of Etsy, Inc. It is used here only to identify the service this
software interoperates with, as permitted by nominative fair use. No endorsement is
claimed or implied.

## Your responsibilities as a user

This software talks to your own Etsy shop with your own credentials. You remain
responsible for complying with:

- the [Etsy API Terms of Use](https://www.etsy.com/legal/api)
- the [Etsy Seller Policy](https://www.etsy.com/legal/sellers)
- any law that applies to the customer data this tool exports for you — order exports
  contain your buyers' names, email addresses and postal addresses

etsyotomasyon uses only official Etsy Open API v3 endpoints. It does not scrape etsy.com,
does not drive Etsy's seller interface, and stores no Etsy session cookies.

## Third-party dependencies

| Package | Licence |
|---|---|
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause |
| [typer](https://github.com/fastapi/typer) | MIT |
| [rich](https://github.com/Textualize/rich) | MIT |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |

Development only: [pytest](https://github.com/pytest-dev/pytest) (MIT),
[ruff](https://github.com/astral-sh/ruff) (MIT).

## Fonts

The app's interface ships three typefaces in `etsyotomasyon/web/static/fonts`, as the latin and
latin-ext WOFF2 files Google Fonts serves. All three are licensed under the
[SIL Open Font License 1.1](etsyotomasyon/web/static/fonts/OFL.txt) (OFL-1.1); the licence text
and their copyright lines are in `OFL.txt` next to them.

| Font | Copyright |
|---|---|
| [Inter](https://github.com/rsms/inter) | The Inter Project Authors |
| [Plus Jakarta Sans](https://github.com/tokotype/PlusJakartaSans) | The Plus Jakarta Sans Project Authors |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | The JetBrains Mono Project Authors |
