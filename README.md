# 🗺️ Route Finder — Milk Run Fleet Optimization

Streamlit app buat optimasi rute pengiriman *milk run*: pilih satu warehouse (WH), pilih drop point (DP) yang mau dikirim, lalu app-nya otomatis:

1. Membagi DP-DP itu ke armada (truk) milik WH tersebut sesuai kapasitas masing-masing (**Capacitated Vehicle Routing Problem**, via **Clarke & Wright savings algorithm**)
2. Mencari urutan kunjungan paling optimal per armada, mengikuti jalan asli — bukan garis lurus (**Traveling Salesman Problem**, exact via **Held-Karp**, fallback approximation via **Christofides**)
3. Menghitung jam berangkat, ETA tiap stop, dan biaya BBM per trip
4. Kalau armada masih sempat, otomatis dikasih **trip kedua** di hari yang sama

Data-nya dummy/simulasi (nama toko, lokasi, volume semuanya fiktif) — dibuat buat latihan/portofolio, bukan operasional nyata.

## Setup

```bash
git clone <repo-ini>
cd <folder-repo>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

App ini butuh API key gratis dari [OpenRouteService](https://openrouteservice.org/dev/#/signup) buat ngitung jarak/waktu tempuh jalan asli & gambar rute di peta:

1. Daftar & bikin API key di link di atas
2. Copy `.streamlit/secrets.toml.example` jadi `.streamlit/secrets.toml`
3. Isi `ORS_API_KEY` di file itu dengan key kamu sendiri

> ⚠️ **Tanpa langkah ini app-nya gak akan bisa hitung rute sama sekali** — semua fitur peta & optimasi bergantung ke API tersebut. `secrets.toml` sudah di-gitignore, jadi key kamu gak akan ke-commit kalau kamu push perubahan.

Jalankan:

```bash
streamlit run app.py
```

## Struktur data (`data/`)

| File | Kolom | Keterangan |
|---|---|---|
| `warehouses.csv` | `id, nama, lat, lon, jam_buka, jam_tutup` | Satu baris = satu WH. `jam_buka` menentukan kapan armada mulai bisa dimuat. |
| `drop_points.csv` | `id_dp, id_wh, nama, lat, lon, volume_m3, jam_buka, jam_tutup` | Satu baris = satu titik kirim. `id_wh` = assignment manual ke WH mana (bukan otomatis nearest-WH). `volume_m3` dipakai buat CVRP split. `jam_buka`/`jam_tutup` dicek terhadap ETA, cuma informational (gak mengubah urutan rute). |
| `armada.csv` | `id_armada, id_wh, nama, kapasitas_m3, konsumsi_km_per_liter` | Satu baris = satu truk. `konsumsi_km_per_liter` dipakai buat hitung biaya BBM (harga solar di-hardcode di `app.py`, konstanta `HARGA_SOLAR_PER_LITER`). |

Semua data ini bisa diedit langsung di CSV, atau lewat mode "✏️ Edit Lokasi" di sidebar app (klik peta buat pindah/nambah titik — lebih akurat daripada ngetik lat/lon manual).

## Struktur kode

| File | Isi |
|---|---|
| `app.py` | Entry point. Sidebar filter (pilih WH → pilih DP), orkestrasi CVRP + multi-trip, render peta & tabel. |
| `optimize.py` | Algoritma murni, gak ada dependency ke Streamlit/data: `solve_order()` (TSP per armada), `cw_clusters()` + `assign_clusters()` (CVRP — bagi DP ke armada). |
| `routing.py` | Satu-satunya file yang manggil OpenRouteService API — matrix jarak/waktu & geometri jalan. |
| `edit_panel.py` | Mode edit klik-di-peta buat mindahin/nambah WH & DP. |

### Alur singkat

```
CSV → pilih 1 WH → pilih DP → matrix jarak/waktu (ORS)
    → cw_clusters() + assign_clusters(): DP dibagi ke armada (wave 1)
    → sisa yang belum kebagian? assign lagi ke armada paling cepat available (wave 2, maks 2x trip/armada)
    → solve_order() per armada per trip: urutan kunjungan optimal
    → hitung timeline (muat → jalan → bongkar per stop → balik), cek jam operasional tiap DP
    → gambar rute jalan asli (ORS) + legend per armada di peta
```

Detail lebih dalam (rumus Held-Karp, kenapa tur optimal gak menyilang, bug fairness yang ditemukan & di-fix pas CVRP di-develop, dll) ada di tulisan terpisah — gak disertakan di repo ini.

## Batasan yang diketahui

- OpenRouteService free tier: maks ~59 titik aktif sekaligus per WH (limit matrix 3500 rute), 2000 request/hari, 40 request/menit.
- Durasi tempuh dari ORS berdasarkan tipe jalan, **bukan** data traffic real-time — jam sibuk vs sepi dianggap sama.
- Multi-trip dibatasi maksimal 2x jalan per armada per hari.
