# 🍽️ KASIR MODERN

Sistem Point of Sale (POS) modern dengan desain Tailwind CSS, fitur lengkap untuk restoran dan cafe.

![Dashboard](https://github.com/user-attachments/assets/da289686-fcd4-452b-975b-a67e848559b3)

## ✨ Fitur

- 🔐 **Login & Register** - Sistem autentikasi lengkap
- 👥 **Role & Permission** - Admin, Manager, Kasir, Customer
- 🛒 **Pesanan Manual (POS)** - Antarmuka kasir modern
- 📱 **Pesanan Online (QR Code)** - Pelanggan pesan dari meja
- 🌶️ **Tingkat Kepedasan** - 5 level pilihan pedas
- 🧊 **Suhu Minuman** - Panas, Dingin, atau Normal
- 💳 **Payment Gateway** - Integrasi Midtrans
- 📊 **Laporan** - Export PDF & Excel
- 👤 **Profil & Logout** - Manajemen akun pengguna

## 🚀 Cara Instalasi

### 1. Clone Repository

```bash
git clone https://github.com/Bucin404/KASIR.git
cd KASIR
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

Buat file `.env` berdasarkan `.env.example`:

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

#### Menggunakan SQLite (Default - Mudah untuk Development)

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

3. Jalankan aplikasi - tabel akan dibuat otomatis

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

## 📁 Struktur File

```
KASIR/
├── app.py              # Aplikasi utama Flask
├── config.py           # Konfigurasi aplikasi
├── models.py           # Model database
├── requirements.txt    # Dependencies Python
├── .env.example        # Contoh konfigurasi environment
├── .env                # Konfigurasi Anda (tidak di-commit)
├── templates/          # Template HTML
│   ├── auth/           # Login & Register
│   ├── admin/          # Panel Admin
│   ├── errors/         # Halaman Error
│   └── ...
├── static/             # File statis (CSS, JS, images)
│   ├── css/
│   ├── js/
│   └── qrcodes/        # QR Code yang di-generate
└── uploads/            # File upload
```

## 🔐 Keamanan Production

Untuk deployment production, pastikan:

1. **Ganti SECRET_KEY** dengan string acak yang panjang
2. **Nonaktifkan debug mode**: `FLASK_DEBUG=false`
3. **Gunakan HTTPS** untuk APP_URL
4. **Gunakan MySQL** dengan password yang kuat
5. **Set MIDTRANS_IS_PRODUCTION=true** untuk pembayaran real

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

## 🛠️ Troubleshooting

### Database Error
- Pastikan file `.env` sudah dikonfigurasi dengan benar
- Untuk MySQL, pastikan service MySQL sudah running
- Cek kredensial database

### Midtrans Error
- Pastikan menggunakan Sandbox keys untuk testing
- Cek apakah keys sudah benar di `.env`

### Port 8000 sudah digunakan
- Ganti port di `app.py` bagian bawah
- Atau hentikan proses yang menggunakan port tersebut

## 📄 Lisensi

MIT License - Silakan gunakan dan modifikasi sesuai kebutuhan.

## 🤝 Kontribusi

Pull requests are welcome! Untuk perubahan besar, silakan buka issue terlebih dahulu.
