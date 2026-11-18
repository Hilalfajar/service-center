# Service Center

GUI GTK4/libadwaita untuk mendaftarkan file program sebagai **layanan systemd user**,
lalu memantau dan mengontrolnya dari satu tempat.

## Fitur

- Daftar layanan yang sudah terdaftar, lengkap dengan status berjalan/gagal/berhenti.
- Pemilih file dengan deteksi otomatis perintah:
  - `.py`: memakai `venv/.venv` di sekitarnya bila ada, jika tidak `python3`.
  - `.js/.mjs/.cjs`: dijalankan dengan `node`.
  - `package.json`: `npm start` bila ada `scripts.start`, jika tidak `node` pada `main`/`index.js`.
  - `.sh/.bash`, `.jar`, atau binary yang executable.
- Pilihan direktori kerja, argumen tambahan, variabel environment, dan kebijakan restart.
- Mulai / hentikan / restart, switch autostart (enable/disable), dan log via
  `journalctl --user`.
- Restore database PostgreSQL dari dump `.sql`, `.dump`, `.backup`, atau `.tar`
  langsung dari halaman detail layanan.
- Banner untuk mengaktifkan **linger** agar layanan jalan saat boot tanpa login.

## Restore database

1. Pilih layanan di sidebar.
2. Klik **Restore** pada bagian **Database**.
3. Isi host, port, user, password, dan nama database tujuan.
4. Pilih file dump.
5. Aktifkan **Buat database jika belum ada** bila database tujuan belum ada.
6. Klik **Start**, lalu pantau log di dialog.

Format `.sql` diproses dengan `psql`. Format `.dump`, `.backup`, dan `.tar`
diproses dengan `pg_restore`. Keduanya memerlukan paket `postgresql` di Fedora.
Password hanya dipakai selama proses dan tidak disimpan. Opsi **Kosongkan objek
lama** akan menghapus skema `public` sebelum restore, jadi gunakan dengan hati-hati.

## Pasang

### Dari GitHub

```bash
git clone https://github.com/Hilalfajar/service-center.git
cd service-center
./scripts/install.sh
```

### Dari paket rilis

Unduh dari halaman [Releases](https://github.com/Hilalfajar/service-center/releases).

Fedora / RHEL:

```bash
sudo dnf install ./service-center-1.1.0-1.x86_64.rpm
```

Debian / Ubuntu:

```bash
sudo apt install ./service-center_1.1.0_amd64.deb
```

Arsip tar.gz:

```bash
tar -xzf service-center-1.1.0.tar.gz
cd service-center-1.1.0
./scripts/install.sh
```

Paket sistem memasang aplikasi ke `/usr/bin/service-center` dan
`/usr/lib/service-center`. Instalasi lewat skrip memasang ke `~/.local`
tanpa perlu root. Semua cara membutuhkan GTK4, libadwaita, dan PyGObject.
Paket RPM dan DEB memasang dependensi tersebut otomatis.

Launcher: `~/.local/bin/service-center` atau `/usr/bin/service-center`,
plus entri menu aplikasi.

## Uninstall

Paket sistem:

```bash
sudo dnf remove service-center
```

```bash
sudo apt remove service-center
```

Instalasi lewat skrip:

```bash
rm -rf ~/.local/share/service-center ~/.local/bin/service-center \
       ~/.local/share/applications/io.github.hilalfajar.ServiceCenter.desktop \
       ~/.local/share/icons/hicolor/scalable/apps/service-center.svg
```

## Catatan

Layanan disimpan sebagai unit user di `~/.config/systemd/user/sc-<id>.service`
dan registry di `~/.local/share/service-center/services.json`.
Menghapus layanan dari aplikasi tidak menghapus file programnya.
