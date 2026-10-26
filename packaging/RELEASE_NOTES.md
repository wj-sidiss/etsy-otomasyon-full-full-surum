**0.3.3 makes your mockups look real.** The design now takes the mockup's own light, folds
and fabric texture (**Gerçekçilik**), can be placed on four corners for angled frames and
walls (**4 köşe**), and wraps around mugs (**Kavis**). Set them per mockup in **Mockuplar**;
existing mockups keep working, and with the sliders at 0 the result is exactly what 0.3.2 made.

**Better titles and tags.** New drafts now lead with the design's own phrase, keep shop-wide
tags to three, vary tags from draft to draft, and no longer write "removable" or "self
adhesive" when you also sell paste-up paper. **SEO** also flags listings that share most of
their tags with another listing.

**0.3.3 mockup'larınızı gerçek gösterir.** Tasarım artık mockup'ın ışığını, kıvrımlarını ve
kumaş dokusunu alıyor (**Gerçekçilik**), eğik duran çerçeve ve duvarlar için dört köşeden
yerleştirilebiliyor (**4 köşe**) ve kupanın etrafına sarılıyor (**Kavis**). **Mockuplar**'da
her mockup için ayarlanır; eski mockup'lar çalışmaya devam eder, ayarlar 0'dayken sonuç
0.3.2'dekiyle birebir aynıdır.

**Daha iyi başlık ve etiketler.** Yeni taslaklar tasarımın kendi ifadesiyle başlıyor, bütün
mağazada tekrar eden etiketleri üçle sınırlıyor, etiketleri taslaktan taslağa değiştiriyor ve
yapışkansız kağıt da satıyorsan artık "removable" ya da "self adhesive" yazmıyor. **SEO** sayfası
etiketlerinin çoğunu başka bir ilanla paylaşan ilanları da işaretliyor.

**0.3.2 looks and works like the video, and adds what sellers asked for.**

- **Watermark:** upload your own mark on **Mockuplar**; choose position (centre, corner or
  repeated), opacity and size. It goes on every listing photo (or only on digital products),
  never on the file the buyer downloads.
- **Info images:** tick photos of your template listing (size chart, materials…) on
  **Şablon İlan**; they are added to the end of every draft.
- **Description template:** sentences about the template's own design are no longer copied
  onto new listings; `{başlık}` and `{tasarım}` fill in each draft's title and design.
- **Shop section** to choose when you start an upload, a download for **Intel Macs**, the
  video's fonts, and a new-version check at every start.

**0.3.2 videodaki gibi görünüp çalışıyor ve satıcıların istediklerini ekliyor.**

- **Filigran:** **Mockuplar**'dan kendi filigranınızı yükleyin; konum (orta, köşe ya da
  tekrarlı), saydamlık ve boyut seçin. Tüm ilan fotoğraflarına (ya da yalnızca dijital
  ürünlere) eklenir, alıcının indirdiği dosyaya asla eklenmez.
- **Bilgi görselleri:** **Şablon İlan**'da şablon ilanınızın fotoğraflarından (boyut
  tablosu, malzemeler…) seçtikleriniz her taslağın sonuna eklenir.
- **Açıklama şablonu:** şablon ilanın desenine özel cümleler artık yeni ilanlara
  kopyalanmaz; `{başlık}` ve `{tasarım}` her taslağın başlığı ve tasarımıyla dolar.
- Yükleme başlarken **mağaza bölümü** seçimi, **Intel Mac** için ayrı indirme, videodaki
  yazı tipleri ve her açılışta yeni sürüm kontrolü.

0.3.0 veya 0.3.1 kullanıyorsanız uygulama bu sürümü üst çubukta **Yeni sürüm · İndir**
olarak gösterir.

---

The 0.3.0 notes follow.

---

**etsyotomasyon now opens in your browser.** Double-click it and it opens at
`http://localhost:3000` with the screens from the video: Panel, Tasarım Yükle, İlanlar,
SEO, Siparişler, Kâr-Zarar, Pinterest, Mockuplar, Şablon İlan, Mağaza Bağlantısı and
Ayarlar. Connecting a shop now shows the callback address with a copy button and explains
Etsy's error messages. The print-area editor and the choice of mockups for drafts are
easy to find.

**Digital products work.** If your template listing is a digital download, every draft is
made as one and gets the buyer's files: a loose design is delivered as the design file
itself, and a product folder delivers the files in its `dosyalar` (or `files`) subfolder
(at most 5 files per listing, 20 MB each). A download-only template needs no shipping
profile. **Tasarım Yükle** shows **Ürün: Dijital** (*Product: Digital*), the **Başlat**
(*Start*) window opens with **Dijital ürün · tasarım dosyası indirilebilir dosya olarak
eklenir** (*Digital product · the design file is attached as the download*), and
**Şablon İlan** says **Dijital ürün: her taslağa tasarım dosyası indirilebilir dosya olarak
eklenir** under the chosen listing.

**Choosing `2-PRODUCTS` as the products folder** (or `1-MOCKUPS`, `3-DRAFTS`, or a folder
inside them) no longer makes a second products folder inside the first: the main one is
used, and **Ayarlar** says so. If 0.2.0 already nested one, **Ayarlar** shows it with a
**Use the main folder** button.

**Command line:** `drop run` and `drop auto` use the mockups chosen on **Mockuplar**, in
their order (`--mockups N` = the first N of them), and their `--path` may point inside the
products folder. `listings pull` no longer writes the empty `views` column, and
`seo suggest` shows plain text.

**New versions announce themselves.** From this version on, etsyotomasyon checks GitHub once a
day for a newer release and shows **Yeni sürüm · İndir** (*New version · Download*) in the
top bar, with **Neler yeni?** (*What's new?*). Nothing about you or your shop is sent; you
can turn it off under **Ayarlar**. The file you sell is never used as a listing photo:
digital drafts show your mockups and a small preview.

## Download

| Your computer | File |
|---|---|
| **Windows 10 / 11** | `etsyotomasyon-…-windows.exe`: one file, nothing to install. Double-click it. |
| **Mac (Apple Silicon: M1 and newer)** | `etsyotomasyon-…-macos.zip`: unzip it, then move `etsyotomasyon.app` to Applications. |
| **Mac with Intel** | `etsyotomasyon-…-macos-intel.zip`: the same, for Intel Macs. |

You do not need Python. Your keys and your shop connection stay on your computer, in
`~/.etsyotomasyon`, the same place the command line version uses. Settings and the Etsy
connection from 0.2.0 carry over.

**The first time you open it:**

- **Windows** may say *"Windows protected your PC"*, because the app is not code-signed.
  Click **More info → Run anyway**.
- **macOS** may refuse to open it because Apple could not verify the developer. Open
  **System Settings → Privacy & Security**, scroll down, and click **Open Anyway**.

Your browser then opens on **Mağaza Bağlantısı** (Shop connection). Follow it from top to
bottom. etsyotomasyon has no window of its own: to quit, use **Ayarlar → Kapat**, or close the
tab and it stops by itself about 90 seconds later.

Running from source on Windows, macOS or Linux (Python 3.9–3.13): `pip install -e .`,
then `etsyotomasyon desktop`. See the
[README](https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum#run-from-source-windows-macos-linux).

## Troubleshooting

- **"The requested redirect URL is not permitted"**: add exactly
  `http://localhost:3003/oauth/redirect` under your app's **⋮ → Edit callback URLs** on
  <https://www.etsy.com/developers/your-apps> (http, localhost, no slash at the end), then
  press **Bağlan** (Connect) again. No such menu yet means the app is still waiting for
  approval.
- **Keys refused**: copy both the Keystring and the Shared secret from the same, approved
  app.
- **"This site can't be reached" on localhost:3003 after approving**: keep etsyotomasyon open
  while you approve (it waits 5 minutes), then press **Bağlan** again. If another program
  uses port 3003, close it.
- **App name refused**: Etsy does not accept names containing "Etsy". Do not switch on
  Developer Mode.
- **Tracking upload refused (403)**: Etsy restricts adding tracking with newer API keys in
  many countries. Enter tracking in Shop Manager.
- **"This page only opens from the etsyotomasyon app"** or **"Cannot reach etsyotomasyon"**:
  double-click etsyotomasyon again. It opens a new tab, in the running app or in a fresh one.
- **A `2-PRODUCTS` inside `2-PRODUCTS`, mockups or designs not found**: press **Use the
  main folder** in **Ayarlar → Klasörler**, then move the mockups and the designs not
  uploaded yet into the main folder's `1-MOCKUPS` and `2-PRODUCTS`.
- **A digital product stops before its draft**: put the buyer's files in a `dosyalar` (or
  `files`) folder inside the product folder, at most 5 files of 20 MB each.

---

**etsyotomasyon artık tarayıcıda açılıyor.** Çift tıklayın, `http://localhost:3000` adresinde
videodaki ekranlarla açılır: Panel, Tasarım Yükle, İlanlar, SEO, Siparişler, Kâr-Zarar,
Pinterest, Mockuplar, Şablon İlan, Mağaza Bağlantısı ve Ayarlar. Mağaza bağlarken
geri dönüş adresi kopyalama düğmesiyle gösteriliyor ve Etsy'nin hata mesajları
açıklanıyor. Baskı alanı ayarı ve taslaklara girecek mockup seçimi artık kolayca
bulunuyor.

**Dijital ürünler destekleniyor.** Şablon ilanınız dijital bir ürünse her taslak dijital
açılır ve alıcının dosyaları eklenir: tek başına bir tasarımda tasarım dosyasının kendisi,
bir ürün klasöründe ise içindeki `dosyalar` (ya da `files`) alt klasöründeki dosyalar
(ilan başına en fazla 5 dosya, her biri en fazla 20 MB). Yalnızca dijital bir şablon için
kargo profili gerekmez. **Tasarım Yükle** üstte **Ürün: Dijital** gösteriyor, **Başlat**'a
basınca açılan pencere **Dijital ürün · tasarım dosyası indirilebilir dosya olarak
eklenir** satırıyla başlıyor, **Şablon İlan** da seçili ilanın altında **Dijital ürün: her
taslağa tasarım dosyası indirilebilir dosya olarak eklenir** yazıyor.

**Ürün klasörü olarak `2-PRODUCTS`'ı** (ya da `1-MOCKUPS`, `3-DRAFTS` veya içlerindeki bir
klasörü) seçmek artık ilkinin içine ikinci bir ürün klasörü açmıyor: ana klasör kullanılıyor
ve **Ayarlar** bunu söylüyor. 0.2.0 zaten iç içe bir klasör açtıysa **Ayarlar** onu
**Ana klasörü kullan** düğmesiyle gösteriyor.

**Komut satırı:** `drop run` ve `drop auto`, **Mockuplar**'da seçilen mockup'ları o
sırayla kullanıyor (`--mockups N` = bunların ilk N tanesi); `--path` ürün klasörünün
içini gösterebiliyor. `listings pull` artık boş `views` sütununu yazmıyor, `seo suggest`
düz metin gösteriyor.

**Yeni sürümler artık kendini duyuruyor.** Bu sürümden itibaren etsyotomasyon günde bir kez
GitHub'da yeni sürüm olup olmadığına bakar; varsa üst çubukta **Yeni sürüm · İndir** ve
**Neler yeni?** görünür. Sizinle ya da mağazanızla ilgili hiçbir bilgi gönderilmez;
**Ayarlar**'dan kapatabilirsiniz. Sattığınız dosya hiçbir zaman ilan fotoğrafı olarak
yüklenmez: dijital taslaklarda mockup'larınız ve küçük bir önizleme görünür.

## İndir (Türkçe)

| Bilgisayarınız | Dosya |
|---|---|
| **Windows 10 / 11** | `etsyotomasyon-…-windows.exe`: tek dosya, kurulum yok. Çift tıklayın. |
| **Mac (Apple Silicon: M1 ve sonrası)** | `etsyotomasyon-…-macos.zip`: zip'i açın, `etsyotomasyon.app`'i Uygulamalar klasörüne taşıyın. |
| **Intel işlemcili Mac** | `etsyotomasyon-…-macos-intel.zip`: aynısı, Intel Mac'ler için. |

Python gerekmez. Anahtarlarınız ve mağaza bağlantınız yalnızca sizin bilgisayarınızda,
`~/.etsyotomasyon` klasöründe durur. 0.2.0'daki ayarlarınız ve Etsy bağlantınız aynen devam
eder.

**İlk açılışta:**

- **Windows** *"Windows kişisel bilgisayarınızı korudu"* diyebilir, çünkü uygulama imzalı
  değil. **Ek bilgi → Yine de çalıştır**'a tıklayın.
- **macOS**, geliştiriciyi doğrulayamadığı için uygulamayı açmayabilir. **Sistem Ayarları →
  Gizlilik ve Güvenlik**'e girin, aşağı kaydırın ve **Yine de Aç**'a tıklayın.

Sonra tarayıcınız **Mağaza Bağlantısı** sayfasında açılır; yukarıdan aşağı takip edin.
etsyotomasyon'in kendi penceresi yoktur. Kapatmak için **Ayarlar → Kapat**'ı kullanın ya da
sekmeyi kapatın, yaklaşık 90 saniye sonra kendiliğinden kapanır.

Kaynak koddan çalıştırmak için (Windows, macOS, Linux; Python 3.9–3.13): `pip install -e .`,
sonra `etsyotomasyon desktop`. Ayrıntılar
[README'de](https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum#kaynak-koddan-çalıştırma-windows-macos-linux).

## Sık karşılaşılan hatalar

**Etsy "İstenen yönlendirme URL'sine izin verilmiyor" diyor** (*The requested redirect URL is not permitted*)
Geri dönüş adresi Etsy uygulamanızda kayıtlı değil. <https://www.etsy.com/developers/your-apps> → uygulamanızın yanındaki **⋮** → **Edit callback URLs** → şu adresi **birebir** ekleyip kaydedin: `http://localhost:3003/oauth/redirect`
`https` değil `http`, `127.0.0.1` değil `localhost`, sonunda `/` yok. En kolayı: **Mağaza Bağlantısı**'ndaki adresin yanındaki kopyalama düğmesi. Sonra **Bağlan**'a tekrar basın.

**"Edit callback URLs" menüsü yok**: Uygulamanız henüz onaylanmamış. Onay genelde birkaç dakika sürer; onaylanınca menü çıkar.

**"Etsy bu anahtarları kabul etmedi"**: Keystring ve Shared secret aynı uygulamadan, boşluksuz kopyalanmalı. İkisi birden gerekir. Uygulama onaylanmadan anahtarlar çalışmaz.

**Etsy'de izin verdim ama tarayıcı "Bu siteye ulaşılamıyor" (localhost:3003) diyor**: İzin verirken etsyotomasyon açık olmalı; uygulama 5 dakika bekler. etsyotomasyon'e dönüp **Bağlan**'a tekrar basın. 3003 portunu başka bir program kullanıyorsa o programı kapatın.

**Uygulama adı reddedildi**: Etsy, adında "Etsy" geçen uygulamaları kabul etmiyor. Başka bir ad seçin. Developer Mode'u açmayın.

**Takip numarası yüklenmiyor (403)**: Etsy, 2024'ten beri yeni API anahtarlarıyla takip numarası eklemeyi Türkiye dahil birçok ülkede kısıtlıyor. Takip numaralarını Etsy Mağaza Yöneticisi'nden girin.

**"Bu sayfa yalnızca etsyotomasyon uygulamasından açılır"** ya da **"etsyotomasyon'e ulaşılamıyor"**: etsyotomasyon'e yeniden çift tıklayın. Yeni bir sekme açar; uygulama çalışıyorsa onda, kapanmışsa yeniden başlatarak.

**`2-PRODUCTS`'ın içinde bir `2-PRODUCTS` daha var, mockup'lar ya da tasarımlar bulunmuyor**: **Ayarlar → Klasörler**'de **Ana klasörü kullan**'a basın, sonra mockup'ları ve henüz yüklenmemiş tasarımları ana klasördeki `1-MOCKUPS` ve `2-PRODUCTS`'a taşıyın.

**Dijital bir ürün taslağa geçmeden duruyor**: Alıcının dosyalarını ürün klasörünün içindeki bir `dosyalar` (ya da `files`) klasörüne koyun; en fazla 5 dosya, her biri en fazla 20 MB.

---

What changed: see [CHANGELOG.md](https://github.com/wj-sidiss/etsy-otomasyon-full-full-surum/blob/main/CHANGELOG.md).
