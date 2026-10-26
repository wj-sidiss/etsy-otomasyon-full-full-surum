# Setup

**Türkçe için [aşağı kaydırın](#kurulum-türkçe).**

Nothing here is optional. Miss any one of these and connecting fails, usually with an
unhelpful Etsy error or a `403`. The good news is that you never have to work out *which*
one you missed:

- **In the app:** **Ayarlar → Kontrol listesi → Her şeyi kontrol et** (Settings →
  Checklist → Check everything) checks the ten steps below and has a **Fix** link next to
  anything missing.
- **On the command line:** `etsyotomasyon setup` walks the same list, asks about the parts no
  program can check, and ends with the single next command to run.

> **Using the app?** Steps 1–2 are done for you if you downloaded it. Steps 3–9 are the
> **Mağaza Bağlantısı** (Shop connection) screen, top to bottom. Step 10 is **Mockuplar**
> and **Şablon İlan**. If something goes wrong, see [Troubleshooting](#troubleshooting).

---

## What you need before you start

| # | Thing | Why it cannot be skipped |
|---|---|---|
| 1 | **Python 3.9–3.13**, only when running from source | The downloaded app has Python inside it. |
| 2 | **etsyotomasyon**: the downloaded app, or `pip install -e .` | [README → Download](README.md#download-and-first-run) or [Run from source](README.md#run-from-source-windows-macos-linux). |
| 3 | **An Etsy shop that is open** | etsyotomasyon manages a shop. It cannot create one. |
| 4 | **An Etsy API app** | Free: [Create a seller app](https://www.etsy.com/developers/register-seller-app). |
| 5 | **Keystring AND shared secret** | Etsy needs **both**, colon-joined, on every request. |
| 6 | **A callback URL saved in etsyotomasyon** | OAuth cannot start without one. The app uses `http://localhost:3003/oauth/redirect`. |
| 7 | **That same URL registered on your Etsy app** | Etsy checks it character for character. |
| 8 | **Etsy accepting the credential** | Proves 5 is right before you trust it with a batch. |
| 9 | **Your shop connected** | **Bağlan** in the app, or `etsyotomasyon auth login`. |
| 10 | *(for Tasarım Yükle / `etsyotomasyon drop`)* **mockups and a template listing** | Fields no image can supply. |

---

## Step by step

### 1–2. Install

The downloaded app needs nothing else: see [README → Download and first
run](README.md#download-and-first-run). From source, with Python 3.9–3.13:

```bash
git clone https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum-2.git
cd etsyotomasyon
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e .
etsyotomasyon --version
etsyotomasyon desktop                # opens the app in your browser
```

On Windows without activation: `.venv\Scripts\python -m pip install -e .`, then
`.venv\Scripts\etsyotomasyon desktop`.

### 3. Have an Etsy shop

If you do not have one yet, open it at <https://www.etsy.com/sell> first. This tool
works on an existing shop. It does not create one, and it never publishes anything
unless you ask it to.

### 4. Create an API app

Since July 2026 Etsy offers a **Seller App**: an app for your own shop, two fields,
usually approved within minutes. Signed in with the shop's account, open
<https://www.etsy.com/developers/register-seller-app> (*Create a seller app*).

- **App name:** anything without the word "Etsy", which Etsy's trademark rules refuse.
- **Why you want to use the API:** say plainly that it is your own tool for your own
  shop, running on your computer. The app has this text behind a Copy button: *"I manage
  my own shop with a tool that runs on my own computer: creating draft listings in bulk
  from my product photos, updating my listings and adding tracking numbers to my orders.
  It connects only to my shop, and the keys stay on my computer."* Then press **Read
  Terms and Create App**.

Etsy allows one app per account. If you already have one (an older *personal* key), use
its keys instead. Do not switch on **Developer Mode** in the developer settings: it hides
your shop from search.

Once approved, <https://www.etsy.com/developers/your-apps> shows two values:

- a **Keystring**, which works like a username and is semi-public;
- a **Shared secret** (eye icon), which is a real secret: treat it like a password.

> **You need both.** This trips almost everyone up, because OAuth with PKCE is described
> as removing the client secret, and it does, from the *token exchange*. It does **not**
> remove the shared secret from the `x-api-key` header, which every single request
> carries. With the keystring alone, Etsy replies:
>
> ```
> 403 {"error":"Invalid API key: should be in the format 'keystring:shared_secret'."}
> ```

### 5. Save the keys

- **In the app:** paste both into **Mağaza Bağlantısı** and press **Kaydet ve kontrol et**
  (Save and check). Etsy is asked straight away whether it accepts them.
- **On the command line:** `etsyotomasyon init` asks for the keystring, then the shared secret
  **with the typing hidden**, writes `~/.etsyotomasyon/.env` with `0600` permissions and
  checks the credential against Etsy.

Both write the same file, `~/.etsyotomasyon/.env`. Never commit it and never paste it into an
issue. Prefer to do it by hand? `cp .env.example ~/.etsyotomasyon/.env` and fill in the values.

### 6–7. The callback URL

Etsy's own app settings screen states the rules:

> - Must start with `http://` or `https://`
> - Host must be a **domain name** (e.g. `example.com`)
> - **IP addresses are not allowed** (e.g. `127.0.0.1`)

`localhost` counts as a domain name, so etsyotomasyon uses:

```
http://localhost:3003/oauth/redirect
```

The app saves this address for you. Add the identical string to your app on Etsy: open
<https://www.etsy.com/developers/your-apps>, click **⋮** next to the app → **Edit callback
URLs**, paste it and save. The Seller App form has no callback field; the **⋮** menu
appears only after Etsy approves the app. It must be `http` (not https), `localhost` (not
127.0.0.1), port `3003`, with no slash at the end. etsyotomasyon then catches the redirect
itself and you copy nothing.

> Etsy's written documentation says the callback must use `https`. Taken literally that
> rules out a local listener, but the settings screen accepts `http://localhost:PORT/…`
> and that is what is actually enforced. Use `127.0.0.1` and it *will* be rejected. That
> is the real constraint, and it cost this project a rewrite to establish.

On the command line, any other host works too: Etsy sends your browser there, the page
does not need to exist, and you paste the address back in once.

### 8–9. Connect

- **In the app:** press **Bağlan** (Connect) on **Mağaza Bağlantısı**. Etsy's consent
  page opens in a new tab; approve it and the app shows your shop as connected. Keep
  etsyotomasyon open while you approve; it waits up to 5 minutes.
- **On the command line:** `etsyotomasyon setup` confirms steps 1–8, then `etsyotomasyon auth login`
  opens the browser.

The token lands in `~/.etsyotomasyon/token.json` with `0600` permissions, lasts an hour, and
refreshes itself. The refresh token lasts 90 days, so you connect again about once a
quarter at most.

etsyotomasyon asks for the minimum it needs and **never asks for a delete scope**. It cannot
delete a listing even if it is compromised.

### 10. Mockups and a template listing

**You must build one listing in Etsy by hand first.** `taxonomy_id`,
`shipping_profile_id`, `return_policy_id`, `who_made`, `when_made`, processing times and
price are decisions about a business, not facts about a picture. Guessing them would put
wrong listings in a real shop, so etsyotomasyon copies yours instead.

- **In the app:** add your product photos on **Mockuplar** and set each print area, then
  pick that listing on **Şablon İlan**. **Tasarım Yükle** is then ready.
- **On the command line:** `etsyotomasyon drop init`, then
  `etsyotomasyon drop template --from-listing <listing_id>`. Put mockups in `1-MOCKUPS`,
  designs in `2-PRODUCTS`, and run `etsyotomasyon drop run` (see [CLI.md](CLI.md)). `drop run`
  and `drop auto` use the mockups switched on in **Mockuplar**, in that order;
  `--mockups N` takes the first N of them.

**Digital products.** If the template listing is a digital download (Etsy's type
*download*, or *both*), every draft is made that type and gets the buyer's files after its
images. A loose design is delivered as the design file itself. A product folder delivers
the files in its `dosyalar` (or `files`) subfolder, such as PDF, ZIP, PNG or SVG; the photos
in the folder itself become the images. Etsy takes at most 5 files per listing, each up to
20 MB. A download-only template needs no shipping profile; a *both* template does.
**Tasarım Yükle** then shows **Ürün: Dijital** (*Product: Digital*), the **Başlat**
(*Start*) window opens with **Dijital ürün · tasarım dosyası indirilebilir dosya olarak
eklenir** (*Digital product · the design file is attached as the download*), and
**Şablon İlan** says **Dijital ürün: her taslağa tasarım dosyası indirilebilir dosya olarak
eklenir** under the chosen listing. A product folder still needs its photos: one with only
a `dosyalar` folder shows *Fotoğraf yok* (*No photos*) and stops at the **Kontrol**
(*Check*) step.

**The products folder** is `Etsy Studio` on your Desktop. To use another one, open
**Ayarlar → Klasörler → Değiştir** (Settings → Folders → Change), or pass `--path` to the
`drop` commands. Pick the folder that holds `1-MOCKUPS` and `2-PRODUCTS`. If you pick one
of those (or a folder inside them), etsyotomasyon uses the folder they belong to instead of
making a second products folder inside the first, and tells you.

---

## Before your API app is approved

You are not blocked. These work with no key and no network at all:

```bash
etsyotomasyon listings template -o products.csv     # a starter CSV
etsyotomasyon listings push products.csv --dry-run  # validates every row offline
etsyotomasyon orders ship tracking.csv --dry-run
```

The dry run checks title length, the 13-tag ceiling, 20 characters per tag, Etsy's tag
character set, every enum, required fields, and whether each image file exists. You can
have 300 products validated and ready before Etsy replies.

---

## Troubleshooting

Run the checklist first (**Ayarlar → Her şeyi kontrol et**, or `etsyotomasyon setup`). It
usually names the problem outright.

### Connecting the shop

**Etsy says "The requested redirect URL is not permitted"**
(Turkish: *"İstenen yönlendirme URL'sine izin verilmiyor"*). The callback address is not
registered on your Etsy app. Open <https://www.etsy.com/developers/your-apps>, click **⋮**
next to your app → **Edit callback URLs**, add exactly
`http://localhost:3003/oauth/redirect` and save: `http` not `https`, `localhost` not
`127.0.0.1`, no slash at the end. The easiest way is the copy button next to the address
on **Mağaza Bağlantısı**. Then press **Bağlan** again.

**There is no "Edit callback URLs" menu.** Your app has not been approved yet. Approval
usually takes minutes, and the menu appears once it is approved.

**"Etsy refused these keys"** (or *invalid client*). Copy the Keystring and the Shared
secret from the same app, without spaces. Both are needed, and a brand-new app's keys do
not work until the app is approved.

**After approving, the browser says "This site can't be reached" on `localhost:3003`.**
etsyotomasyon must be open while you approve; it waits 5 minutes. Go back to etsyotomasyon and
press **Bağlan** again. If another program uses port 3003, close that program.

**Etsy refused the app name.** Etsy does not accept app names containing "Etsy". Choose
another name. Do not switch on Developer Mode.

**Sending tracking numbers fails with 403.** Since 2024 Etsy has restricted adding
tracking numbers with newer API keys in many countries, Türkiye included. Enter them in
Etsy's Shop Manager.

### Running the app

**Windows says "Windows protected your PC".** The app is not code-signed. Click **More
info → Run anyway**.

**macOS says the app cannot be opened.** Open **System Settings → Privacy & Security**,
scroll down and click **Open Anyway**.

**"This page only opens from the etsyotomasyon app."** The address was opened by hand or from
a bookmark, without the app's session. Double-click etsyotomasyon (or run `etsyotomasyon desktop`)
again; it opens a signed-in tab in the app that is already running.

**"Cannot reach etsyotomasyon."** The app has stopped. It stops about 90 seconds after its
last tab closes when nothing is running, and after **Ayarlar → Kapat**. Double-click it
again and reload the page. Nothing is lost.

**Port 3000 is taken by another program.** etsyotomasyon uses the next free port (3001, 3002,
3004 and so on) and opens the browser there. From source, `etsyotomasyon desktop --port N`
picks one.

**There is a `2-PRODUCTS` inside `2-PRODUCTS`, and mockups or designs are not found.** An
older version made a second products folder when `2-PRODUCTS` was chosen as the products
folder. **Ayarlar → Klasörler** points it out: press **Use the main folder** (*Ana klasörü
kullan*), then move the mockups and the designs not uploaded yet from the inner folders
into the main folder's `1-MOCKUPS` and `2-PRODUCTS`.

**A digital product stops before its draft.** It has nothing a buyer could download, or
too much: a product folder needs a `dosyalar` (or `files`) subfolder with at most 5 files,
each up to 20 MB. Programs and scripts (`.exe`, `.bat` and the like) cannot be sold as
downloads; zip several files together if there are more than 5.

**A product folder shows *Fotoğraf yok* (No photos) and stops at Kontrol (Check).** It holds
only its `dosyalar` folder. Every listing needs at least one photo: put the product's photos
(01, 02, ...) in the product folder itself, next to `dosyalar`. `drop run` skips such a
folder with the same reason, and `drop auto` stops before uploading anything.

### Error messages

| Symptom | Cause |
|---|---|
| `403 Invalid API key: should be in the format 'keystring:shared_secret'` | Only one half is set. See step 5. |
| `403 API key not found or not active` | Both halves are set but one is wrong, or the app is not approved yet. Re-check both on your app page. |
| `403 Forbidden` on a specific command | A missing scope. `etsyotomasyon auth status` shows what Etsy *granted*. Connect again. |
| Etsy says `redirect_uri is not valid` | Step 7. Compare character by character, including the port and any trailing slash. |
| `Etsy does not accept IP addresses` | Use `localhost`, not `127.0.0.1`. |
| `Cannot listen on 127.0.0.1:3003` | Another program holds the port. Close it, or on the command line pick another port and change it in **both** places. |
| `400` when creating a listing | Usually a `taxonomy_id` that is not a leaf category, or a missing `shipping_profile_id` (a physical or *both* template needs one; a download-only template does not). `--dry-run` catches most of it. |
| Turkish characters look wrong in Excel | Your spreadsheet did not save as UTF-8. etsyotomasyon always writes UTF-8 with BOM. |

---

## Two things worth knowing

**Rate limits are per app.** A Personal Access app gets **5 requests/second and 5,000 per
day**, not the 10/sec and 10,000/day the general docs quote, which is the commercial
tier. Your app's real allowance is printed on its row at
[your-apps](https://www.etsy.com/developers/your-apps). etsyotomasyon defaults to 4/second, and
**Panel** shows the requests left for today.

**Order exports contain your customers' personal data**: names, email addresses, postal
addresses, gift messages. `.gitignore` covers `*.csv` for exactly this reason, but a file
you move elsewhere is no longer protected.

---
---

# Kurulum (Türkçe)

Buradaki hiçbir adım isteğe bağlı değil. Biri eksikse bağlantı kurulamaz, genelde de
anlaşılmaz bir Etsy hatası ya da `403` ile. İyi haber: **hangisinin eksik olduğunu bulmak
size kalmıyor.**

- **Uygulamada:** **Ayarlar → Kontrol listesi → Her şeyi kontrol et** aşağıdaki on adımı
  kontrol eder, eksik olanın yanında **Düzelt** bağlantısı çıkar.
- **Komut satırında:** `etsyotomasyon setup` aynı listeyi gezer, programın kontrol edemeyeceği
  kısımları size sorar ve sonunda çalıştırmanız gereken **tek komutu** yazar.

> **Uygulamayı mı kullanıyorsunuz?** İndirdiyseniz 1–2. adımlar hazır. 3–9. adımlar
> **Mağaza Bağlantısı** ekranında, yukarıdan aşağı sırayla. 10. adım **Mockuplar** ve
> **Şablon İlan**. Bir sorun çıkarsa [Sık karşılaşılan hatalar](#sık-karşılaşılan-hatalar).

## Gerekenler

| # | Ne | Neden atlanamaz |
|---|---|---|
| 1 | **Python 3.9–3.13**, yalnızca kaynak koddan çalıştırırken | İndirilen uygulamanın içinde Python var. |
| 2 | **etsyotomasyon**: indirilen uygulama ya da `pip install -e .` | [README'deki Türkçe bölüm](README.md#türkçe): indirme ya da kaynak koddan çalıştırma. |
| 3 | **Açık bir Etsy mağazanız** | Araç mağaza yönetir, mağaza açmaz. |
| 4 | **Etsy API uygulaması** | Ücretsiz: [Seller App oluşturun](https://www.etsy.com/developers/register-seller-app). |
| 5 | **Keystring VE shared secret** | Etsy her istekte **ikisini birden** ister. |
| 6 | **etsyotomasyon'te kayıtlı bir geri dönüş (callback) adresi** | OAuth onsuz başlamaz. Uygulama `http://localhost:3003/oauth/redirect` kullanır. |
| 7 | **Aynı adresin Etsy uygulamanızda kayıtlı olması** | Etsy harfi harfine karşılaştırır. |
| 8 | **Etsy'nin anahtarları kabul etmesi** | Toplu işe girişmeden önce 5. adımın doğru olduğunu kanıtlar. |
| 9 | **Mağazanızın bağlı olması** | Uygulamada **Bağlan**, komut satırında `etsyotomasyon auth login`. |
| 10 | *(Tasarım Yükle / `etsyotomasyon drop` için)* **mockup'lar ve bir şablon ilan** | Görselden çıkarılamayan alanlar. |

## Adımlar

**1–2. Kurulum.** İndirilen uygulama başka bir şey istemez. Kaynak koddan: depoyu klonlayın,
sanal ortam kurun, `pip install -e .`, sonra `etsyotomasyon desktop` (ayrıntılar
[README](README.md#kaynak-koddan-çalıştırma-windows-macos-linux)'de).

**3.** Etsy mağazanız yoksa önce <https://www.etsy.com/sell> adresinden açın. Bu araç var
olan bir mağaza üzerinde çalışır ve siz istemedikçe **hiçbir şeyi yayınlamaz**; yeni
ilanlar taslak olarak gider.

**4.** Temmuz 2026'dan beri Etsy satıcılara kendi mağazaları için **Seller App** veriyor:
iki alanlı bir form, onay genelde birkaç dakika. Mağazanızın hesabıyla giriş yapıp
<https://www.etsy.com/developers/register-seller-app> adresini açın (*Create a seller app*).

- **App name:** içinde "Etsy" geçmeyen herhangi bir ad; Etsy'nin marka kuralları "Etsy"
  içeren adı reddeder.
- **Why you want to use the API:** kendi bilgisayarınızda çalışan, yalnızca kendi
  mağazanıza bağlanan kendi aracınız olduğunu açıkça yazın. Uygulamada bu metin bir
  Kopyala düğmesiyle hazır (Etsy'nin formu İngilizce olduğu için metin de İngilizce;
  yukarıdaki İngilizce bölümde de var). Sonra **Read Terms and Create App**'e basın.

Etsy hesap başına bir uygulamaya izin verir. Zaten bir uygulamanız (eski bir *personal*
anahtar) varsa onun anahtarlarını kullanın. Geliştirici ayarlarındaki **Developer
Mode**'u açmayın, mağazanızı aramada gizler.

Onaylanınca <https://www.etsy.com/developers/your-apps> sayfasında **Keystring** ve
**Shared secret** görünür (secret için göz ikonu).

> **İkisi de gerekli.** Herkesin takıldığı yer burası: PKCE'nin "client secret'ı
> kaldırdığı" söylenir; doğru, ama yalnızca *token değişiminden* kaldırır. Her isteğin
> taşıdığı `x-api-key` başlığından kaldırmaz. Yalnızca keystring ile Etsy şunu döner:
>
> ```
> 403 {"error":"Invalid API key: should be in the format 'keystring:shared_secret'."}
> ```

**5. Anahtarları kaydedin.** Uygulamada ikisini **Mağaza Bağlantısı**'na yapıştırıp
**Kaydet ve kontrol et**'e basın; Etsy'ye hemen sorulur. Komut satırında `etsyotomasyon init`
keystring'i sorar, shared secret'ı **gizli girişle** alır, `~/.etsyotomasyon/.env` dosyasını
`0600` izinle yazar ve anahtarı Etsy'ye doğrulatır. İkisi de aynı dosyaya yazar. Bu
dosyayı asla commit etmeyin, asla bir issue'ya yapıştırmayın.

**6–7. Geri dönüş adresi.** Etsy'nin kendi ayar ekranındaki kurallar: `http://` veya
`https://` olacak, host bir **alan adı** olacak, **IP adresi kabul edilmiyor**. `localhost`
bir alan adı sayılır, o yüzden etsyotomasyon şunu kullanır:

```
http://localhost:3003/oauth/redirect
```

Uygulama bu adresi sizin için kaydeder. Birebir aynısını Etsy'de uygulamanıza ekleyin:
<https://www.etsy.com/developers/your-apps> → uygulamanızın yanındaki **⋮** → **Edit callback
URLs** → yapıştırın → kaydedin. Seller App formunda bu alan yok; **⋮** menüsü ancak Etsy
uygulamayı onayladıktan sonra görünür. `http` olmalı (https değil), `localhost` olmalı
(127.0.0.1 değil), port `3003`, sonunda `/` olmamalı. Sonra etsyotomasyon yönlendirmeyi
kendisi yakalar, siz hiçbir şey kopyalamazsınız.

> Etsy'nin yazılı dokümanı "https şart" diyor. Harfiyen alırsanız yerel dinleyici imkânsız
> görünür, ama ayar ekranı `http://localhost:PORT/…` adresini kabul ediyor ve asıl
> uygulanan bu. `127.0.0.1` yazarsanız **reddedilir.**

**8–9. Bağlanın.** Uygulamada **Mağaza Bağlantısı**'nda **Bağlan**'a basın; Etsy'nin onay
sayfası yeni sekmede açılır, izin verince uygulama mağazanızı bağlı gösterir. İzin
verirken etsyotomasyon açık kalmalı, en fazla 5 dakika bekler. Komut satırında önce
`etsyotomasyon setup`, sonra `etsyotomasyon auth login`. Oturum `~/.etsyotomasyon/token.json` içine
`0600` izinle yazılır, bir saat geçerlidir ve kendini yeniler. Yenileme anahtarı 90 gün
geçerlidir.

etsyotomasyon yalnızca ihtiyacı olan izinleri ister ve **silme izni hiç istemez**; ele geçirilse
bile bir ilanı silemez.

**10. Mockup'lar ve şablon ilan.** **Önce Etsy'de bir ilanı elle, düzgünce açmanız
gerekir.** Kategori, kargo profili, iade politikası, üretim bilgisi, işleme süresi ve
fiyat bir işletmeye dair kararlardır, bir görsele dair olgular değil. Bunları tahmin etmek
mağazanıza yanlış ürün sokar; o yüzden etsyotomasyon sizinkini kopyalar. Uygulamada
**Mockuplar**'a ürün fotoğraflarınızı ekleyip baskı alanlarını ayarlayın, sonra **Şablon
İlan**'dan o ilanı seçin. Komut satırında `etsyotomasyon drop init`, sonra
`etsyotomasyon drop template --from-listing <listing_id>`. `drop run` ve `drop auto`,
**Mockuplar**'da açık olan mockup'ları o sırayla kullanır; `--mockups N` bunların ilk N
tanesini alır.

**Dijital ürünler.** Şablon ilan dijital bir ürünse (Etsy'deki türü *download* ya da
*both*), her taslak o türde açılır ve görsellerinden sonra alıcının dosyaları eklenir. Tek
başına bir tasarımda alıcı tasarım dosyasının kendisini indirir. Bir ürün klasöründe,
içindeki `dosyalar` (ya da `files`) alt klasöründeki PDF, ZIP, PNG, SVG gibi dosyalar
eklenir; klasörün kendisindeki fotoğraflar ilanın görselleri olur. Etsy bir ilana en fazla 5
dosya alır, her biri en fazla 20 MB. Yalnızca dijital bir şablon için kargo profili
gerekmez; *both* için gerekir. Bu durumda **Tasarım Yükle** üstte **Ürün: Dijital**
gösterir, **Başlat**'a basınca açılan pencere **Dijital ürün · tasarım dosyası
indirilebilir dosya olarak eklenir** satırıyla başlar, **Şablon İlan** da seçili ilanın
altında **Dijital ürün: her taslağa tasarım dosyası indirilebilir dosya olarak eklenir**
yazar. Ürün klasörünün fotoğrafları yine gerekir: içinde yalnızca `dosyalar` klasörü olan
bir ürün *Fotoğraf yok* olarak görünür ve **Kontrol** adımında durur.

**Ürün klasörü** masaüstünüzdeki `Etsy Studio`'dur. Başka bir klasör için **Ayarlar →
Klasörler → Değiştir**'i kullanın ya da `drop` komutlarına `--path` verin. `1-MOCKUPS` ve
`2-PRODUCTS`'ı içeren klasörü seçin. Bunlardan birini (ya da içlerindeki bir klasörü)
seçerseniz etsyotomasyon, ilkinin içine ikinci bir ürün klasörü açmak yerine onların ait olduğu
klasörü kullanır ve bunu söyler.

## API onayınız gelmeden de çalışır

```bash
etsyotomasyon listings push urunler.csv --dry-run
```

Anahtarsız ve internetsiz çalışır: başlık uzunluğu, 13 etiket sınırı, etiket başına 20
karakter, Etsy'nin izin verdiği karakterler, tüm seçenek değerleri, zorunlu alanlar ve
görsel dosyalarının gerçekten var olup olmadığı kontrol edilir.

## Sık karşılaşılan hatalar

Önce kontrol listesini çalıştırın (**Ayarlar → Her şeyi kontrol et** ya da
`etsyotomasyon setup`); genelde sorunu doğrudan söyler.

**Etsy "İstenen yönlendirme URL'sine izin verilmiyor" diyor**
(*The requested redirect URL is not permitted*). Geri dönüş adresi Etsy uygulamanızda
kayıtlı değil. <https://www.etsy.com/developers/your-apps> → uygulamanızın yanındaki **⋮** →
**Edit callback URLs** → şu adresi **birebir** ekleyip kaydedin:
`http://localhost:3003/oauth/redirect`. `https` değil `http`, `127.0.0.1` değil
`localhost`, sonunda `/` yok. En kolayı **Mağaza Bağlantısı**'ndaki adresin yanındaki
kopyalama düğmesi. Sonra **Bağlan**'a tekrar basın.

**"Edit callback URLs" menüsü yok.** Uygulamanız henüz onaylanmamış. Onay genelde birkaç
dakika sürer; onaylanınca menü çıkar.

**"Etsy bu anahtarları kabul etmedi"** (ya da *invalid client*). Keystring ve Shared secret
aynı uygulamadan, boşluksuz kopyalanmalı. İkisi birden gerekir. Uygulama onaylanmadan
anahtarlar çalışmaz.

**Etsy'de izin verdim ama tarayıcı `localhost:3003` için "Bu siteye ulaşılamıyor" diyor.**
İzin verirken etsyotomasyon açık olmalı; uygulama 5 dakika bekler. etsyotomasyon'e dönüp **Bağlan**'a
tekrar basın. 3003 portunu başka bir program kullanıyorsa o programı kapatın.

**Uygulama adı reddedildi.** Etsy, adında "Etsy" geçen uygulamaları kabul etmiyor. Başka bir
ad seçin. Developer Mode'u açmayın.

**Takip numarası yüklenmiyor (403).** Etsy, 2024'ten beri yeni API anahtarlarıyla takip
numarası eklemeyi Türkiye dahil birçok ülkede kısıtlıyor. Takip numaralarını Etsy Mağaza
Yöneticisi'nden girin.

**Windows "Windows kişisel bilgisayarınızı korudu" diyor.** Uygulama imzalı değil. **Ek
bilgi → Yine de çalıştır**'a tıklayın.

**macOS uygulamanın açılamayacağını söylüyor.** **Sistem Ayarları → Gizlilik ve
Güvenlik**'e girin, aşağı kaydırın ve **Yine de Aç**'a tıklayın.

**"Bu sayfa yalnızca etsyotomasyon uygulamasından açılır."** Adres elle ya da bir yer
işaretinden, uygulamanın oturumu olmadan açıldı. etsyotomasyon'e yeniden çift tıklayın (ya da
`etsyotomasyon desktop`); çalışan uygulamada oturumlu bir sekme açar.

**"etsyotomasyon'e ulaşılamıyor."** Uygulama kapanmış. Son sekmesi kapandıktan yaklaşık 90
saniye sonra, devam eden bir iş yoksa kendiliğinden kapanır; **Ayarlar → Kapat** da
kapatır. Yeniden çift tıklayıp sayfayı yenileyin, hiçbir şey kaybolmaz.

**3000 portunu başka bir program kullanıyor.** etsyotomasyon bir sonraki boş portu (3001, 3002,
3004…) kullanır ve tarayıcıyı orada açar. Kaynak koddan `etsyotomasyon desktop --port N` ile
port seçebilirsiniz.

**`2-PRODUCTS`'ın içinde bir `2-PRODUCTS` daha var, mockup'lar ya da tasarımlar
bulunmuyor.** Eski bir sürüm, ürün klasörü olarak `2-PRODUCTS` seçilince içine ikinci bir
ürün klasörü açıyordu. **Ayarlar → Klasörler** bunu gösterir: **Ana klasörü kullan**'a
basın, sonra içteki klasörlerden mockup'ları ve henüz yüklenmemiş tasarımları ana
klasördeki `1-MOCKUPS` ve `2-PRODUCTS`'a taşıyın.

**Dijital bir ürün taslağa geçmeden duruyor.** Alıcının indirebileceği bir dosyası yok ya
da fazlası var: bir ürün klasörünün içinde en fazla 5 dosyalık, her biri en fazla 20 MB
olan bir `dosyalar` (ya da `files`) klasörü olmalı. Programlar ve betikler (`.exe`, `.bat`
gibi) indirilebilir ürün olarak satılamaz; 5'ten fazla dosyayı tek bir ZIP'te toplayın.

**Bir ürün klasörü *Fotoğraf yok* diye görünüyor ve Kontrol adımında duruyor.** Klasörde
yalnızca `dosyalar` klasörü var. Her ilanın en az bir fotoğrafı olmalı: ürünün
fotoğraflarını (01, 02, ...) `dosyalar`'ın yanına, ürün klasörünün kendisine koyun.
`drop run` böyle bir klasörü aynı sebeple atlar, `drop auto` hiçbir şey yüklemeden durur.

Komut satırı hata mesajlarının tablosu yukarıdaki İngilizce
[Error messages](#error-messages) bölümünde.

## İki önemli not

**Hız sınırı uygulama başına.** Personal Access uygulaması **saniyede 5, günde 5.000**
istek alır; genel dokümandaki 10/sn ve 10.000/gün ticari erişim içindir. Uygulamanızın
gerçek sınırı [your-apps](https://www.etsy.com/developers/your-apps) sayfasında yazar;
**Panel** de bugün kalan istek sayısını gösterir.

**Sipariş dosyaları müşterilerinizin kişisel verisini içerir**: ad, e-posta, adres, hediye
notu. `.gitignore` tam da bu yüzden `*.csv` dosyalarını yok sayar; ama başka bir yere
taşıdığınız dosya artık korumasız.
