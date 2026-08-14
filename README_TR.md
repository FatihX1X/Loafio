# Loafio

Türkçe | [English](README.md)

Loafio, League of Loaf yarışması için geliştirilmiş, envanter sınırlı ve
otonom bir piyasa yapıcı bottur. Windows üzerinde yerel olarak çalışır;
aktif `terafab` emir defterinde maker öncelikli yürütme, envanter eğimi,
podium temposuna göre boyutlandırma, eski durum temizliği ve watchdog restart uygular.

> [!CAUTION]
> `run.cmd`, kullanıcı onayı istemeden canlı yarışma işlemlerine başlar.
> Kâr, podium derecesi, gerçekleşme kalitesi veya azami gerçekleşen zarar
> garantisi yoktur. Equity zarar kesicisi bulunmaz: siz durdurana kadar çalışır
> ve yarışma bakiyesinin tamamını kaybedebilir.

## Strateji özeti

- Normal emirleri mümkün olduğunca pasif tutarak taker maliyetinden kaçınır.
- Normalde taraf başına 15.000 USDL, catch-up modunda 25.000 USDL ve podium
  temposunun çok gerisindeki sprint modunda 40.000 USDL'ye kadar kullanır.
- 30.000 USDL Terafab envanteri hedefler; sert tavan 80.000 USDL'dir.
- Alış ve satış boyutlarını mevcut envantere göre eğer.
- Kuyruk önceliğini korur; yalnız fiyat, dolum veya risk koşulları
  gerektirdiğinde emir yeniler.
- Spread'i kasıtlı olarak geçmeden kısa ve orta vadeli leaderboard temposunu izler.
- Self-trade'i engeller ve tanınmayan aktif emirlerle çalışmayı reddeder.
- Loaf bir emrin zaten terminal olduğunu bildirirse eski yerel kaydı temizler.
- Aynı self-trade engeli on saniyede altı kez tekrarlanırsa watchdog restart ister.
- Borsa genelindeki trading halt durumunu geçici kabul eder; watchdog'u durdurmak
  yerine emir işlemlerini bekletip otomatik yeniden dener.
- Başlangıçta tanınmayan aktif emirleri iptal edip uzlaştırmayı otomatik yeniden
  başlatır; manuel temizleme gerektirmez.
- Taker emrini yalnız acil veya manuel pozisyon kapatmada kullanır.
- Oturum, emir, fill, nonce, ücret, equity, leaderboard ve watchdog yeniden
  başlatma verilerini SQLite'ta saklar.

Loaf, Terafab riskini hedge edecek ayrı bir enstrüman sunmadığından bu yapı
gerçek anlamda delta-neutral değil, inventory-neutral'dır.

## Çalışma ve risk modeli

Her manuel `run.cmd` çalıştırması yeni bir oturum oluşturur ve raporlama için
başlangıç equity değerini kaydeder. Equity izlenip kaydedilir fakat oturumu
durdurmaz, kilitlemez veya pozisyon kapatmaz. Bot `Ctrl+C`, kalıcı bir
yapılandırma/preflight hatası ya da manuel `flatten` komutuna kadar çalışır.

Python süreci çöker veya tekrarlayan eski-emir/self-trade döngüsü algılarsa
PowerShell watchdog beş saniye sonra aynı session ID ile yeniden başlatır.
Başlangıç uzlaştırması yerel eski emir durumunu borsanın aktif emir görünümüyle
yeniler. 80.000 USDL envanter tavanı ve piyasa-verisi güvenlik duraklamaları
korunur; ancak bunların hiçbiri hesap düzeyinde zarar limiti değildir.

Loaf borsa genelinde trading halt etkinleştirdiğinde sunucu yeni emir ve iptalleri
reddeder. Loafio açık kalır, başlangıç kontrolleri arasında 15 saniye bekler ve
Loaf işlemleri yeniden açtığında otomatik devam eder.

## Gereksinimler

- Windows 10 veya Windows 11
- PowerShell 5.1+
- Python 3.11+
- Aktif yarışma turuna kabul edilmiş bir Loaf hesabı
- Loaf API anahtarı ve sayısal Loaf kullanıcı ID'si

Tekrarlanabilir kurulum için resmî Loaf SDK bağımlılığı
`e1157bcc2cba41fcde6e0f929cc58ad61bc4d442` commit'ine sabitlenmiştir.

## Kurulum

Repoyu klonlayıp kurulum komutunu çalıştırın:

```powershell
git clone https://github.com/FatihX1X/Loafio.git
cd Loafio
.\setup.cmd
```

`setup.cmd`, `.venv` sanal ortamını oluşturur, sabitlenmiş çalışma ve geliştirme
bağımlılıklarını kurar ve gerekirse `.env.example` dosyasından yerel `.env`
dosyasını üretir.

## Yapılandırma

Repo kökünde `.env` dosyasını oluşturun veya düzenleyin:

```dotenv
LOAF_API_KEY=
LOAF_USER_ID=
LOAF_API_BASE_URL=https://api.loafmarkets.com/api
LOAF_TARGET_TOKEN=terafab
LOAF_DB_PATH=.state/loaf_bot.sqlite3
LOAF_LOG_DIR=logs
```

API anahtarlarını [Loaf API ayarlarından](https://beta.loafmarkets.com/api)
oluşturun ve yenileyin. Anahtarı repoya commit etmeyin, komuta yapıştırmayın
ve loglara yazmayın. `.env`, veritabanı ve loglar `.gitignore` ile hariç tutulur.

Resmî dokümantasyon, `LOAF_USER_ID` değerini özel
`portfolio:{userId}` WebSocket kanalında kullanılan sayısal kullanıcı ID'si olarak
tanımlar. Web arayüzünde görünmüyorsa API anahtarını `.env` dosyasına yazıp
şu salt-okunur komutu çalıştırın:

```powershell
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv(); from loaf import LoafClient; c=LoafClient(); print(c.get('/auth/profile')['userId']); c.close()"
```

Yalnız ekrana basılan sayıyı `LOAF_USER_ID` alanına kopyalayın.

## Canlı işlemi başlatma

Başlatmadan önce elle oluşturulmuş açık emirleri iptal edin. Bot, tanımadığı
emirlerin kontrolünü devralmayı bilerek reddeder.

```powershell
.\run.cmd
```

Bot; hesap, yarışma, varlık, ücret, emir ve özel WebSocket kontrollerini
yapar. Bütün kontroller geçerse canlı fiyat vermeye hemen başlar. Bilgisayarı
açık ve internete bağlı tutun.

Normal kapatma için `Ctrl+C` kullanın. Bot açık emirleri iptal eder, 30 saniyeye
kadar pasif satış dener ve kalan Terafab miktarını market sell ile kapatır.

## Operasyon komutları

```powershell
# Son yerel oturum, equity, hacim ve envanter durumunu gösterir
.\.venv\Scripts\python.exe -m loaf_bot status

# Bütün emirleri iptal eder ve Terafab pozisyonunu market sell ile kapatır
.\.venv\Scripts\python.exe -m loaf_bot flatten

```

`flatten` yıkıcı bir komuttur: açık emirleri iptal ettikten sonra mevcut Terafab
pozisyonunu market emriyle tasfiye eder.

Çalışma verileri şu konumlara yazılır:

- `.state/loaf_bot.sqlite3`
- `logs/loaf-maker.log`
- `logs/watchdog.log`

## Doğrulama

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

Offline testler; yuvarlama, envanter eğimi, podium temposu, kısmi fill, nonce
tekilliği, HTTP belirsizliği, self-trade engeli, acil kapatma, oturum kalıcılığı,
otomatik döngü restartı, eski veri ve WebSocket uzlaştırmasını kapsar.

## Güvenlik

- `.env`, `.state` ve `logs` dosyalarını asla commit etmeyin.
- Bir anahtar sohbet, terminal görüntüsü, log veya Git geçmişinde açığa
  çıkarsa hemen iptal edin.
- Mümkün olan en az yetkiyle ayrı bir yarışma hesabı kullanın.
- Çalıştırmadan önce yarışma kurallarını ve yerel yasal gereklilikleri inceleyin.

Kaynaklar:

- [Trading bot oluşturma](https://docs.loafmarkets.com/en/guides/building-a-trading-bot/)
- [WebSocket API](https://docs.loafmarkets.com/en/api-reference/websocket/)
- [Orders API](https://docs.loafmarkets.com/en/api-reference/orders/)
- [Trading competition](https://docs.loafmarkets.com/en/trading-competition/)

## Sorumluluk reddi

Bu proje eğitim ve yarışma kullanımı için sunulur; finansal tavsiye değildir.
Kimlik bilgileri, hesap uygunluğu, strateji davranışı, zararlar ve Loaf
kurallarına uyum kullanıcının sorumluluğundadır.
