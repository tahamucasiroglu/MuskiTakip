# MUSKİ Kesinti Takip

MUSKİ (Muğla Su ve Kanalizasyon İdaresi) su kesintilerini kayıt altına alır. MUSKİ geçmiş kesintileri
yayınlamadığı için bu proje [muski.gov.tr/su-kesintileri](https://www.muski.gov.tr/su-kesintileri) sayfasını
**15 dakikada bir** Selenium ile ziyaret eder ve her kesintiyi kalıcı olarak saklar. Telefona gelen MUSKİ SMS'leri de
ayrı bir kayıt türü olarak eklenebilir.

## Kayıt türleri

| Tür | Kaynak | Ne saklanır |
|---|---|---|
| `web` | Site, otomatik | İlçe, kesinti tarihi, tahmini süre, durum, mahalleler + sayfada görünmeyen alanlar: MUSKİ kesinti ID, iş emri no, iş emri durumu, ilçe/mahalle ID ve UAVT kodları, ham HTML kartı ve ham JSON |
| `mesaj` | SMS | Arıza yeri (sokak), sebep, bildirilen bitiş saati (uzatmalar dahil), etkilenen mahalleler, mesaj metinleri |

İki tür birbirinden bağımsızdır; aynı kesinti her iki türde de bulunabilir. Her tür kendi içinde tekilleştirilir.

### Web verisi nereden geliyor?
- `/su-kesintileri` sayfası: tüm kesintiler (devam eden + tamamlanan), ama ID yok.
- Ana sayfanın kullandığı `/api/home/su-kesintileri` JSON servisi: en yeni 5 kesinti, tüm ID'lerle.
  İkisi ilçe + kesinti dakikası + mahalle örtüşmesiyle eşleştirilir. Yeni kesintiler listenin başına geldiği için ID'ler pratikte hep yakalanır.
- Gerçek bitiş saati yayınlanmıyor. Kesintinin "devam ediyor" olarak son görüldüğü an ile "tamamlandı" olarak ilk
  görüldüğü (ya da siteden kalktığı) an arasında bittiği kesindir; bu aralık "Bitiş (en erken – en geç)" olarak gösterilir.
- Her ziyaretin ham yanıtı, içerik değiştiyse SHA-256 özetiyle `raw_snapshots` tablosunda saklanır (kanıt için).

## Arayüz
`http://<pi-ip>:5090` — liste, filtreler (ilçe, mahalle, durum, tür, tarih aralığı, kayıt türü, arama), kesinti detayı ve
değişiklik geçmişi, istatistik, site ziyaret kayıtları, veritabanı indirme/yedekler. Filtrelenmiş liste Excel, PDF, Word,
CSV ve JSON olarak indirilebilir.

## Raspberry Pi kurulumu
```bash
bash deploy/deploy.sh kullanici@pi-adresi   # son commit'i gönderir ve deploy/install.sh'ı çalıştırır
```
- Kod: `/opt/muskitakip/app`, venv: `/opt/muskitakip/venv`, projeye özel Chromium+ChromeDriver: `/opt/muskitakip/chromium`
  (sistemdeki Chromium'a ve diğer uygulamalara dokunulmaz)
- Veri: `/var/lib/muskitakip/muski.db` (SQLite), günlük yedekler `/var/lib/muskitakip/backups` (son 90)
- Servisler (`muskitakip` kullanıcısıyla, açılışta otomatik başlar):
  - `muskitakip-scraper.timer` → her 15 dakikada `muskitakip-scraper.service`
  - `muskitakip-web.service` → arayüz (port 5090)
  - `muskitakip-backup.timer` → her gün 03:30 yedek
- Loglar: `journalctl -u muskitakip-scraper -n 50`, `journalctl -u muskitakip-web -f`

## SMS içe aktarma
```bash
sudo -u muskitakip env $(cat /etc/muskitakip.env | xargs) /opt/muskitakip/venv/bin/python -m muskitakip.sms mesajlar.txt
```
Dosyadaki mesajlar telefondaki gibi en yeni üstte kabul edilir (`--eski-once` ile tersi, `--deneme` ile yazmadan dener).
Fatura, başvuru vb. mesajlar atlanır. Aynı metin tekrar gelirse sayaç artar, "devam eden" ve "tamamlanmış olup su
veriliyor" mesajları aynı arıza yerindeki önceki kayda bağlanır. Komut tekrar çalıştırılırsa mükerrer kayıt oluşmaz.

## Geliştirme
```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m pytest -q
MUSKI_DATA_DIR=./data .venv/Scripts/python -m muskitakip.scraper   # yerelde Chrome ile tek tarama
.venv/Scripts/python -m muskitakip.web
```
