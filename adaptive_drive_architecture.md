# Adaptif Sürüş Müziği — Sistem Mimarisi

Kapsam: ESP32 olmadan PC'de mock veriyle çalışan bir libpd/Pd prototipi. Bu doküman
**mimari ve karar gerekçeleridir** — kodu sen yazacaksın. Patch objeleri sinyal-akışı
seviyesinde tarif edilir, bağlantı listesi değil. Vanilla Pd 0.54 üzerinde doğrulanmış
obje kısıtları (Bölüm 9) dahil edilmiştir; bunları baştan uygularsan telefon/libpd
taşımasında sürpriz olmaz.

---

## 1. Değişmez kural: veri-kaynağı bağımsızlığı

Tek bir motor patch'i (`engine.pd`) var ve verinin nereden geldiğini **bilmez**.
`[r rpm]` / `[r speed]` / `[r throttle]` / `[r brake]` receive'lerinden sonraki her şey
sabittir; sadece bu isimlere kimin bastığı değişir:

```
PC TEST :  mock (OSC) → [netreceive] → [route] → [s rpm] ...      ┐
Pi PROD :  ESP32 (OSC) → host → libpd_float("rpm", x)            ├─► aynı [r rpm] ► motor
TELEFON :  ESP32 (BLE) → app → libpd_float("rpm", x)             ┘
```

Bu kuralı bozmazsan (kaynağa özel hiçbir mantık `[r rpm]`'in arkasına geçmez) aynı motoru
üç hedefte de değişmeden çalıştırırsın. Mimarinin tek en önemli prensibi budur.

---

## 2. Sistem mimarisi — üç blok

```
┌─ VERI GIRISI (kaynağa özel, motorun dışı) ─────────────────┐
│  mock/ESP → [netreceive -u -b] → [oscparse] → [route ...]  │
│             → [s rpm] [s speed] [s throttle] [s brake]     │
└─────────────────────────────────────────────────────────────┘
            │
┌─ MASTER CLOCK (tek phasor) ─────────────────────────────────┐
│  [phasor~] → bar fazı (sample sync) + bar/beat bang (quantize)│
└─────────────────────────────────────────────────────────────┘
            │
┌─ SES MOTORU ────────────────────────────────────────────────┐
│  stem abstraction'lar (faz-kilitli loop) → [throw~ mix]      │
│  kontrol→ses: vertical (sürekli gain/cutoff) + horizontal     │
│  (quantized state/one-shot)                                  │
│  → FX (filter, tempo delay) → [dac~]                         │
└─────────────────────────────────────────────────────────────┘
```

Sorumluluklar kesin ayrı: clock zamanı üretir, ses motoru sesi üretir, kontrol eşlemesi
ikisini bağlar. Hiçbir blok diğerinin işine karışmaz.

---

## 3. Master clock — beatsync'in kalbi

Tek bir `[phasor~]` her şeyin saatidir. Hem sample senkronu hem quantize tetikleyiciler
**aynı** phasor'dan türer; ayrı saatler kullanırsan kaçınılmaz drift olur.

Sinyal akışı:

```
bpm (float) → [/ 240] → [sig~] → [phasor~]      // bpm/240 = bar/saniye (4 vuruş × 60)
                                     │
                                     ├─► bar fazı: doğrudan sample okumaya (Bölüm 4)
                                     │
                                     └─► bar bang: faz-0 geçişini yakala (aşağıda)
```

120 BPM, 4/4 → 1 bar = 2 sn → phasor 0.5 Hz, bar başına bir tur.

**Bar/beat bang'i nasıl üretilir (vanilla, doğrulanmış):**
`<~` ve `edge~` vanilla'da **yok** (Bölüm 9). Bunun yerine `[threshold~]`:

- Bar bang: `[phasor~] → [threshold~ 0.001 0 0.0005 0]` → **sol outlet** = bar başı bang.
  Phasor wrap'ta 0'a düşünce `lo` altına inip arm olur, hemen ardından `hi` eşiğini
  yukarı geçince sol outlet bang verir (≈ faz 0, ~2 ms içinde).
- Beat bang: `[phasor~] → [*~ 4] → [wrap~] → [threshold~ 0.001 0 0.0005 0]` → sol outlet.

`threshold~` argüman sırası: `hi hidebounce lo lodebounce`. Tek bağımlılıksız obje,
libpd ve telefonda da birebir çalışır.

**Kritik prensip:** araç verisi asla doğrudan tetikleyici değildir. RPM bir _hedef state_
belirler; geçiş bu bar bang'inde uygulanır. Responsive his + grid bütünlüğü buradan gelir.

---

## 4. Ses motoru — faz-kilitli sample okuma

Her stem aynı bar-fazından okunur → kaç tanesi aynı anda açılırsa açılsın hepsi her zaman
aynı barın aynı örneğinde. Drift imkânsız.

Stem başına bir **abstraction** (`stem-voice.pd`), yaratım argümanları: dizi adı + uzunluk.

```
[r~ barPhase]              // master clock'tan signal (send~/receive~)
   │
[*~ <uzunluk>]             // 0..1 → 0..N örnek
   │
[tabread4~ <dizi>]         // 4-nokta interpolasyonlu tablo okuma
   │
[*~] ◄── [r~ gain_<dizi>]  // vertical gain (Bölüm 5), per-stem isim
   │
[throw~ mix]               // ortak mix bus'a topla
```

Tasarım kararları:
- Tüm loop'lar **aynı** BPM ve **aynı** örnek uzunluğunda. 2 bar @120BPM @48kHz = 192000 örnek.
- Loop başı/sonu sıfır geçişinde olsun (tabread4~ wrap yapmaz; aksi halde dikiş tıkırdar).
- Diziler `[table <ad> <boyut>]` ile yaratılır, `[soundfiler]` `read -resize` ile doldurur.
- Stem ekleme = yeni abstraction örneği + yeni dizi. Motorun mantığı değişmez. Paket
  sistemi (Bölüm 11) doğal olarak buradan büyür.

---

## 5. Kontrol → ses: iki ayrı eksen

Bu ayrım müzikal his ile kaos arasındaki sınırdır.

**Vertical (sürekli) — bar bang beklemez:**
RPM'den türeyen `intensity` (0..1) stem gain'lerini ve filtre cutoff'unu sürekli sürer.
Her parametre `[line~]` ile ~50 ms glide → anlık hisseder ama tık yapmaz.

```
[r rpm] → [- 800] → [/ 6200] → [clip 0 1] → [s intensity]
```

Her stem kendi crossfade eğrisini uygular (sert eşik değil, yumuşak rampa → gürültü
titremesini doğal söndürür):

```
[r intensity] → [- <lo>] → [/ <span>] → [clip 0 1] → [pack f 50] → [line~] → [s~ gain_<stem>]
```

Örnek bant aralıkları (intensity üzerinden, üst üste binerek katmanlanır):
pad → her zaman 1.0 · kick → 0.12–0.32 · perc → 0.32–0.50 · bass → 0.45–0.67 ·
lead → 0.68–0.90.

**Horizontal (kesikli) — sadece bar bang'inde:**
state geçişleri (`idle/cruise/accel/decel/brake`) ve one-shot'lar (fill, riser).
Araç verisi _hedef state_ üretir; `[r barTick]` geldiğinde uygulanır. Hysteresis şart
(aç/kapa için iki ayrı eşik) yoksa eşik etrafında state titrer.

```
hedef state mantığı → [s targetState]
[r barTick] → [r targetState] (snapshot) → [sel idle cruise accel decel brake]
                                              → stem aktivasyon + fill/riser one-shot
```

İlk PC sürümünde horizontal'ı minimal tut (sadece vertical + clock + FX çalışsın);
state machine ve one-shot'ları "sinyal işleme sonra" aşamasına bırak.

---

## 6. FX zinciri

Mix bus'a global FX, araç verisiyle modüle. İlk sürümde iki rock-solid vanilla efekt yeter:

```
[catch~ mix]
   │
[vcf~]  ◄── cutoff: [r intensity] → [* 6000] → [+ 400] → [pack f 50] → [line~]   // orta inlet
        ◄── Q: [3( (loadbang ile bir kez)                                          // sağ inlet
   │  (out0 = lowpass)
   ├─────────────────────────────────────────► dry → master toplam
   │
[delwrite~ del 1000] ◄── feedback ile toplanmış giriş
[delread~ del 250]  → feedback [*~ 0.35] (geri) + wet [*~ 0.3] (master'a)          // 1/8 @120
   │
[+~] (dry + wet) → [*~ 0.8] → [dac~]  (mono → iki kanal)
```

- Lowpass cutoff RPM'le açılır: idle'da boğuk, yüksek devirde parlak. En belirgin "nefes alma".
- Tempo-senkron delay 1/8'lik (250 ms). İleride `accel` state'inde send'i açıp cruise'da
  kısarak horizontal eksene bağlanır.
- Reverb istersen `[rev3~]` vanilla'da var ama I/O'su farklı; ilk sürümde atla, sonra ekle.

---

## 7. Veri şeması (OSC kontratı — ESP ile aynı)

Mock bunu yollar, ESP de aynısını yollayacak. Adresler sabit kontrat:

| Adres            | Tip   | Aralık   | Not                          |
|------------------|-------|----------|------------------------------|
| `/car/rpm`       | float | 800–7000 | intensity buradan            |
| `/car/speed`     | float | 0–220    | km/h, ileride reverb/harmoni |
| `/car/throttle`  | float | 0–100    | "niyet" sinyali (RPM'den temiz)|
| `/car/brake`     | float | 0–1      | sonra analog                 |
| `/car/gear`      | int   | 0–6      | opsiyonel                    |

~20 Hz (50 ms) gönderim. Patch tarafı:
`[netreceive -u -b 9000] → [oscparse] → [route /car/rpm /car/speed /car/throttle /car/brake]`
→ ilgili `[s ...]`. (`-b` = ham byte; `oscparse` bunu OSC mesajına çevirir, `route` adrese
göre ayırır.)

---

## 8. Mock (PC test kaynağı)

Ayrı bir süreç (Python `python-osc` ya da Pd'nin kendisi) 127.0.0.1:9000'e OSC basar.
Scripted bir sürüş döngüsü: `idle → accelerate → cruise → hard accel → cruise → brake → idle`,
döngüsel. Üstüne **bilerek** ±40 RPM gürültü + zaman jitter'ı ekle — smoothing ihtiyacını
gerçek koşulda görmek için. İnteraktif test istersen throttle'ı klavyeden elle sür.

Bu mock, gerçek ESP geldiğinde aynı OSC kontratını konuştuğu için patch'e dokunmadan
yerini ESP'ye bırakır.

---

## 9. Vanilla Pd obje kısıtları (Pd 0.54'te doğrulanmış)

Telefon/libpd hedefi için bağımlılıksız (vanilla) kalmak önemli. Doğrulanan tuzaklar:

- `<~`, `>~` (sinyal karşılaştırma) **vanilla'da yok** — ELSE/zexy gibi external ister.
  Kullanma; clock mantığını `[threshold~]` ile kur (Bölüm 3).
- `edge~` **vanilla'da yok**. Bar bang için `[threshold~]` kullan.
- `[threshold~]`, `[wrap~]`, `[rzero~]`, `[phasor~]`, `[tabread4~]`, `[oscparse]`,
  `[netreceive]`, `[vcf~]`, `[delwrite~]`/`[delread~]`, `[soundfiler]` → hepsi vanilla, güvenli.
- **Abstraction argümanı tuzağı:** `.pd` dosyasında yaratım argümanı `$1`/`$2`'yi
  **çıplak yazarsan çalışmaz** ("argument number out of range" hatası). Pd kaydederken
  `\$1` (ters bölü ile) yazar; sen de dosyada `\$1`, `\$2` yazmalısın ki abstraction'a
  argüman olarak geçsin. Elle patch yazıyorsan en sık kaybettiren nokta budur.
- Pd 0.52+ kullan (oscparse/threshold~ mevcut). Telefon libpd'si de güncel olsun.

---

## 10. Kurulum sırası (önce çalışsın, sonra incelt)

1. Placeholder sample'lar (basit kick/perc/bass/pad/lead), tam 192000 örnek, kusursuz loop.
2. Master clock + **tek** stem (pad) → fazda çaldığını duy. (clock doğru mu?)
3. Kalan stem'ler + vertical gain matrisi → mock ile layering'i test et (tık yok, akıcı mı?).
4. FX (vcf~ cutoff + tempo delay) → araç verisiyle modülasyon duyuluyor mu?
5. (Sonra) horizontal state machine + fill/riser one-shot, hysteresis.
6. (Sonra) sinyal işleme: one-euro filtre, eşik ayarı — "sonra düşünürüz" dediğin kısım.
7. (Sonra) ESP32 → aynı OSC kontratı → patch'e dokunmadan gerçek veri.

---

## 11. İleriye dönük — telefon ürünü ve paket modeli

**Telefon (nihai hedef):** aynı `engine.pd`, telefon host'u içinde libpd ile çalışır.
- iOS: libpd + AVAudioEngine/AudioUnit · Android: libpd + **Oboe** (düşük latency şart).
- Tek kod tabanı istiyorsan JUCE (libpd gömülü) ya da Flutter + native libpd modülü.
- Telefon hem host hem köprü: **ESP32 → BLE → telefon** (veri), **telefon → A2DP → araç
  hoparlörü** (ses). İki Bluetooth rolü paralel çalışır.

**A2DP latency — baştan tasarıma göm:** telefondan arabaya Bluetooth ses 150–250 ms
gecikmelidir ve codec'i araç belirler (kontrol edemezsin). Loop'lar kendi içinde fazda
kaldığı için beatsync bozulmaz, ama sürücü hareketi ile tepki arasına gecikme girer.
Geçişleri bar'a quantize ettiğin ve glide kullandığın için bu çoğunlukla affedilir;
keskin "ani fren = anlık darbe" tarzı perküsif efektlerden kaçın, gecikmeyi ele verir.
Testi mutlaka gerçek araç Bluetooth'unda yap, kulaklıkta değil.

**Paket modeli (tekrarlayan gelir):** motor sabit; içerik bir "bundle" olur.

```
package_synthwave/
├── manifest.json   # bpm, stem listesi, gain bantları, fx mapping, state matrisi
├── stems/          # pad, kick, perc, bass, lead, fill, riser (...)
└── cover.png
```

Bunun çalışması için motoru baştan **paket-agnostik** kur: stem sayısı, eşikler, fx
gönderimleri sabit kodlanmasın, açılışta manifest'ten gelsin. O zaman yeni paket =
yeni klasör; app güncellemesi gerekmez, sadece diziler yeniden yüklenir. Senin asıl
değerin burada — motoru herkes yazabilir, inandırıcı paketi sen bestelersin.
