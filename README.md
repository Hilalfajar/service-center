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
- Banner untuk mengaktifkan **linger** agar layanan jalan saat boot tanpa login.

## Pasang

```bash
./scripts/install.sh
```

Launcher: `~/.local/bin/service-center`, plus entri menu aplikasi.

## Uninstall

```bash
rm -rf ~/.local/share/service-center ~/.local/bin/service-center \
       ~/.local/share/applications/io.github.hilalfajar.ServiceCenter.desktop \
       ~/.local/share/icons/hicolor/scalable/apps/service-center.svg
```

## Catatan

Layanan disimpan sebagai unit user di `~/.config/systemd/user/sc-<id>.service`
dan registry di `~/.local/share/service-center/services.json`.
Menghapus layanan dari aplikasi tidak menghapus file programnya.
