# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- The bell and the Panel showed the raw text `orders:print_ready` / `orders:print_missing` after the order print-file job ran: the two notifications now use their strings. A new test checks that every notification key in the code has a Turkish and an English string.
- The from-source steps in README, SETUP and CONTRIBUTING said `cd etsyotomasyon`; the cloned folder is `etsy-otomasyon-full-full-surum`.

### Added
- **Bleed for print files.** The *Baskı dosyası* window has a *Taşma payı* (none, 0.25 in or
  0.5 in, remembered): the file runs that far past every edge, so a printer who trims a little
  leaves no white line. A pattern simply continues (the trimmed size lines up exactly with the
  file made without a bleed), a picture is enlarged to cover it. The file's name carries
  `bleed0.5in`, its pixel size includes the bleed, and the partner is sent both the file and
  the trimmed size (`bleed` in `GET/POST /api/orders/{id}/print`).

### Added
- **Your own production partner.** In an order's *Baskı dosyası* window, **Üretim ortağı
  bağla** takes a name, the address your partner takes orders at (its API, a Zapier or Make
  webhook, or your own server; https, or http to this computer), an optional token, whether
  to send the files too (up to 25 MB each, larger ones go by name) and your costs (price per
  square metre, fixed price per order). Saved on this computer only (`partner.json`); no
  partner is built in. **Siparişi gönder** then posts the order as JSON (buyer's address, each
  item's size, paper and file, the invoice sheet) after a confirmation, once per order
  (*Tekrar gönder* sends it again), and records the answer beside the files. The window
  shows the estimated production cost against the item income and warns when the order
  would lose money. Nothing is sent to Etsy and nothing is sent before the button is
  pressed (`GET/POST /api/partner`, `POST /api/orders/{id}/partner`).
- **Competitor analysis beyond the tags.** The SEO page's keyword research now also shows
  what the ranking listings charge (price range and median, in the most common currency),
  how many favourites the middle one has, the title phrases most of them use, and the five
  most favourited listings with links (`market` in `GET /api/seo/research`). It reads the
  same search the tags come from, so it costs no extra Etsy request.

### Added
- **Print files.** Found on a real shop: 88 of its 99 listings had only their mockups left,
  so an order of one of them could not be printed. Every listing now has a folder
  for the design an order is printed from, `print-files/<listing id>/` in the products
  folder:
  - Every draft etsyotomasyon makes (the app's **Tasarım Yükle** and `etsyotomasyon drop auto`)
    keeps a copy of what it was made from there: a loose design byte for byte, a product
    folder the files in its `dosyalar` / `files` subfolder (ready photos alone are
    mockups, so a folder without one keeps nothing). A copy that fails never fails the
    draft. Drafts made before this keep theirs the first time **İlanlar** opens, while
    their design is still in `2-PRODUCTS` or `archive/`.
  - **İlanlar**: a listing without one says *baskı dosyası yok* under its title, and
    **Filtrele** has *Baskı dosyası: Var / Yok* with counts (`GET /api/listings?print=`,
    and `print_file` on each row; `null` for a download, which Etsy delivers itself).
  - **The listing's page**: a row under the source design shows the print file (pixels
    and size) or *Baskı dosyası yok*. Drop a file on it or pick one with **Dosya ekle**
    (an image, PDF, SVG, PSD, AI, EPS or ZIP, up to 100 MB; the same file twice is kept
    once), or open its folder to copy a larger one in. Nothing of this goes to Etsy.
  - **Siparişler**: an order waiting to ship with an item whose listing has no print file
    says *Baskı dosyası yok* (`print_file` on each item).
  - **Print-ready files from an order.** **Siparişler** has a *Baskı dosyası* link on each
    order waiting to ship. It reads every item's size from its variation ("1 Roll 23×160
    in", "Mural 120×140 in", "SAMPLE 15×20 in", cm too) and makes its file from the
    listing's print file into `print-files/orders/<order id>/`, as a JPEG with its dpi
    (150 by default): a roll or sample repeats the design a whole number of times across
    the roll's width, so rolls hang side by side without a step; a mural covers the whole
    wall (made at a lower dpi if it would not fit in memory, and it says so). A design whose
    opposite edges do not meet is flagged, since a line would show where it repeats.
    The dialog offers 150 or 300 dpi (a 23×160 in roll at 300 dpi is 6900×48000 px; a mural
    goes to a lower dpi to fit in memory and says so), keeps the pattern the same size at
    both, and flags a design that must be enlarged more than 1.5× (it would print soft).
    Nothing is sent to Etsy (`GET/POST /api/orders/{id}/print`, `drop/production.py`).
    Opening it makes the files at once (one click: *Siparişi hazırla*), at the 150 or 300 dpi
    last chosen, and **Siparişleri hazırla** in the header does the same for every order
    waiting to ship, naming the ones that still lack a print file.
  - **The order's invoice sheet.** Making an order's files also writes
    `order-<id>-invoice.pdf` beside them: the sheet Etsy prints for the order (ship-to, order
    number and date, items with their variations, totals), made from the order itself, with
    the picture of each item sold at the bottom right, at the proportions of the size
    ordered (a long roll shows its top). It is what the printer is sent with the print file.
  - **Seamless patterns.** A pattern whose opposite edges do not meet (a line would show
    where it repeats) is repaired in the order's file: the tile is shifted half its size and
    cross-faded into itself along the edges, then measured again. The dialog says so; the
    design file itself is never changed. A design enlarged more than 1.2× to fill its file is
    sharpened a little, and one enlarged more than 1.5× is flagged.
  - **Murals of a tile.** A mural size ordered from a near-square (tile) design repeats the
    pattern at the same scale as a roll; a wide or tall picture still covers the wall.
  - **Ready before you look.** When Siparişler opens, a background job makes the print files
    and invoice sheet of every order waiting to ship that has none yet, and the bell says how
    many were made and how many lack a print file on their listing
    (`POST /api/orders/print-waiting`).
  - **Ship-by date.** Each order still to send shows when it must ship by: orders due today
    or tomorrow are amber, late ones red (Etsy counts a late shipment against the shop).
  - **SEO audit: photos.** Four checks on a listing read with its images: fewer than 5
    photos is a warning (`images.few`), fewer than 10 a note (`images.some`), photos without
    alt text a note (`images.alt`), and a main photo under 2000 px on its short side a note
    (`images.small`). Nothing is said about a listing read without its images.

## [0.3.3] — 2026-10-04

### Changed
- **Titles and tags of new drafts.** Found on a real shop's audit: 44% of its tag slots
  were the same dozen shop-wide tags, so its listings competed with each other, and its
  titles opened with a generic room phrase and promised "removable, self adhesive" for a
  product that also comes as paste-up paper. The generator now works like this:
  - **Title**: the design's own phrase leads ("Jungle Animals Wallpaper", inside the first
    40 characters), then up to two market phrases that each add new words, then one phrase
    of a room or a look ("Kids Room Decor"), and last the seller's material qualifier,
    taken from the template ("Peel and Stick", or "Peel and Stick or Traditional" when the
    template also sells paste). Padding such as "Wall Decor" never counts as a new search,
    a product noun ("Mural") is used once unless it is the product itself, and a phrase
    that only says how the product goes on is left to the qualifier. Wallpaper and mural
    are products now.
  - **Material claims**: "removable", "self adhesive", "renter friendly", "temporary" and
    "peel and stick" are written into a title or a tag only when the template's own title,
    tags or materials make them. A template that mentions paste anywhere (title, tags,
    materials, or its description: "traditional wallpaper", "pre-pasted", "wallpaper paste",
    "non-adhesive") sells a product they are false for, so none is written and the
    qualifier says both. The template's own tags are filtered the same way.
  - **Tags**: at most 3 of the 13 are the shop-wide kind (the template's format and
    material tags, and the market's most used tags that hold no word of the design); the
    rest are about the design: its name with the product ("lemon wallpaper"), with
    "mural", with a room the market names, and the market's own tags that carry a word of
    it, ranked by the share of ranking listings that use them. Two tags that are one search
    (`gift`/`gifts`, "wall mural"/"mural") never both go in. A tag the shop's other drafts
    already carry ranks lower, and no draft shares 7 or more tags with another: within a
    run (products are numbered in folder order, and each waits for the ones before it, so
    the result does not depend on how they were scheduled) and against the upload history,
    which now records each draft's tags (`tags`; older entries without them are fine).
    The template's tags about its own look or room ("coastal wallpaper") are no longer
    reused for other designs.
  - Drafts of the same design again, or of designs the market describes alike, no longer
    come out with the same thirteen tags.
- **SEO page, Düzelt and `etsyotomasyon seo suggest`** suggest tags by the same rules: nothing
  that is one search with a tag the listing has, no adhesive claim the listing does not make
  (none if it also sells paste), no more of the shop's generic tags than the listing can
  afford (3), and nothing that would put 7 of its tags on another listing.

### Added
- **SEO audit: two checks across your listings, and one on the title.** From the listings
  already read (no extra Etsy call):
  - *Tags shared with another listing*: 7 or more shared is a note (-3), 9 or more a
    warning (-10); the other listing's id is in the message and the page's details. On the
    audited shop 30 listings share 9 or more with another.
  - *Generic tags*: 5 or more of a listing's tags are on at least half of the shop's
    listings is a note (-3), more than half of its tags a warning (-10); not in a shop of
    fewer than 5 listings.
  - *Generic opening* (`title.generic_opening`): the first 40 characters hold no word about
    the design is a warning (-10); the first phrase holds none ("Kitchen Wallpaper | ...")
    but the 40 characters do is a note (-3). Not raised again when the first 40 characters
    hold no keyword at all (`title.front_empty` already is).

  A listing none of this applies to keeps its score. The SEO page (with a sentence for each
  in Turkish and English), its CSV report and `etsyotomasyon seo audit` use all three; the
  listings table and the dashboard still score each listing alone, so they show the opening
  check but not the two across-listings ones.

### Added
- **Realistic mockups.** A design no longer sits flat on top of the photo:
  - *Gerçekçilik* (realism, 0-100): the design takes the mockup's own light, folds and
    fabric texture inside the print area. Darker folds darken the print, highlights lift
    it, and a flat area changes nothing, so a design keeps its colours on a white or light
    garment. On fabric it also follows the folds a little. Each mockup type has its own
    default: 65 for T-shirts, sweatshirts and hoodies, 60 for totes, 50 for pillows, 45 for
    mugs, 30 for canvas, 25 for phone cases, 15 for posters and stickers, 35 for other.
    0 is the flat paste of before.
  - *4 köşe* (four corners): a print area can be four draggable corners instead of a
    rectangle, and the design is placed in perspective (a framed print shot at an
    angle, a tilted phone case). Each corner can also be moved with the arrow keys.
  - *Kavis* (curve, 0-100): the design wraps round a mug, tumbler or bottle, narrower and
    a little darker towards the sides, with a slight arc that stays inside the print area.
    On by default (55) for mugs and offered for the *other* type too. A curved product is
    rigid, so its highlights are never read as folds.
  The print-area editor in **Mockuplar** has the shape switch and both sliders, with a
  live preview drawn by the same code as the drafts. *Aynı ölçüdeki mockup'lara da
  uygula* copies the corners and the settings as well; a slider left on its default does
  not overwrite another mockup's own value, and a same-size mockup that only borrows the
  area keeps its own type's realism and curve. Tasarım Yükle, `drop run`,
  `drop auto` and the watermark preview all composite this way, and the watermark still
  goes on afterwards. Existing print areas keep working; with realism and curve at 0
  they render exactly as before. Corners, realism and curve are stored in
  `positions.json` next to `x`, `y`, `w`, `h`, which still hold the area's bounding box.
  A 2000 px composite takes about 0.2-0.4 s. A steep perspective is drawn larger and
  averaged down so it does not shimmer; on a large mockup that is done in bands, so it
  needs about half the memory it otherwise would (about 0.4 GB at 4000 px).

## [0.3.2] — 2026-09-30

### Added
- A download for Intel Macs, `etsyotomasyon-<version>-macos-intel.zip`, built on GitHub's Intel macOS runner.

- **Filigran (watermark)**: a card at the bottom of **Mockuplar** takes your own mark (a
  PNG with a transparent background is recommended; JPG and WebP up to 10 MB), with a
  live preview on one of your mockups, drawn by the same code as the drafts. Choose
  *Yalnızca dijital ürünler* (the default) or *Tüm ilan görselleri*, the position (center,
  bottom-right corner or a diagonal repeat), the opacity (10-90 %, 35 % by default) and
  the size as a share of the photo's width (30 %, or 15 % for each repeated mark). A mark
  on a solid background can have it removed. Tasarım Yükle, `drop run` and `drop auto`
  stamp it on a copy of every listing photo (the mockups, the flat image or preview, a
  product folder's own photos) in the batch's `watermarked` folder, tell Etsy the
  picture is watermarked, and never touch the files buyers download or your originals.
  The copies keep the photo's size and quality and carry no EXIF. A photo the mark cannot
  go on stops its product rather than going up without it. The start card says
  "Filigran: açık · dijital ürünlerde" (or off); `etsyotomasyon drop watermark` sets it from
  the command line (`--no-watermark` leaves it off for one run). It is kept as
  `watermark.png` and `watermark.json` beside the folders.
- **Şablon İlan: info images** (*bilgi görselleri*). Under the template's fields, tick
  the template listing's photos every listing should end with (materials, roll size,
  installation, sample, measuring...) or add pictures of your own, and set their order.
  etsyotomasyon keeps full-size copies in the products folder (`info-images`), so runs need no
  Etsy call for them and the command line (`drop run`, `drop auto`) uses the same ones.
  Every draft gets them after its own photos, in that order, with their alt text and no
  watermark; the upload history records them, and the listing page names them. Etsy
  takes 20 pictures per listing, so each one leaves room for one mockup less: the
  Mockuplar counter and the Tasarım Yükle start card show the real numbers, and a product
  folder whose photos leave too little room gets the first ones that fit, with a warning.
  At most 10.
- **Şablon İlan → Açıklama şablonu** (description template): the text every new draft's
  description is written from, starting as the template listing's own description with
  its title turned into `{başlık}`. `{başlık}` is each draft's title and `{tasarım}` its
  design's name (English: `{title}`, `{design}`). Sentences holding a word of the template
  listing's own design (from its title and tags, such as "lemon"; never a product
  word such as "wallpaper" or "sample") are highlighted: they would be copied onto every
  draft. It is saved in `product.json`; saving the same listing again keeps it.
- Until such sentences are gone, the start card says so once, with a link that opens the
  description template, and `etsyotomasyon drop template` lists them. While no description
  template is saved yet, each product of a run warns too ("Açıklamada şablon ilanın
  desenine özel 1 cümle kaldı").
- **Tasarım Yükle → Başlat** has a **Mağaza bölümü** (*Shop section*) choice: your
  shop's sections, starting on **Şablondaki gibi (…)** (the template listing's). Every
  draft of that run goes into the section you pick, or into none; the template itself
  is not changed. The choice is remembered per shop. A shop without sections says so; a
  template or remembered section deleted on Etsy is pointed out (the template's then
  gives drafts without a section instead of failing them), and a section deleted just
  before Başlat stops the run before anything is sent.
- `etsyotomasyon drop auto` and `drop run` take `--section NAME_OR_ID` (or `none`) for the
  same, checked against your shop's sections first.
- **The video's fonts are bundled**: Inter, Plus Jakarta Sans and JetBrains Mono (SIL
  Open Font License 1.1, see NOTICE.md) ship with the app, so every computer draws the
  same text. On Windows a medium weight no longer looks as bold as a semibold one (the
  active menu item stands out again), and headings and numbers use the display face.
- **Tasarım Yükle**: a design saved without transparency is no longer uploaded silently
  as a finished photo. Its row warns, and when it sits on a solid background the start
  card offers to place it on the mockups instead (the background joined to the edges is
  removed; white inside the design stays).
- The Kontrol step warns about pictures that would look soft on Etsy: a short side under
  1000 px, or a design enlarged more than twice onto a mockup.
- Every picture a draft gets carries an alt text with the design, the product and its
  colour ("Retro Mountain Sunset t-shirt, white").
- The start card says when the template's product differs from the main mockup (a mug
  template with a T-shirt as the first mockup).
- **Siparişler**: for a shop that ships from Türkiye, a note before the first send says
  that Etsy may refuse tracking numbers from newer API keys, with the CSV download and
  Etsy's orders page. It goes for good once Etsy has accepted a number.

### Changed

- The new-version check now looks at every start of the app, unless it already looked
  less than an hour ago, and then once a day while the app stays open. A failed look is
  still followed by the next one an hour later, a restart included.
- **Şablon İlan** shows how many of each listing sold, best sellers first, counted from
  the months **Kâr-Zarar** has already read (no extra Etsy calls; the tooltip names the
  months). Without those months it shows the favourites in Etsy's order, as before.
- **Şablon İlan** names the category by its first and last level in your language
  ("Giyim › Tişörtler"), shows only whole listing rows until "daha göster" is pressed,
  and keeps each field's grey bar until its value has faded in.
- **Mockuplar**: Kaydet stays dimmed until a new mockup's print area is drawn (it can
  still save the default area), the "Baskı alanı" label shows while drawing once the
  rectangle is big enough, and the chips, notices and buttons match the video.
- A download-only template's drafts use only the mockups that show no physical product
  (posters, canvases, frames) plus the flat preview; the start card names them ("Poster ·
  Meşe") and the ones left out. `drop run` and `drop auto` use the same mockups and name
  the ones they leave out.
- **İlanlar**: "N yeni taslak" counts only the drafts of etsyotomasyon's latest run; older
  drafts read "N taslak".
- **SEO → Düzelt** offers a title change only when removing whole repeated phrases ends
  the repetition.
- **Taslak İlan** names each picture's product and colour from the file the run sent,
  counts the pictures the run uploaded ("7/7 görsel"), and shows a transparent design on
  a checkerboard. The Kontrol step's toast sits top right, as in the video.
- The bell's badge stays clear after a connect the page itself already announced.
- Font weights are the video's 400/500/600/700/800 only; table column heads are 11.4 px,
  and a few labels were tightened so Inter fits a 1280 px window.

### Fixed

- A new draft's description no longer carries the template listing's own title: the
  title, and a line made of its `|`- or `,`-separated parts, become the draft's title
  (a "Sage Lemon Wallpaper | Olive Citrus Mural" line no longer reaches a woodland
  mural's draft).
- Two designs whose names differ only by their extension (`sunset.jpg` and
  `sunset.jpeg`, `poster.png` and `poster.gif`) no longer share one watermarked copy; the
  batch keeps one `watermarked` folder, so a long design name no longer doubles the path
  (Windows' 260-character limit).
- A download-only template no longer loses its posters when T-shirt mockups come first
  and info images shorten the list: the physical ones are left out before the 19-picture
  limit is applied, on the start card, in the run and in `drop run` / `drop auto`.
- An info image with the same file name as one of a product's photos goes up with its own
  alt text, and the listing page tells them apart by Etsy's image id.
- `etsyotomasyon listings push` of a `drop run` batch tells Etsy which pictures carry the
  watermark and sends the info images' alt texts, as `drop auto` and the app do.
- Alt texts read from a template listing's photos no longer keep Etsy's HTML escapes
  (`&amp;`), and a photo download that Etsy's image server redirects fails with a clear
  message instead of saving the redirect page as the picture.
- The start card's request estimate counts a JPG on a solid background as placed on
  every mockup (the "Mockup'lara yerleştir" choice), and keeps that choice when a deleted
  shop section reopens the card.
- Keyboard: the description link on **Şablon İlan**, the template photos and the info
  images' × keep the focus after a change, and the Filigran card's choices are radio
  groups moved with the arrow keys; removing or replacing the mark keeps the focus in
  the card. A watermark setting changed just before a reload is saved, Ctrl+S in the
  description dialog saves only a changed text, and "Tüm ilan görselleri" says so even
  before a template listing is chosen.
- The Filigran card's title uses the display face of the other cards, the Panel feed's
  "20 hours ago" fits a 1280 px window, and the two toast areas have their own names for
  screen readers.
- **Mağaza Bağlantısı**: once the shop is connected, "Devam" leads to the first setup step
  still to do (Mockuplar, then Şablon İlan) and to Tasarım Yükle when setup is finished,
  instead of always to Mockuplar.
- **Siparişler**: a 22-digit USPS tracking number shows whole beside its ✓ at the
  video's window width, and a cell no longer draws a stray "…" under the number's end.
- The new-version pill also folds away "Neler yeni?" when the page title would still be
  cut (English **Profit & loss** at 1280 px); Ayarlar keeps the link.

## [0.3.1] — 2026-09-28

### Fixed

- Drafts made from a template listing whose weight or size was never filled in were
  refused by Etsy: Etsy reports such a weight as 0 but accepts only values above 0. A
  weight or size of 0, or one without its unit, is now left out (with a note) instead
  of failing every draft, both for templates and for CSV rows.

### Changed

- The repository is now `wj-sidiss/etsy-otomasyon-full-full-surum`; the old address redirects.

## [0.3.0] — 2026-09-27

### Changed

- **The app now runs in your browser.** Double-clicking the download, or running
  `etsyotomasyon desktop`, starts a small server on this computer and opens
  `http://localhost:3000` (the next free port if 3000 is taken) instead of a Tk window.
  Double-clicking again opens a tab in the app that is already running. Closing the last
  tab stops the app about 90 seconds later unless a task is still running, and
  **Ayarlar → Kapat** quits at once. Turkish comes first and English second; the app
  follows the system language, and you can switch it in **Ayarlar**.
- **Only this computer can reach it.** The server listens on `127.0.0.1` and `::1` only.
  Only the tab the app opened has a session. Requests with a foreign Host or Origin are
  refused, and every response carries a strict Content Security Policy.
- **`etsyotomasyon desktop --port N --no-browser`.** `python -m etsyotomasyon desktop` works the same
  way. The downloaded app still runs any command when given arguments, for scheduled
  jobs.
- **The README is about the app now.** The command line documentation moved to
  `CLI.md`, and SETUP.md names the app's screens.
- The release check starts the packaged app's web server and fetches every file of the
  interface from it.
- **Command line:**
  - `etsyotomasyon drop run` and `etsyotomasyon drop auto` use the mockups switched on in
    **Mockuplar**, in that order (the first is the main image), as the app does. `--mockups N`
    now means the first N of that selection rather than the first N files in the folder;
    with no saved selection the result is the same as before. `drop auto` gained
    `--mockups`.
  - `etsyotomasyon listings pull` no longer writes the `views` column. Etsy's API has no view
    count, so it was always empty.
  - `etsyotomasyon seo suggest` reads the listing as plain text, so a tag such as
    `mother's day gift` is no longer measured (and flagged) as `mother&#39;s day gift`.
  - `etsyotomasyon doctor --path` treats a folder inside a products folder (`2-PRODUCTS`, ...)
    the way the `drop` commands do: it checks the products folder around it and says so.
  - `etsyotomasyon drop run` counts a digital template's download files in its request
    estimate.

### Added

The screens from the video, all in Turkish and English:

- **Mağaza Bağlantısı** (Shop connection) walks through the Seller App steps with copy
  buttons. The keys are checked with Etsy the moment they are saved. Connecting opens
  Etsy's consent page in a new tab, which closes itself when done. The screen then shows
  the permissions Etsy granted and offers a disconnect.
- **Mockuplar** (Mockups) uploads mockups and has a print-area editor. One area can be
  applied to every mockup of the same size, and you choose which mockups go into drafts.
- **Şablon İlan** (Template listing) picks the listing whose price, category, shipping,
  return policy, processing time, description and variations every new draft copies.
- **Tasarım Yükle** (Upload designs) takes designs or folders of finished photos by drag
  and drop. Each one goes through six steps (mockup, research, title, tags, check, draft),
  three at a time, with live progress, and is sent to Etsy as a draft. A run can be
  stopped. The upload history prevents duplicates, and nothing is published.
- **İlanlar** and **Taslak İlan** (Listings, Draft listing) show drafts and live
  listings. You can edit the title, tags and description, and publish one listing or many
  after a confirmation that states Etsy's listing fee. Listings can be downloaded as CSV
  or updated from one.
- **SEO** scores every listing out of 100, weakest first, and offers tag research for any
  keyword. **Düzelt** (Fix) sends the suggested change after a confirmation, and refuses
  if the listing changed on Etsy since the audit. The report downloads as CSV.
- **Siparişler** (Orders) lists waiting, shipped and delivered orders. Tracking numbers
  can be typed or loaded from a CSV, and they are sent after a confirmation.
- **Kâr-Zarar** (Profit & loss) shows revenue, Etsy fees from the payment ledger, the
  product and shipping costs you enter, and net profit and margin per month. Amounts are
  in the shop's currency and in TRY, using the Central Bank of Turkey's rate (fetched at
  most once a day) or one you type.
- **Panel** (Dashboard) shows today's numbers and quick actions.
- **Pinterest** connects your account, queues Pins from live listings and posts the ones
  that are due.
- **Ayarlar** (Settings) covers language, hiding the shop name for screenshots, several
  shops, the products folder, the ten-step setup checklist, and quitting.
- **Across the app:** a notification bell, long tasks that keep running when you move
  between screens, and a question before you leave a page with unsaved edits.
- **New-version notice.** About 10 seconds after it starts and then once a day, the app
  asks GitHub for the latest release: one GET to `api.github.com` with a
  `etsyotomasyon/<version>` User-Agent, and nothing else is sent. A newer version shows as a
  small pill in the top bar, "Yeni sürüm v0.3.1 · İndir", which opens the release page,
  with **Neler yeni?** for the release notes; the × hides it for that version. It is
  announced once in the bell. **Ayarlar** shows the version, when it last checked, a
  **Şimdi kontrol et** button and a toggle for the daily check;
  `ETSYOTOMASYON_NO_UPDATE_CHECK=1` turns every check off. A failed check (offline, GitHub's
  rate limit) is only a line in the log.
- **Etsy's trademark notice** is shown on every screen.
- **Digital products.** A template listing of type `download` (or `both`) makes drafts of
  that type, in the app and with `drop run` / `drop auto`. After its images each draft gets
  the files the buyer downloads (Etsy's `uploadListingFile`): a loose design is delivered
  as the design file itself, and its listing photos are always the mockups plus a
  1200 px preview, never the file being sold (an opaque JPG printable too); a product
  folder delivers the files in its `dosyalar` (or `files`) subfolder, while the photos in
  the folder stay the listing's images. Files in a subfolder of `dosyalar` are not sent
  (Etsy takes files, not folders): the product stops and asks for a zip. At most 5
  files per listing and 20 MB per file (Etsy's seller limits; the API spec states none);
  programs and scripts are refused. A download-only template needs no shipping profile.
  A product with nothing to deliver, too many files or an oversized file stops with its
  own message before its draft is made; the upload history records the files sent.
  A **made-to-order** template (a custom portrait, an invitation) needs no download file,
  as Etsy allows: `dosyalar` files still go when there are some, a loose design is not
  attached (it is a sample), and the draft says to add the buyer's file in Etsy.
  **Tasarım Yükle** shows **Ürün: Dijital** (*Product: Digital*; **Fiziksel + dijital** for
  *both*), its **Başlat** (*Start*) window opens with **Dijital ürün · tasarım dosyası
  indirilebilir dosya olarak eklenir** (*Digital product · the design file is attached as
  the download*), and **Şablon İlan** marks the listing **Dijital** and says **Dijital
  ürün: her taslağa tasarım dosyası indirilebilir dosya olarak eklenir** under it.
  A product folder with only its `dosyalar` and no photos is listed as *Fotoğraf yok*
  (*No photos*) and stops at the **Kontrol** (*Check*) step instead of being skipped
  silently.
- **CSV `files` column.** `listings template`, `listings pull`, the app's CSV download and
  `examples/listings.csv` have a `files` column after `images`: what a new `download` or
  `both` draft attaches, relative to the CSV. A CSV without it pushes as before.
  `listings push` and `drop auto` print the download files each row sent, and
  `push --out` has a `files_uploaded` column.

### Fixed

From reports on 0.2.0:

- **"The requested redirect URL is not permitted."** **Mağaza Bağlantısı** shows the
  callback address with a copy button and the exact place to add it in Etsy (**⋮ → Edit
  callback URLs**, which appears only after Etsy approves the app). It asks once, before
  opening Etsy, whether the address has been added. While it waits, it lists Etsy's usual
  errors and how to fix each one.
- **Checks before connecting.** The keys, the callback address and port 3003 are checked
  first, with a plain message when another program holds the port.
- **Pasted keys are cleaned up.** Spaces, line breaks and quotes are removed, and a
  single `keystring:shared_secret` line is accepted.
- **The print area is easy to find.** Every mockup card has a visible "Baskı alanını
  ayarla" (Set print area) action. A banner says how many mockups still use the default
  area, which puts the design in the middle.
- **Choosing mockups is explicit.** You pick which mockups go into drafts in a selection
  mode, with select all and none, type and colour filters, and a counter. Etsy allows 20
  images per listing, so up to 19 mockups plus the flat design. Nothing is left out
  silently, and you can change the order, and with it the main image.
- **Running from source and troubleshooting are documented.** The README and SETUP.md
  cover running from source on Windows, macOS and Linux with Python 3.9–3.13, and both
  have a troubleshooting section.
- **Digital templates were refused.** `drop auto` stopped with "Automatic upload currently
  supports physical products only" when the template listing was a digital download.
  Digital products are now supported (see Added).
- **A products folder inside the products folder.** Choosing `2-PRODUCTS` (or
  `1-MOCKUPS`, `3-DRAFTS`, or any folder inside them) as the products folder made a second
  one inside the first (`Etsy Studio\2-PRODUCTS\2-PRODUCTS`), and the mockups already in
  `Etsy Studio\1-MOCKUPS` were no longer found. **Ayarlar** and the `--path` option of the
  `drop` commands now use the products folder those belong to and say so. A folder an
  older version nested that way is pointed out in **Ayarlar** with a button to use the
  main folder, which carries its template listing and upload history over (so nothing
  already drafted is drafted again), and the folders it left inside `2-PRODUCTS`, its
  `archive` included, are never taken for products. A new folder that is only *named*
  `2-PRODUCTS` makes its parent the products folder only when that parent holds nothing
  else, and a `README.txt` etsyotomasyon did not write is never replaced.

From the review before this release:

- **Text with `&` or an apostrophe is handled correctly.** Etsy returns a seller's own
  titles, tags and descriptions HTML-escaped (`Mom&#39;s Mug &amp; Gift`). They are now
  decoded once, where they are read. As a result, edits, SEO fixes, new drafts, Pins and
  CSV files carry plain text instead of sending the entities back to Etsy.
- **Tracking numbers and Pins need the confirmation on the server too,** as publishing,
  edits and SEO fixes already did. Nothing reaches a buyer without it.
- **Kâr-Zarar counts refunds correctly.** A refund comes off revenue without its tax
  share, and a fully refunded order counts as nothing. The Panel counts revenue the same
  way. A month too large to read whole is marked as partial instead of being shown as
  complete.
- **Receipts, the payment ledger and listings are read in full.** Only Etsy's marketplace
  search stops at its first 12,000 results.
- **Titles and tags for new drafts.** The market research searches the design's name
  with the template's product when the name does not say it (`dog dad paw print` on a
  shirt template searches shirts, not posters, and the title is no longer just the name
  and "Shirt"). Free tag slots take only the template listing's tags that suit any design
  of its product, never the ones about its own design. A phrase too long for a tag leaves
  a shorter one (`leaf phone case` from `monstera leaf phone case`). A Turkish name is
  capitalised the Turkish way (`Kedi Pati İzi`) when it, or the template's text, is
  Turkish. Every screen checks a title with the same rule as a CSV push. A market phrase
  with a size such as `8.5x11` or `3/4 sleeve` is no longer cut into `8 5x11` or `3 4`.
  A tag written with a capital `İ` (`İstanbul poster`) is the same tag as `istanbul
  poster` when the SEO page suggests tags and when tags are checked for duplicates.
- **Dropping files on Tasarım Yükle.** A product folder dropped again with its new
  `dosyalar` no longer turns into a `-2` copy (the photos go up before the download
  files), and dropping only the missing `dosyalar` of a product adds it to that product.
  A corrected download file replaces the old one, which is kept in `archive`. A
  `dosyalar` folder means download files only for a digital template. A product folder
  named `1-MOCKUPS`, `2-PRODUCTS` or `3-DRAFTS` is refused instead of vanishing.
- **An image that fails on a digital draft** no longer leaves out its download files:
  they are still sent, and the message says what happened to them.

### Removed

- The Tk window, and with it the need for `tkinter`.

## [0.2.0] — 2026-09-26

### Added

- **A desktop app — no terminal, no Python.** Download one file from the Releases page:
  a single `.exe` for Windows, or `etsyotomasyon.app` for Apple Silicon Macs. The everyday
  commands have buttons, grouped into Setup, Upload products, Listings, Orders, SEO and
  Pinterest tabs, with a log underneath showing exactly what ran and what came back. The buttons
  run the same commands as the terminal, so validation, dry runs and error messages are
  identical. Anything that reaches the live shop asks first in a dialog. The window is
  in English and Turkish and follows the system language. Keys and tokens live in
  `~/.etsyotomasyon`, shared with the command line.
- **`etsyotomasyon desktop`** opens the same window from an installed copy, and the
  downloaded app runs any command when given arguments (`etsyotomasyon.exe pinterest post`),
  so it can be scheduled without Python installed.
- **Connecting a shop, step by step.** The Setup tab walks through Etsy's *Seller App*
  (July 2026: two fields, usually approved in minutes) with everything to paste into
  Etsy's form behind a Copy button, checks the keys the moment they are saved, and ticks
  each step as it is done. It tells apart keys Etsy refused, a shop not yet connected, a
  sign-in that expired, and Etsy being unreachable. Waiting for the browser can be
  cancelled. The page Etsy sends the browser back to now says, in English and Turkish,
  to return to the app.
- **Several shops on one computer.** Each shop has its own keys, sign-in, Pin queue and
  products folder. The window has a shop picker with *Add a shop*; the command line has
  `etsyotomasyon shops list|add|remove` and a global `--shop <id>` (or ETSYOTOMASYON_SHOP, checked
  the same way). One shop keeps everything in `~/.etsyotomasyon`, exactly as before; a further
  shop never reads a `.env` from the working directory, so it cannot borrow another
  shop's keys.

### Changed

- **`etsyotomasyon init` writes `~/.etsyotomasyon/.env`** (the selected shop's home with `--shop`)
  instead of `./.env`, so the keys it saves are the ones the desktop app reads. `--path`
  still writes anywhere, and a `./.env` is still read for the first shop.
- **Etsy's trademark notice** is shown in the window and the README, as Etsy's API Terms
  require of every application.
- **Release builds on GitHub.** Pushing a version tag builds both apps on GitHub's
  runners, checks that each packaged app starts and builds its window, and publishes
  them to a GitHub Release.
- **Pinterest, optional.** `etsyotomasyon pinterest` turns an active listing's photos into
  Pins linking back to it, on the seller's own Pinterest account through their own app.
  Pins are queued and posted a few a day across the whole queue, never twice for the
  same image and board, and a Pin that was sent but not confirmed is parked for a human
  rather than re-sent. `--ai-modified` sets Pinterest's AI disclosure.
- **Variations on new drafts.** `listings push --inventory-from <listing_id>` copies that
  listing's options — properties, per-option prices and quantities, and processing
  profile — onto every draft it creates, and `drop auto` does the same from its template
  listing. A draft whose options cannot be set is reported as partial, never as done.
- **Processing profiles.** `readiness_state_id` is a listing column, captured by
  `drop template` and sent on create. When it is present the older processing day counts
  are not sent alongside it.

### Fixed

- **An `https://localhost` callback no longer hangs the sign-in.** The local listener
  speaks plain HTTP, so it is now used only for `http://localhost`; an https callback
  takes the paste flow.
- **A token request Etsy refuses for its format is retried as JSON.** Some apps get a
  403 "should be in the format 'keystring:shared_secret'" for a form-encoded token
  request that Etsy accepts as JSON (etsy/open-api#1678).
- **A refused tracking upload says why.** Etsy restricts tracking uploads for newer API
  keys in many countries, Türkiye included; the 403 now says so instead of suggesting a
  missing scope.
- **Etsy's error text on the local sign-in page is escaped.**
- **A listing takes twenty images, not ten.** Etsy's API schema allows up to 20, and a
  listing with more than ten photos was refused locally for no reason.
- **Physical drafts are accepted by Etsy again.** Etsy now refuses a physical create
  without a processing profile; the template's profile is carried onto the draft.
- **A template with variations no longer poisons every draft's quantity.** Etsy reports
  a varied listing's quantity as the total across its options, far above the 999 it
  accepts on a create. The copied figure is capped, and anything over 999 is caught
  locally before it is sent.

## [0.1.0] — 2026-09-25

First public release.

### Added

- **Setup that checks as it goes.** `etsyotomasyon init` writes `.env` with the shared secret
  typed hidden and verifies the credential against Etsy. `etsyotomasyon setup` walks every
  prerequisite and names the single next command; `etsyotomasyon doctor` runs the same
  checklist without asking anything, for scripts.
- **OAuth 2.0 with PKCE.** A one-shot `localhost` listener catches the redirect, or you
  paste the address back. Tokens live in `~/.etsyotomasyon/token.json` with `0600`
  permissions and refresh themselves, including mid-batch.
- **Bulk listings from a spreadsheet.** `listings template`, `listings pull` and
  `listings push`. An empty `listing_id` creates a draft, a filled one updates. The whole
  file is validated before anything is sent — titles, the 13-tag and 20-character rules,
  enum values, prices, image count, format and size — and one bad row stops the run
  unless `--partial` is given. New listings are always drafts.
- **Orders and tracking.** `orders pull` exports orders to CSV (`--since`, `--unshipped`),
  `orders carriers` lists valid carrier names per country, and `orders ship` uploads
  tracking in bulk after a dry run and a confirmation.
- **SEO.** `seo audit` scores every listing out of 100, worst first, plus shop-level
  checks for listings competing for the same searches. `seo keywords` samples what
  actually ranks for a term — tags, title phrases, price band — with only a keystring.
  `seo suggest` combines both for one listing.
- **Designs in, drafts out.** `drop init` creates a workspace folder; `drop template`
  copies business settings from a listing you built by hand; `drop run` composites
  loose artwork onto your mockups, researches each concept, writes titles and tags
  within Etsy's limits and produces a `review.csv` without sending anything; `drop auto`
  uploads folders of finished photos as drafts, with an upload history that prevents
  duplicates.
- **Print-area calibration.** `drop calibrate` sets where a design lands on each mockup,
  shares it with every mockup of the same size, imports a pixel-based file, and draws
  the rectangle on the mockup so it can be checked before it is saved.
- **Images that match what the seller sees.** Phone photos are turned upright from their
  EXIF orientation, colour profiles are converted to sRGB, transparent templates are
  flattened onto white, 16-bit greyscale keeps its tone, and JPEGs keep full colour
  resolution. Formats Etsy refuses are converted, and the row says so.
- **`--anonymise`** hides shop name, ids, titles, URLs and tags in terminal output so a
  screenshot can be shared.

### Notes on Etsy's API

Recorded here and in the code so nobody has to re-derive them:

- `x-api-key` must carry **both** the keystring and the shared secret, colon-joined, on
  every request including unauthenticated ones. PKCE removes the client secret from the
  *token exchange* only — not from this header.
- Callback URLs may be `http://` or `https://`, and the host must be a **domain name**.
  `localhost` is accepted; `127.0.0.1` is rejected. Etsy's prose docs say https-only,
  which is narrower than what is enforced and leads you to build the wrong flow.
- Rate limits are **per app**. A Personal Access app gets 5 requests/second and 5,000
  per day — not the 10/sec and 10,000/day the general documentation quotes.
- Array form fields such as `tags` and `materials` are **comma-joined strings**, not
  repeated keys. Repeated keys silently drop all but one value.
- `createDraftListing` takes form encoding; `createReceiptShipment` takes JSON.
- Listing images must be JPG, PNG or GIF, at most 20MB, and at most twenty per listing.
- A physical listing needs a processing profile (`readiness_state_id`) on create, and a
  listing's quantity may not exceed 999 — though a listing with variations *reports* the
  total across its options, which usually does.
- There is no idempotency key, so non-idempotent writes are never retried on a timeout
  or a 5xx — a repeat would mean a duplicate listing, or a second email to a buyer.

[Unreleased]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/releases/tag/v0.3.1
[0.3.0]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/releases/tag/v0.3.0
[0.2.0]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/releases/tag/v0.2.0
[0.1.0]: https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/releases/tag/v0.1.0
