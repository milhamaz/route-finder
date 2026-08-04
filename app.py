"""Entry point: pick one warehouse, split its drop points across armada by
capacity (CVRP via Clarke & Wright), route each vehicle (TSP), render the
result on a map with per-vehicle ETAs and fuel cost."""
from datetime import date, datetime, time, timedelta

import folium
import pandas as pd
import streamlit as st
from openrouteservice.exceptions import ApiError
from streamlit_folium import st_folium

from edit_panel import render_edit_panel
from optimize import assign_clusters, cw_clusters, solve_order
from routing import get_matrix, get_route_geometry

st.set_page_config(page_title="Route Finder", page_icon="🗺️", layout="wide")
st.title("🗺️ Route Finder — Milk Run Optimization")
st.caption(
    "Rute kunjungan optimal 1 warehouse ke drop point terpilih, dibagi ke armada sesuai "
    "kapasitas (Clarke & Wright), mengikuti jalan asli (OpenRouteService), pulang-pergi "
    "(round trip) kembali ke warehouse. Armada yang masih sempat jalan lagi otomatis dapet trip ke-2."
)

ROUTE_COLORS = ["#2563eb", "#f97316", "#16a34a", "#9333ea", "#dc2626", "#0891b2"]
ROUTE_DOTS = ["🔵", "🟠", "🟢", "🟣", "🔴", "🔷"]
HARGA_SOLAR_PER_LITER = 6800  # Rp — semua armada diasumsikan solar
LOADING_RATE_PER_UNIT = 0.1  # menit/m³ muat di WH (forklift)
UNLOAD_RATE_PER_UNIT = 0.2  # menit/m³ bongkar di DP (manual)
MAX_TRIPS_PER_VEHICLE = 2


def load_raw():
    wh = pd.read_csv("data/warehouses.csv").dropna(subset=["id", "lat", "lon"])
    dp = pd.read_csv("data/drop_points.csv").dropna(subset=["id_dp", "id_wh", "lat", "lon"])
    arm = pd.read_csv("data/armada.csv").dropna(subset=["id_armada", "id_wh"])
    return wh, dp, arm


def parse_hhmm(s) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def biaya_bbm(distance_m: float, konsumsi_km_per_liter: float) -> float:
    """Rp = jarak (km) / konsumsi (km/liter) x harga solar per liter."""
    return (distance_m / 1000 / konsumsi_km_per_liter) * HARGA_SOLAR_PER_LITER


st.sidebar.header("Filter")
if st.sidebar.button("🔄 Reload data"):
    st.cache_data.clear()
    st.rerun()

wh_all, dp_all, armada_all = load_raw()

if st.sidebar.toggle("✏️ Mode Edit Lokasi", key="edit_toggle"):
    render_edit_panel(wh_all, dp_all)
    st.stop()

wh_names = wh_all.set_index("id")["nama"]
selected_wh = st.sidebar.radio(
    "Warehouse",
    options=wh_all["id"].tolist(),
    format_func=lambda i: wh_names[i],
    index=None,
    key="wh_radio",
)

if selected_wh is None:
    st.info("Pilih minimal 1 WH di sidebar.")
    st.stop()

dp_wh = dp_all[dp_all["id_wh"] == selected_wh].reset_index(drop=True)
if dp_wh.empty:
    st.warning(f"{wh_names[selected_wh]} belum punya drop point (kolom `id_wh` di drop_points.csv).")
    st.stop()

dp_labels = dp_wh.set_index("id_dp")["nama"]
selected_dp_ids = st.sidebar.multiselect(
    "Drop point yang dikirim",
    options=dp_wh["id_dp"].tolist(),
    default=dp_wh["id_dp"].tolist(),
    format_func=lambda i: f"{i} — {dp_labels[i]}",
    key=f"dp_select_{selected_wh}",
)

if not selected_dp_ids:
    st.info("Pilih minimal 1 drop point di sidebar untuk menghitung rute.")
    st.stop()

active_dp = dp_wh[dp_wh["id_dp"].isin(selected_dp_ids)].reset_index(drop=True)

armada_wh = armada_all[armada_all["id_wh"] == selected_wh].reset_index(drop=True)
if armada_wh.empty:
    st.error(f"{wh_names[selected_wh]} belum punya armada terdaftar di `data/armada.csv`.")
    st.stop()

wh_row = wh_all[wh_all["id"] == selected_wh].iloc[0]
wh_jam_buka = parse_hhmm(wh_row["jam_buka"])
wh_jam_tutup = parse_hhmm(wh_row["jam_tutup"])
wh_open_dt = datetime.combine(date.today(), wh_jam_buka)

wh_point = pd.DataFrame([{
    "id": wh_row["id"], "nama": wh_row["nama"], "lat": wh_row["lat"], "lon": wh_row["lon"],
    "volume_m3": None, "jam_buka": None, "jam_tutup": None,
}])
dp_points = active_dp.rename(columns={"id_dp": "id"})[
    ["id", "nama", "lat", "lon", "volume_m3", "jam_buka", "jam_tutup"]
]
points = pd.concat([wh_point, dp_points], ignore_index=True)
coords = list(zip(points["lon"], points["lat"]))

max_points = int(3500 ** 0.5)
if len(points) > max_points:
    st.error(
        f"Titik aktif ({len(points)}) melebihi batas gratis OpenRouteService "
        f"(maks ~{max_points} titik sekaligus, karena matrix jarak dibatasi 3500 rute = titik²). "
        "Kurangi jumlah drop point aktif lewat filter di sidebar."
    )
    st.stop()

with st.spinner("Menghitung matrix jarak & waktu tempuh..."):
    try:
        durations, distances = get_matrix(coords)
    except ApiError as e:
        st.error(f"OpenRouteService menolak permintaan: {e}")
        st.stop()

dp_local_idx = list(range(1, len(points)))
demand = {idx: points.loc[idx, "volume_m3"] for idx in dp_local_idx}
vehicle_capacities = list(zip(armada_wh["id_armada"], armada_wh["kapasitas_m3"]))
armada_meta = armada_wh.set_index("id_armada")
# With 3+ vehicles, cap merges at the 2nd-largest capacity so Clarke-Wright can't glue
# everything into one blob only the single biggest truck can carry, stranding the rest
# idle. With only 1-2 vehicles that would just waste the bigger one's headroom for no
# reason, so fall back to the plain max there.
caps_desc = sorted((cap for _, cap in vehicle_capacities), reverse=True)
ceiling = caps_desc[1] if len(caps_desc) >= 3 else caps_desc[0]


def build_trip(vehicle_id: str, cluster: list[int], ready_at: datetime, trip_no: int) -> dict:
    local_idx_full = [0] + cluster
    sub_durations = [[durations[i][j] for j in local_idx_full] for i in local_idx_full]
    sub_distances = [[distances[i][j] for j in local_idx_full] for i in local_idx_full]
    local_order = solve_order(sub_durations)
    volume = sum(points.loc[i, "volume_m3"] for i in cluster)
    loading_min = volume * LOADING_RATE_PER_UNIT
    departure = ready_at + timedelta(minutes=loading_min)

    stops = []
    t = departure
    travel_duration = distance = 0.0
    for k in range(1, len(local_order)):
        leg = sub_durations[local_order[k - 1]][local_order[k]]
        travel_duration += leg
        distance += sub_distances[local_order[k - 1]][local_order[k]]
        t += timedelta(minutes=leg / 60)
        if local_order[k] == 0:
            return_time = t
            break
        dp_idx = local_idx_full[local_order[k]]
        row = points.loc[dp_idx]
        jam_buka, jam_tutup = parse_hhmm(row["jam_buka"]), parse_hhmm(row["jam_tutup"])
        eta = t.time()
        stops.append({
            "idx": dp_idx, "visit_seq": len(stops) + 1, "eta": t,
            "on_time": jam_buka <= eta <= jam_tutup,
        })
        t += timedelta(minutes=row["volume_m3"] * UNLOAD_RATE_PER_UNIT)

    return {
        "vehicle_id": vehicle_id, "trip_no": trip_no, "cluster": cluster,
        "global_order": [local_idx_full[k] for k in local_order],
        "distance": distance, "travel_duration": travel_duration,
        "volume": volume, "n_dp": len(cluster), "loading_min": loading_min,
        "departure": departure, "return_time": return_time, "stops": stops,
    }


clusters1 = cw_clusters(dp_local_idx, demand, durations, ceiling)
best_fit_order = sorted(vehicle_capacities, key=lambda t: t[1])
assignment1, unassigned1 = assign_clusters(clusters1, best_fit_order)

trips = []
ready_at = {vid: wh_open_dt for vid, _ in vehicle_capacities}
for vehicle_id, cluster in assignment1.items():
    trip = build_trip(vehicle_id, cluster, ready_at[vehicle_id], trip_no=1)
    trips.append(trip)
    ready_at[vehicle_id] = trip["return_time"]

unassigned_final = unassigned1
if unassigned1 and MAX_TRIPS_PER_VEHICLE >= 2:
    remaining = [i for cluster in unassigned1 for i in cluster]
    # idle vehicles (still at ready_at == wh_open_dt) sort first; among already-dispatched
    # vehicles, whichever returns soonest goes next.
    availability_order = sorted(vehicle_capacities, key=lambda vc: ready_at[vc[0]])
    clusters2 = cw_clusters(remaining, demand, durations, ceiling)
    assignment2, unassigned2 = assign_clusters(clusters2, availability_order)
    for vehicle_id, cluster in assignment2.items():
        trip = build_trip(vehicle_id, cluster, ready_at[vehicle_id], trip_no=2)
        trips.append(trip)
    unassigned_final = unassigned2

if unassigned_final:
    unassigned_idx = [idx for cluster in unassigned_final for idx in cluster]
    unassigned_names = ", ".join(f"{points.loc[i, 'id']} ({points.loc[i, 'nama']})" for i in unassigned_idx)
    unassigned_volume = sum(points.loc[i, "volume_m3"] for i in unassigned_idx)
    st.error(
        f"{len(unassigned_idx)} drop point ({unassigned_volume:.0f} m³) gak kebagian armada — "
        f"kapasitas {wh_names[selected_wh]} kurang buat menampung semua drop point terpilih walau "
        f"udah dicoba sampai {MAX_TRIPS_PER_VEHICLE}x jalan per armada: {unassigned_names}. "
        "Kurangi drop point yang dipilih atau tambah kapasitas armada di `data/armada.csv`."
    )

late_returns = [t for t in trips if t["return_time"].time() > wh_jam_tutup]
if late_returns:
    detail = ", ".join(f"{t['vehicle_id']} trip {t['trip_no']} ({t['return_time']:%H:%M})" for t in late_returns)
    st.info(
        f"FYI: {len(late_returns)} trip balik ke WH setelah jam operasional ({wh_row['jam_tutup']}) — "
        f"{detail}. Gak masalah selama semua DP di trip itu masih ke-service sebelum jam tutup masing-masing."
    )

total_duration = sum(t["travel_duration"] for t in trips)
total_distance = sum(t["distance"] for t in trips)
total_cost = sum(biaya_bbm(t["distance"], armada_meta.loc[t["vehicle_id"], "konsumsi_km_per_liter"]) for t in trips)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Drop point terlayani", sum(t["n_dp"] for t in trips))
col2.metric("Total jarak", f"{total_distance / 1000:.1f} km")
col3.metric("Total waktu tempuh", f"{total_duration / 60:.0f} menit")
col4.metric("Total biaya BBM", f"Rp {total_cost:,.0f}".replace(",", "."))

vehicle_order = []
for t in trips:
    if t["vehicle_id"] not in vehicle_order:
        vehicle_order.append(t["vehicle_id"])
vehicle_color = {vid: ROUTE_COLORS[i % len(ROUTE_COLORS)] for i, vid in enumerate(vehicle_order)}
vehicle_dot = {vid: ROUTE_DOTS[i % len(ROUTE_DOTS)] for i, vid in enumerate(vehicle_order)}

with st.expander("Ringkasan per armada"):
    summary = pd.DataFrame(
        [
            {
                "": vehicle_dot[t["vehicle_id"]],
                "Armada": f"{t['vehicle_id']} — {armada_meta.loc[t['vehicle_id'], 'nama']}",
                "Trip": t["trip_no"],
                "Drop Point": t["n_dp"],
                "Volume (m³)": f"{t['volume']:.0f} / {armada_meta.loc[t['vehicle_id'], 'kapasitas_m3']:.0f}",
                "Muat (menit)": round(t["loading_min"]),
                "Berangkat": f"{t['departure']:%H:%M}",
                "Balik": f"{t['return_time']:%H:%M}",
                "Jarak (km)": round(t["distance"] / 1000, 1),
                "Biaya (Rp)": f"{biaya_bbm(t['distance'], armada_meta.loc[t['vehicle_id'], 'konsumsi_km_per_liter']):,.0f}".replace(",", "."),
            }
            for t in trips
        ]
    )
    st.dataframe(summary, width="stretch", hide_index=True)

center = [points["lat"].mean(), points["lon"].mean()]
m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")


def dp_badge_icon(label: str, bg: str) -> folium.DivIcon:
    """Round numbered badge for drop points."""
    html = f"""
    <div style="
        background-color:{bg}; color:white; border-radius:50%;
        width:28px; height:28px; display:flex; align-items:center; justify-content:center;
        font-weight:bold; font-size:11px; border:2px solid white;
        box-shadow:0 0 3px rgba(0,0,0,0.5);">
        {label}
    </div>"""
    return folium.DivIcon(html=html, icon_size=(28, 28), icon_anchor=(14, 14))


folium.Marker(
    location=[wh_row["lat"], wh_row["lon"]],
    popup=f"WH — {wh_row['nama']} ({wh_row['id']})",
    tooltip=f"WH · {wh_row['nama']}",
    icon=folium.Icon(color="black", icon="warehouse", prefix="fa"),
).add_to(m)

unroutable = []
eta_rows = []
with st.spinner("Mengambil rute jalan asli per segmen..."):
    for t in trips:
        vehicle_id, trip_no = t["vehicle_id"], t["trip_no"]
        color = vehicle_color[vehicle_id]
        order = t["global_order"]
        for i in range(len(order) - 1):
            a, b = order[i], order[i + 1]
            start = (points.loc[a, "lon"], points.loc[a, "lat"])
            end = (points.loc[b, "lon"], points.loc[b, "lat"])
            path, is_real = get_route_geometry(start, end)
            if not is_real:
                dash = "12"  # coarse dash = ORS couldn't find a real road, straight-line fallback
                unroutable.append((points.loc[a, "id"], points.loc[b, "id"]))
            else:
                dash = "1,9" if trip_no == 2 else None  # fine dots = this vehicle's 2nd trip
            folium.PolyLine(
                path, color=color, weight=4, opacity=0.8, dash_array=dash,
                tooltip=f"{vehicle_id} — {armada_meta.loc[vehicle_id, 'nama']} (trip {trip_no})",
            ).add_to(m)

        for stop in t["stops"]:
            idx, visit_seq, eta = stop["idx"], stop["visit_seq"], stop["eta"]
            row = points.loc[idx]
            folium.Marker(
                location=[row["lat"], row["lon"]],
                popup=(
                    f"#{visit_seq} — {row['nama']} ({row['id']}) · trip {trip_no} · "
                    f"{vehicle_id} {armada_meta.loc[vehicle_id, 'nama']} · ETA {eta:%H:%M}"
                ),
                tooltip=f"{trip_no}.{visit_seq} · {row['nama']} ({vehicle_id})",
                icon=dp_badge_icon(f"{trip_no}.{visit_seq}", color),
            ).add_to(m)
            eta_rows.append({
                "Armada": f"{vehicle_id} — {armada_meta.loc[vehicle_id, 'nama']}",
                "Trip": trip_no,
                "Urutan": visit_seq,
                "Id": row["id"],
                "Nama": row["nama"],
                "ETA": f"{eta:%H:%M}",
                "Jam Operasional": f"{row['jam_buka']}–{row['jam_tutup']}",
                "Status": "✅ Sesuai jam" if stop["on_time"] else "⚠️ Di luar jam",
            })

if unroutable:
    segs = ", ".join(f"{a} ↔ {b}" for a, b in unroutable)
    st.warning(
        f"ORS tidak menemukan jalan untuk segmen: {segs} — digambar garis lurus putus-putus "
        "sementara. Kemungkinan koordinat titik tersebut meleset dari jalan yang ter-cover peta. "
        "Cek ulang lat/lon-nya di CSV."
    )

if trips:
    has_trip2 = any(t["trip_no"] == 2 for t in trips)
    legend_rows = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
        f'<span style="width:10px;height:10px;border-radius:50%;'
        f'background:{vehicle_color[vid]};display:inline-block;flex:none;"></span>'
        f'<span>{vid} — {armada_meta.loc[vid, "nama"]}</span></div>'
        for vid in vehicle_order
    )
    note = (
        '<div style="margin-top:6px;padding-top:6px;border-top:1px solid #ddd;">'
        '— solid: trip 1&nbsp;&nbsp;┄ dashed: trip 2</div>'
        if has_trip2 else ""
    )
    legend_html = f'''
    <div style="
        position: fixed; bottom: 30px; left: 30px; z-index: 9999;
        background: white; color: #111; padding: 8px 12px; border-radius: 8px;
        box-shadow: 0 1px 6px rgba(0,0,0,0.35); font-size: 12px;">
        {legend_rows}{note}
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))

st_folium(m, width=None, height=650, returned_objects=[])

with st.expander("Urutan kunjungan & ETA", expanded=True):
    if not eta_rows:
        st.info("Gak ada rute yang berhasil dibuat.")
    else:
        eta_df = pd.DataFrame(eta_rows)
        if (eta_df["Status"] == "⚠️ Di luar jam").any():
            st.warning("Ada drop point yang ETA-nya di luar jam operasional — cek tabel di bawah.")
        st.dataframe(eta_df, width="stretch", hide_index=True)
