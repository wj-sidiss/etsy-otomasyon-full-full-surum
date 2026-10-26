# Security policy

etsyotomasyon holds credentials that can create, edit and publish listings in a real shop,
read customer names and addresses, and mark orders shipped. It runs on your own
computer: the app is a small web server that only your browser on that computer can
reach. This page states exactly what it does with your credentials and data.

## Reporting a vulnerability

Please report privately through
[GitHub's private vulnerability reporting](https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum-2/security/advisories/new)
rather than opening a public issue.

Include what you did, what happened, and what you expected. A proof of concept helps.
Expect an initial reply within a week. There is no bug bounty — this is an unfunded
project — but you will be credited in the release notes unless you prefer otherwise.

## What etsyotomasyon stores, and where

Everything lives in the shop's home folder. The first shop's home is `~/.etsyotomasyon`
(`%USERPROFILE%\.etsyotomasyon` on Windows). Each further shop has its own home,
`~/.etsyotomasyon/shops/<id>/`, so no key, token or queue is shared between shops.
`ETSYOTOMASYON_HOME` moves the whole tree.

| Secret | Where | Permissions |
|---|---|---|
| Etsy keystring, shared secret and callback address; Pinterest app id and secret (optional) | `.env` in the shop's home: `~/.etsyotomasyon/.env`, or `~/.etsyotomasyon/shops/<id>/.env` | `0600` |
| Etsy OAuth access and refresh token | `token.json` in the shop's home | `0600` |
| Pinterest token (optional) | `pinterest_token.json` in the shop's home | `0600` |
| Pinterest Pin queue (optional) | `pinterest-queue.json` in the shop's home | default |
| The running app's port and session token | `~/.etsyotomasyon/web.json`, removed when the app stops | `0600` |
| The last update check: the latest release seen, its notes, when it was checked, which version was announced or hidden (not a secret) | `~/.etsyotomasyon/update.json` | `0600` |

The app and `etsyotomasyon init` write the `.env` for you. Real environment variables win
over it. For the first shop, the command line also reads a `.env` in the current
directory (older versions kept the keys there). The app ignores that file, so a stray
`.env` next to the executable cannot change the open shop's keys.

POSIX modes are not enforced on Windows; the files still sit inside your user profile.

Order exports (`orders pull`, the CSV downloads) contain buyer names and addresses.
`.gitignore` covers `.env`, `.etsyotomasyon/`, `token.json` and `*.csv` exports. Check
`git status` before your first commit anyway.

## Who etsyotomasyon talks to

etsyotomasyon itself makes network requests to these hosts only:

| Host | Why |
|---|---|
| `openapi.etsy.com` | Etsy's Open API v3: every read and write, and the OAuth token endpoint |
| `api.etsy.com` | Etsy's OAuth token endpoint (the address Etsy's OAuth guide gives) |
| `api.pinterest.com` (`api-sandbox.pinterest.com` for a sandbox app) | Pinterest's API, only after you set up Pinterest |
| `i.etsystatic.com` | Etsy's public image server: a plain GET of the template listing's full-size photos, only when you add them as info images on **Şablon İlan**. No API key, sign-in or cookie is sent; only `https` addresses on `*.etsystatic.com` are fetched, a redirect is not followed, and each file is capped at 20 MB. |
| `www.tcmb.gov.tr` | `kurlar/today.xml`, the Central Bank of Turkey's public exchange-rate file, for the TRY amounts on **Kâr-Zarar**. A plain GET at most once a day; nothing is sent. |
| `api.github.com` | `/repos/wj-sidiss/etsy-otomasyon-full-full-surum-2/releases/latest`, to see whether a newer etsyotomasyon is out. A plain GET about 10 seconds after each start of the app (unless it asked less than an hour ago) and then once a day while it runs (an hour later after a failed try). Nothing is sent but the request itself with a `User-Agent: etsyotomasyon/<version>` header: no identifier, no cookie, nothing about you or your shop. Off with **Ayarlar → Yeni sürümleri denetle** (the automatic check) or `ETSYOTOMASYON_NO_UPDATE_CHECK=1` (every check). |

Nothing else. There is no telemetry and no account with this project. The update check
reads a public GitHub page; its answer is treated as untrusted: the release notes are
shown as plain text, and the download link only ever points at `github.com`.
The consent pages (`www.etsy.com/oauth/connect`, `www.pinterest.com/oauth/`) open in
your browser; etsyotomasyon does not call them. The pages of the app show listing photos
straight from Etsy's image servers (and board covers from Pinterest); your browser
loads those without a referrer.

## What etsyotomasyon does not do

- **No credential in output.** The app shows the first characters of the keystring and
  the *length* of the shared secret and the Pinterest app secret, never the values. No
  page receives a secret or a token. `init` and `doctor` print the same prefix and
  length, and `auth status` prints neither. `init` reads the secret with hidden input,
  so it does not reach your screen or your shell history.
- **No delete scope.** `DEFAULT_SCOPES` excludes `listings_d`. etsyotomasyon cannot delete a
  listing even if it is compromised or buggy.
- **No scraping and no stored Etsy session cookies.** Access is OAuth only, and etsyotomasyon
  never sees your Etsy or Pinterest password: you approve on their own pages.

## The local web server

Double-clicking the app, or `etsyotomasyon desktop`, starts a server and opens your browser
at `http://localhost:3000` (the next free port if 3000 is taken). Every web page you
have open can make your browser send requests to localhost, so the server trusts
nothing by default:

- **This computer only.** It listens on the loopback addresses only: `127.0.0.1`, and
  `::1` on the same port where IPv6 is available. Nothing on your network can reach it.
- **A new session each launch.** Each start creates a random token. The browser is
  opened with it once (`?k=…`), and the server swaps it for an `HttpOnly`,
  `SameSite=Strict` cookie and removes it from the address. Every `/api/` request
  except `/api/ping` needs that cookie.
- **Host and Origin are checked.** A request whose `Host` is not `localhost:PORT`,
  `127.0.0.1:PORT` or `[::1]:PORT` is refused, which defeats DNS rebinding. Every write
  (POST, PUT, PATCH, DELETE) needs an `X-Etsyotomasyon: 1` header. A page on another site
  cannot add it without a CORS preflight, and the server never answers one. When the
  browser sends an `Origin`, it must be the app's own.
- **Strict headers.** No response carries CORS headers. Every response carries a
  Content Security Policy (`default-src 'self'`, `script-src 'self'`,
  `connect-src 'self'`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.
- **Files stay inside their folders.** Workspace files, mockups and thumbnails are
  served only from inside the app's folders; any other path is a 404.
- **It stops by itself** about 90 seconds after the last tab closes, unless a task is
  still running. **Ayarlar → Kapat** stops it at once.

## Connecting to Etsy and Pinterest

- Etsy uses OAuth 2 with PKCE (S256) and a random `state`; Pinterest uses OAuth 2 with
  a random `state`. The code is exchanged for a token directly with Etsy or Pinterest,
  over HTTPS.
- While it waits for your answer, etsyotomasyon listens on the port of your callback
  address: `http://localhost:3003/…` for Etsy and `http://localhost:8085/` for
  Pinterest by default. The listener binds `127.0.0.1` only, waits at most 5 minutes
  and closes the port afterwards.
- **The listener accepts only the expected `state`.** Any other page can send a request
  to localhost. A request without the `state` of the consent request being waited for
  is refused with a plain page that repeats nothing from it. It is not recorded or
  passed on to the app, and the wait continues.

## Things worth knowing

**Both halves of the app credential are secret in practice.** Etsy requires
`keystring:shared_secret` in every `x-api-key` header. Treat the pair like a password.

**The refresh token is the valuable one.** It lasts 90 days and mints access tokens
without further consent. If `token.json` is exposed, disconnect on **Mağaza
Bağlantısı** (or run `etsyotomasyon auth logout`) and revoke the app's access in your Etsy
account settings.

**Nothing goes live without two confirmations.** In the app, everything that changes
the live shop or reaches buyers asks first in a dialog. That covers publishing one or
many listings, editing a live listing, an SEO fix, updating listings from a CSV,
sending tracking numbers and posting Pins. The server then refuses the request unless
it carries `"confirm": true`, which only that dialog sends. On the command line,
`listings push` and `orders ship` ask before sending and support `--dry-run`.

**New listings are always drafts.** **Tasarım Yükle**, `drop` and `listings push`
create listings as drafts, never active. A listing goes live only when you publish it:
with **Yayınla** in the app, or with `state=active` on an update row of `listings push`.
If you find any other path that makes a listing active, that is a security issue:
report it.

**Sending tracking numbers is irreversible.** Etsy emails the buyer and marks the order
shipped, and the API offers no undo. The app confirms first; `orders ship` asks and
supports `--dry-run`. Keep both.

**Narrow your scopes if you only read.** Before your first login:

```bash
ETSY_SCOPES=shops_r listings_r transactions_r
```

## If you leak a credential

1. Regenerate the app credential at <https://www.etsy.com/developers/your-apps> (for
   Pinterest, the app secret at <https://developers.pinterest.com/apps/>).
2. Disconnect in the app (or run `etsyotomasyon auth logout`) and revoke the app in your Etsy
   account settings.
3. If it reached a git commit, rotating is what fixes it — rewriting history does not,
   because the old value may already have been fetched.

## Supported versions

This project is pre-1.0. Fixes land on `main` and in the next release; there are no
backports.
