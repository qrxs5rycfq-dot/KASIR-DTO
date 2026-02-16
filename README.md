# 🍽️ KASIR MODERN

Sistem Point of Sale (POS) modern dengan desain Tailwind CSS, fitur lengkap untuk restoran dan cafe.

![Dashboard](https://github.com/user-attachments/assets/da289686-fcd4-452b-975b-a67e848559b3)

## ✨ Fitur

- 🔐 **Login & Register** — Sistem autentikasi lengkap
- 👥 **Role & Permission** — Admin, Manager, Kasir, Customer
- 🛒 **Pesanan Manual (POS)** — Antarmuka kasir modern (responsive tablet & mobile)
- 📱 **Pesanan Online (QR Code)** — Pelanggan pesan dari meja
- 🌶️ **Tingkat Kepedasan** — 5 level pilihan pedas
- 🧊 **Suhu Minuman** — Panas, Dingin, atau Normal
- 💳 **Payment Gateway** — Midtrans, BRI QRIS, Tripay
- 🖨️ **Cetak Struk** — Bluetooth & LAN thermal printer via Android app (3 copy: Pelanggan, Kasir, Koki)
- 📊 **Laporan** — Export PDF & Excel
- 🔔 **Push Notification (FCM)** — Notifikasi pesanan real-time per role
- 🌐 **Integrasi Platform** — GrabFood, GoFood, ShopeeFood (opsional)
- 👤 **Profil & Logout** — Manajemen akun pengguna

## 🚀 Cara Instalasi

### 1. Clone Repository

```bash
git clone https://github.com/qrxs5rycfq-dot/KASIR-DTO.git
cd KASIR-DTO
```

### 2. Buat Virtual Environment (Opsional tapi Direkomendasikan)

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Konfigurasi Environment (.env)

```bash
# Salin file contoh
cp .env.example .env

# Edit file .env sesuai kebutuhan
nano .env  # atau gunakan editor favorit Anda
```

### 5. Jalankan Aplikasi

```bash
python app.py
```

Buka browser dan akses: **http://localhost:8000**

## 🔑 Login Default

| Role  | Username | Password   |
|-------|----------|------------|
| Admin | admin    | admin123   |
| Kasir | kasir    | kasir123   |

## ⚙️ Konfigurasi Environment (.env)

Buat file `.env` berdasarkan `.env.example`. Berikut panduan per bagian:

### Konfigurasi Dasar

```env
# Secret key untuk keamanan (WAJIB diganti untuk production!)
SECRET_KEY=your-super-secret-key-change-this

# Mode debug
FLASK_DEBUG=true

# URL aplikasi (untuk QR Code)
APP_URL=http://localhost:8000
```

### Konfigurasi Database

#### Menggunakan SQLite (Default — Mudah untuk Development)

```env
USE_MYSQL=false
```

SQLite akan otomatis membuat file `kasir.db` di folder aplikasi.

#### Menggunakan MySQL (Untuk Production)

```env
USE_MYSQL=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=password_anda
MYSQL_DATABASE=kasir_db
```

**Langkah setup MySQL:**

1. Buat database di MySQL:
   ```sql
   CREATE DATABASE kasir_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   ```

2. Sesuaikan kredensial di file `.env`

3. Jalankan aplikasi — tabel akan dibuat otomatis

### Konfigurasi Midtrans (Payment Gateway)

1. Daftar di [Midtrans Dashboard](https://dashboard.midtrans.com)

2. Untuk testing, gunakan **Sandbox Mode**:
   - Masuk ke Dashboard → Settings → Access Keys
   - Salin Server Key dan Client Key (yang dimulai dengan `SB-Mid-...`)

3. Tambahkan ke `.env`:
   ```env
   MIDTRANS_SERVER_KEY=SB-Mid-server-XXXXXXXXXXXXXX
   MIDTRANS_CLIENT_KEY=SB-Mid-client-XXXXXXXXXXXXXX
   MIDTRANS_IS_PRODUCTION=false
   ```

4. Untuk **Production**:
   ```env
   MIDTRANS_SERVER_KEY=Mid-server-XXXXXXXXXXXXXX
   MIDTRANS_CLIENT_KEY=Mid-client-XXXXXXXXXXXXXX
   MIDTRANS_IS_PRODUCTION=true
   ```

### Konfigurasi Tripay (Payment Gateway)

1. Daftar di [Tripay](https://tripay.co.id)

2. Buka menu **Merchant** → pilih merchant → **API Keys**

3. Untuk testing, gunakan **Sandbox**:
   ```env
   TRIPAY_API_KEY=DEV-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   TRIPAY_PRIVATE_KEY=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
   TRIPAY_MERCHANT_CODE=T00000
   TRIPAY_IS_PRODUCTION=false
   ```

4. Untuk **Production**:
   ```env
   TRIPAY_API_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   TRIPAY_PRIVATE_KEY=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
   TRIPAY_MERCHANT_CODE=T00000
   TRIPAY_IS_PRODUCTION=true
   ```

5. Set **Callback URL** di dashboard Tripay: `https://domain-anda.com/api/payment/tripay/callback`

### Konfigurasi BRI QRIS (Payment Gateway)

```env
BRI_CLIENT_ID=your_client_id
BRI_CLIENT_SECRET=your_client_secret
BRI_MERCHANT_ID=your_merchant_id
BRI_TERMINAL_ID=your_terminal_id
BRI_PRIVATE_KEY_PATH=/path/to/private_key.pem
BRI_IS_PRODUCTION=false
```

## 🔔 Konfigurasi Push Notification (FCM)

Aplikasi ini mendukung push notification ke perangkat Android menggunakan **Firebase Cloud Messaging (FCM)**. Notifikasi dikirim secara otomatis saat ada pesanan baru, berdasarkan role pengguna:

| Role      | Notifikasi yang Diterima              |
|-----------|---------------------------------------|
| Admin     | Semua pesanan baru                    |
| Manager   | Semua pesanan baru                    |
| Kasir     | Pesanan baru yang perlu diproses      |
| Koki      | Pesanan baru yang perlu dimasak       |

### Langkah Konfigurasi FCM

#### 1. Buat Project Firebase

1. Buka [Firebase Console](https://console.firebase.google.com)
2. Klik **Add project** → beri nama (misal: `kasir-modern`)
3. Ikuti wizard sampai selesai

#### 2. Dapatkan Server Key

1. Di Firebase Console, buka **Project Settings** (ikon ⚙️ di kiri atas)
2. Pilih tab **Cloud Messaging**
3. Aktifkan **Cloud Messaging API (V1)** jika belum aktif
4. Salin **Server key** (string panjang dimulai dengan `AAAA...`)

#### 3. Tambahkan ke `.env`

```env
FCM_SERVER_KEY=AAAAxxxxxxxx:APA91bxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

#### 4. Konfigurasi Android App

1. Di Firebase Console, buka **Project Settings** → tab **General**
2. Klik **Add app** → pilih **Android**
3. Masukkan package name: `com.dapoerterasobor.printservice`
4. Download file `google-services.json`
5. Letakkan di folder `android-print-service/app/`
6. Build ulang aplikasi Android

#### 5. Registrasi Token di Android

Aplikasi Android secara otomatis mengirim FCM token ke server saat login melalui endpoint:

```
POST /api/fcm/register
Body: { "fcm_token": "token_dari_firebase" }
```

Token disimpan di kolom `fcm_token` pada tabel `users` dan digunakan server untuk mengirim push notification.

### Cara Kerja

1. **Pesanan baru dibuat** → Server membuat notifikasi di database
2. Server mencari user dengan role yang sesuai (`target_roles`) dan memiliki `fcm_token`
3. Server mengirim push notification ke semua perangkat yang cocok via FCM HTTP API
4. Pengiriman bersifat **best-effort** — jika gagal, notifikasi tetap tersimpan di database dan bisa dilihat di web

> **Catatan**: Jika menggunakan FCM HTTP v1 API, pastikan menggunakan service account key (JSON) sebagai pengganti server key. Konfigurasi ini dapat disesuaikan di `utils.py`.

## 📁 Struktur File

```
KASIR-DTO/
├── app.py                  # Entry point, app factory, blueprint registration
├── config.py               # Konfigurasi aplikasi dari .env
├── models.py               # Model database (SQLAlchemy)
├── extensions.py           # Inisialisasi Flask extension
├── utils.py                # Helper functions (auth, notification, FCM)
├── socket_handlers.py      # WebSocket event handlers
├── requirements.txt        # Dependencies Python
├── .env.example            # Contoh konfigurasi environment
├── .env                    # Konfigurasi Anda (tidak di-commit)
│
├── routes/                 # Blueprint routes (modular)
│   ├── auth.py             # Login, register, change password, FCM register
│   ├── views.py            # Halaman dashboard, POS, kitchen, reports, dll.
│   ├── admin.py            # Panel admin (users, menu, tables, payment gateway)
│   ├── api_menu.py         # API menu (CRUD)
│   ├── api_cart.py         # API keranjang
│   ├── api_orders.py       # API pesanan, kitchen, shifts, notifications
│   ├── api_payments.py     # API pembayaran (Midtrans, BRI QRIS, Tripay)
│   ├── api_print.py        # API print queue (pending prints, USB printer)
│   └── webhooks.py         # Webhook platform (GrabFood, GoFood, ShopeeFood)
│
├── templates/              # Template HTML (Jinja2 + Tailwind CSS)
│   ├── auth/               # Login & Register
│   ├── admin/              # Panel Admin
│   ├── partials/           # Sidebar, navbar, dll.
│   ├── errors/             # Halaman Error
│   └── ...
├── static/                 # File statis (CSS, JS, images)
│   ├── css/
│   ├── js/
│   └── qrcodes/            # QR Code yang di-generate
├── uploads/                # File upload (foto menu, dll.)
│
└── android-print-service/  # Aplikasi Android (WebView + Print Service)
    └── app/src/main/
        ├── java/.../
        │   ├── MainActivity.java     # WebView POS + Settings panel
        │   ├── PrintService.java     # Background service (WebSocket + printer)
        │   ├── SplashActivity.java   # Splash screen (responsive tablet/phone)
        │   └── BootReceiver.java     # Auto-start setelah reboot
        └── res/
            ├── layout/               # UI layout
            ├── drawable/             # Icons & backgrounds
            └── values/               # Strings, colors, themes, dimensions
```

## 🔐 Keamanan Production

Untuk deployment production, pastikan:

1. **Ganti SECRET_KEY** dengan string acak yang panjang
2. **Nonaktifkan debug mode**: `FLASK_DEBUG=false`
3. **Set FLASK_ENV=production** untuk secure cookies
4. **Gunakan HTTPS** untuk APP_URL
5. **Gunakan MySQL** dengan password yang kuat
6. **Set MIDTRANS_IS_PRODUCTION=true** untuk pembayaran real
7. **Set TRIPAY_IS_PRODUCTION=true** jika menggunakan Tripay

## 📱 Fitur QR Code

Setiap meja memiliki QR Code unik yang bisa di-scan pelanggan:

1. Login sebagai Admin/Manager
2. Buka menu **Meja & QR**
3. Klik tombol **QR** pada meja yang diinginkan
4. Download dan cetak QR Code
5. Pelanggan scan → langsung bisa pesan dari meja

## 📊 Export Laporan

1. Login sebagai Admin/Manager
2. Buka menu **Laporan**
3. Pilih rentang tanggal
4. Klik **Download PDF** atau **Download Excel**

## 🖨️ Cetak Struk via Android

Lihat [ANDROID_INTEGRATION.md](ANDROID_INTEGRATION.md) untuk panduan lengkap setup aplikasi Android termasuk:
- Build APK dari source code
- Konfigurasi printer Bluetooth / LAN
- WebSocket real-time printing
- JavaScript bridge API

## 🛠️ Troubleshooting

### Database Error
- Pastikan file `.env` sudah dikonfigurasi dengan benar
- Untuk MySQL, pastikan service MySQL sudah running
- Cek kredensial database

### Midtrans Error
- Pastikan menggunakan Sandbox keys untuk testing
- Cek apakah keys sudah benar di `.env`

### Tripay Error
- Pastikan `TRIPAY_API_KEY`, `TRIPAY_PRIVATE_KEY`, dan `TRIPAY_MERCHANT_CODE` sudah diisi
- Pastikan Callback URL sudah diset di dashboard Tripay
- Untuk testing, gunakan Sandbox mode (`TRIPAY_IS_PRODUCTION=false`)

### FCM Notification Tidak Terkirim
- Pastikan `FCM_SERVER_KEY` sudah diisi di `.env`
- Pastikan file `google-services.json` ada di `android-print-service/app/`
- Pastikan user sudah login dari Android app (token otomatis terdaftar)
- Cek log server untuk error FCM (pengiriman bersifat best-effort)

### Port 8000 sudah digunakan
- Ganti port di `app.py` bagian bawah
- Atau hentikan proses yang menggunakan port tersebut

## 📄 Lisensi

MIT License — Silakan gunakan dan modifikasi sesuai kebutuhan.

## 🤝 Kontribusi

Pull requests are welcome! Untuk perubahan besar, silakan buka issue terlebih dahulu.
