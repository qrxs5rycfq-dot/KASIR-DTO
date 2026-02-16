# Panduan Aplikasi Android POS - Dapoer Teras Obor

## Arsitektur

```
┌──────────────────────────────────────────────────────┐
│              Android App                              │
├──────────────────────────────────────────────────────┤
│                                                      │
│  ┌────────────────────────────────────────┐          │
│  │  WebView (Tampilan POS dari VPS)       │          │
│  │  - Full JavaScript & Cookie support    │          │
│  │  - File upload (foto menu, CSV)        │          │
│  │  - Camera capture (foto langsung)      │          │
│  │  - File download (PDF/Excel laporan)   │          │
│  │  - Geolocation                         │          │
│  └────────────────────────────────────────┘          │
│                                                      │
│  ┌────────────────────────────────────────┐          │
│  │  PrintService (Background Service)     │          │
│  │  - WebSocket Client (Socket.IO)        │          │
│  │  - Terima print job real-time          │          │
│  │  - Auto-reconnect                      │          │
│  │  - Foreground notification             │          │
│  │  - Auto-start setelah boot             │          │
│  └──────────────┬─────────────────────────┘          │
│                 │                                     │
│       ┌─────────┴─────────┐                          │
│       ▼                   ▼                          │
│  ┌──────────┐     ┌──────────────┐                   │
│  │Bluetooth │     │ LAN / WiFi   │                   │
│  │Printer   │     │ Printer      │                   │
│  │(SPP)     │     │ (TCP:9100)   │                   │
│  └──────────┘     └──────────────┘                   │
└──────────────────────────────────────────────────────┘
```

## Fitur Lengkap

### WebView POS
- ✅ Tampilan POS dari VPS (responsive)
- ✅ Login, navigasi, semua halaman admin
- ✅ Upload foto menu (kamera + galeri)
- ✅ Download laporan PDF/Excel
- ✅ Geolocation untuk lokasi cabang
- ✅ JavaScript bridge (`AndroidPrint.*`)
- ✅ Cookie & session management
- ✅ Zoom support
- ✅ External link handler (tel:, mailto:, whatsapp:)

### Print Service
- ✅ WebSocket real-time (Socket.IO)
- ✅ Bluetooth thermal printer (ESC/POS via SPP)
- ✅ LAN/WiFi thermal printer (TCP port 9100)
- ✅ Auto-reconnect printer & server
- ✅ Background foreground service (tidak di-kill Android)
- ✅ Auto-start setelah reboot
- ✅ Notifikasi status di notification bar
- ✅ Statistik cetak (berhasil/gagal)

### Permissions
- ✅ INTERNET, NETWORK_STATE, WIFI_STATE
- ✅ BLUETOOTH, BLUETOOTH_CONNECT, BLUETOOTH_SCAN
- ✅ CAMERA
- ✅ READ/WRITE_EXTERNAL_STORAGE, READ_MEDIA_IMAGES
- ✅ ACCESS_FINE_LOCATION
- ✅ FOREGROUND_SERVICE, WAKE_LOCK
- ✅ POST_NOTIFICATIONS
- ✅ RECEIVE_BOOT_COMPLETED
- ✅ VIBRATE

## Cara Build

### Prasyarat
1. **Android Studio** (Hedgehog 2023.1.1 atau lebih baru)
2. **JDK 17** (termasuk di Android Studio)
3. **Android SDK 34** (install via SDK Manager)

### Langkah Build

```bash
# 1. Buka folder android-print-service di Android Studio
#    File → Open → pilih folder android-print-service/

# 2. Tunggu Gradle sync selesai (download dependencies)

# 3. Build APK
#    Build → Build Bundle(s) / APK(s) → Build APK(s)

# 4. APK ada di:
#    app/build/outputs/apk/debug/app-debug.apk
```

### Build via Command Line

```bash
cd android-print-service

# Debug APK
./gradlew assembleDebug

# Release APK (perlu signing config)
./gradlew assembleRelease
```

> **Catatan**: Pertama kali build, Gradle akan mengunduh `gradle-wrapper.jar`
> secara otomatis. Pastikan ada koneksi internet.

## Cara Pakai

### 1. Install APK
- Transfer `app-debug.apk` ke HP Android
- Install (aktifkan "Install from Unknown Sources" jika perlu)

### 2. Konfigurasi Awal
1. Buka aplikasi → tekan tombol **⚙️** (kiri atas)
2. Isi **URL Server**: `http://[IP-VPS]:8000`
3. Isi **Username** & **Password** (akun kasir/admin)
4. Pilih jenis printer:
   - **Bluetooth**: Pilih printer dari daftar paired devices
   - **LAN/WiFi**: Masukkan IP:Port (contoh: `192.168.1.50:9100`)
5. Tekan **💾 Simpan Pengaturan**

### 3. Mulai Print Service
1. Tekan tombol **▶ Start** (kanan atas)
2. Status berubah ke **🟢 Print Service: AKTIF**
3. Service akan otomatis:
   - Login ke server
   - Connect ke printer
   - Connect WebSocket
   - Menunggu pesanan baru

### 4. Cara Kerja
- Saat ada pesanan baru di POS → server kirim via WebSocket → Android cetak otomatis
- Jika printer mati → auto-reconnect setiap 5 detik
- Jika server mati → auto-reconnect setiap 2-10 detik
- Jika HP restart → Print Service otomatis mulai lagi

## Struktur File

```
android-print-service/
├── build.gradle                    # Project-level Gradle config
├── settings.gradle                 # Gradle settings
├── gradle.properties               # Gradle properties
├── gradlew                         # Linux/macOS build script
├── gradlew.bat                     # Windows build script
├── .gitignore                      # Git ignore rules
│
├── gradle/wrapper/
│   └── gradle-wrapper.properties   # Gradle version config
│
└── app/
    ├── build.gradle                # App-level dependencies & config
    ├── proguard-rules.pro          # ProGuard obfuscation rules
    │
    └── src/main/
        ├── AndroidManifest.xml     # App manifest (permissions, services)
        │
        ├── java/com/dapoerterasobor/printservice/
        │   ├── MainActivity.java   # WebView POS + Settings UI
        │   ├── PrintService.java   # Background print service (WebSocket + Bluetooth/LAN)
        │   └── BootReceiver.java   # Auto-start service after boot
        │
        └── res/
            ├── layout/
            │   └── activity_main.xml       # Main UI layout
            ├── drawable/
            │   └── ic_launcher.xml         # App icon
            ├── values/
            │   ├── strings.xml             # String resources
            │   ├── themes.xml              # App theme (dark)
            │   └── colors.xml              # Color definitions
            └── xml/
                ├── network_security_config.xml  # HTTP cleartext config
                └── file_paths.xml               # FileProvider paths (camera)
```

## JavaScript Bridge

Dari halaman POS di WebView, bisa akses fungsi native:

```javascript
// Cek apakah Print Service aktif
if (window.AndroidPrint) {
    const isRunning = AndroidPrint.isServiceRunning();
    const printerName = AndroidPrint.getPrinterName();
    const appVersion = AndroidPrint.getAppVersion();

    // Buka settings panel
    AndroidPrint.openSettings();
}
```

## Server Requirements

Server Flask harus menjalankan Flask-SocketIO:

```python
# requirements.txt harus include:
Flask-SocketIO
gevent
gevent-websocket

# app.py sudah dikonfigurasi dengan:
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
```

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| WebView kosong | Pastikan URL server benar dan bisa diakses dari HP |
| Upload foto gagal | Berikan izin kamera dan storage |
| Printer tidak terdeteksi | Pair printer di Bluetooth settings HP dulu |
| Print Service mati sendiri | Matikan battery optimization untuk app ini |
| WebSocket disconnect terus | Pastikan port 8000 tidak di-block firewall |
| LAN printer gagal | Pastikan HP dan printer dalam satu jaringan WiFi |
