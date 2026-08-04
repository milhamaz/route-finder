"""In-app editor: click the map to reposition an existing point or add a new one."""
from datetime import time

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

WH_FILE = "data/warehouses.csv"
DP_FILE = "data/drop_points.csv"
ID_COL = {WH_FILE: "id", DP_FILE: "id_dp"}
NEW_OPT = "+ Tambah titik baru"
SELECT_KEY = "edit_choice_select"
DEFAULT_ZOOM = 15


def _button_spacer() -> None:
    """Vertically align a button with adjacent labeled inputs/captions."""
    st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)


def render_edit_panel(wh_all: pd.DataFrame, dp_all: pd.DataFrame) -> None:
    st.subheader("✏️ Edit Lokasi Warehouse & Drop Point")
    st.caption(
        "Pilih titik yang mau dipindah (atau tambah baru), klik lokasi di peta — muncul pin preview "
        "merah yang bisa diklik ulang buat geser sebelum disimpan."
    )

    points = pd.concat(
        [
            wh_all.assign(tipe="Warehouse", file=WH_FILE)[["id", "nama", "lat", "lon", "tipe", "file"]],
            dp_all.rename(columns={"id_dp": "id"}).assign(tipe="Drop Point", file=DP_FILE)[
                ["id", "nama", "lat", "lon", "tipe", "file"]
            ],
        ],
        ignore_index=True,
    )
    labels = {NEW_OPT: NEW_OPT}
    for _, r in points.iterrows():
        labels[r["id"]] = f"{r['tipe']} · {r['id']} — {r['nama']}"

    # Peek at the widget's own session state to decide this run's layout
    # before instantiating it (Streamlit already updates it pre-rerun).
    is_new = st.session_state.get(SELECT_KEY, NEW_OPT) == NEW_OPT

    new_id = new_nama = new_wh = None
    new_volume, new_jam_buka, new_jam_tutup = None, None, None
    save_clicked = False

    if is_new:
        r1c1, r1c2 = st.columns([1, 2])
        with r1c1:
            choice = st.selectbox(
                "Titik", options=[NEW_OPT] + points["id"].tolist(),
                format_func=lambda k: labels[k], key=SELECT_KEY,
            )
        with r1c2:
            new_type = st.radio("Tipe titik baru", ["Drop Point", "Warehouse"], horizontal=True)

        if new_type == "Warehouse":
            default_id, target_file = f"WH-{len(wh_all) + 1:02d}", WH_FILE
        else:
            default_id, target_file = f"DP-{len(dp_all) + 1:02d}", DP_FILE

        if st.session_state.get("edit_choice_prev") != choice:
            st.session_state["edit_choice_prev"] = choice
            st.session_state.pop("pending_coord", None)
        pending = st.session_state.get("pending_coord")

        if new_type == "Drop Point":
            r2c1, r2c2, r2c3 = st.columns([1, 2, 1])
        else:
            r2c1, r2c2 = st.columns([1, 2])
        with r2c1:
            new_id = st.text_input("ID baru", value=default_id)
        with r2c2:
            new_nama = st.text_input("Nama lokasi")
        if new_type == "Drop Point":
            with r2c3:
                new_wh = st.selectbox(
                    "Warehouse asal", options=wh_all["id"].tolist(),
                    format_func=lambda i: f"{i} — {wh_all.set_index('id')['nama'][i]}",
                )

            r3c1, r3c2, r3c3 = st.columns([1, 1, 1])
            with r3c1:
                new_volume = st.number_input("Volume (m³)", min_value=0, step=1, value=0)
            with r3c2:
                new_jam_buka = st.time_input("Jam buka", value=time(8, 0))
            with r3c3:
                new_jam_tutup = st.time_input("Jam tutup", value=time(17, 0))
        else:
            r3c1, r3c2 = st.columns([1, 1])
            with r3c1:
                new_jam_buka = st.time_input("Jam operasional buka", value=time(7, 0))
            with r3c2:
                new_jam_tutup = st.time_input("Jam operasional tutup", value=time(17, 0))

        r4c1, r4c2 = st.columns([3, 1])
        with r4c1:
            st.caption("Koordinat baru")
            st.write(f"`{pending[0]:.6f}, {pending[1]:.6f}`" if pending else "_belum dipilih_")
        with r4c2:
            _button_spacer()
            disabled = pending is None or not new_id or not new_nama
            if new_type == "Drop Point":
                disabled = disabled or new_volume <= 0
            save_clicked = st.button("✅ Simpan", disabled=disabled, use_container_width=True)
    else:
        r1c1, r1c2, r1c3, r1c4 = st.columns([1, 1, 1, 1])
        with r1c1:
            choice = st.selectbox(
                "Titik", options=[NEW_OPT] + points["id"].tolist(),
                format_func=lambda k: labels[k], key=SELECT_KEY,
            )

        if st.session_state.get("edit_choice_prev") != choice:
            st.session_state["edit_choice_prev"] = choice
            st.session_state.pop("pending_coord", None)
        pending = st.session_state.get("pending_coord")

        current = points[points["id"] == choice].iloc[0]
        target_file = current["file"]

        with r1c2:
            st.caption("Koordinat saat ini")
            st.write(f"`{current['lat']:.6f}, {current['lon']:.6f}`")
        with r1c3:
            st.caption("Lokasi baru")
            st.write(f"`{pending[0]:.6f}, {pending[1]:.6f}`" if pending else "_belum dipilih_")
        with r1c4:
            _button_spacer()
            save_clicked = st.button("✅ Simpan", disabled=pending is None, use_container_width=True)

    # Center on: a pending click > the point being edited > last worked area > dataset mean.
    # NOTE: streamlit-folium's component key is a hash that includes the rendered
    # Leaflet script itself, so ANY change to location/zoom/markers between runs
    # forces a full remount (destroy + recreate the map), not just a soft pan.
    # Trying to live-track the user's pan/zoom therefore fights itself — every
    # attempt to "restore" the view changes the script and triggers another
    # remount. Keeping this fully static except when the edit target genuinely
    # changes is what keeps ordinary panning/zooming from flickering.
    if pending:
        view_center = list(pending)
    elif not is_new:
        view_center = [current["lat"], current["lon"]]
    else:
        view_center = st.session_state.get(
            "edit_last_center", [points["lat"].mean(), points["lon"].mean()]
        )

    m = folium.Map(location=view_center, zoom_start=DEFAULT_ZOOM, tiles="OpenStreetMap")
    for _, r in points.iterrows():
        is_target = (not is_new) and r["id"] == choice
        folium.CircleMarker(
            [r["lat"], r["lon"]],
            radius=7 if is_target else 5,
            color="#dc2626" if is_target else "#6b7280",
            fill=True,
            fill_opacity=0.9,
            tooltip=f"{r['id']} — {r['nama']}",
        ).add_to(m)

    if pending:
        folium.Marker(
            pending,
            tooltip="Lokasi baru (belum disimpan) — klik lagi buat geser",
            icon=folium.Icon(color="red", icon="crosshairs", prefix="fa"),
        ).add_to(m)

    map_data = st_folium(
        m,
        width=None,
        height=500,
        returned_objects=["last_clicked"],
        key=f"edit_map_{st.session_state.get('edit_map_version', 0)}",
    )

    if map_data and map_data.get("last_clicked"):
        clicked = (map_data["last_clicked"]["lat"], map_data["last_clicked"]["lng"])
        if clicked != pending:
            st.session_state["pending_coord"] = clicked
            st.session_state["edit_last_center"] = list(clicked)
            st.rerun()

    if save_clicked:
        if is_new and new_jam_buka >= new_jam_tutup:
            st.error("Jam buka harus lebih awal dari jam tutup.")
            return

        id_col = ID_COL[target_file]
        df = pd.read_csv(target_file)
        if is_new:
            if new_id in df[id_col].values:
                st.error(f"ID {new_id} sudah ada di {target_file}.")
                return
            if new_type == "Warehouse":
                new_row = {
                    "id": new_id, "nama": new_nama, "lat": pending[0], "lon": pending[1],
                    "jam_buka": f"{new_jam_buka:%H:%M}", "jam_tutup": f"{new_jam_tutup:%H:%M}",
                }
            else:
                new_row = {
                    "id_dp": new_id, "id_wh": new_wh, "nama": new_nama,
                    "lat": pending[0], "lon": pending[1], "volume_m3": new_volume,
                    "jam_buka": f"{new_jam_buka:%H:%M}", "jam_tutup": f"{new_jam_tutup:%H:%M}",
                }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            success_msg = f"{new_id} ditambahkan."
        else:
            df.loc[df[id_col] == choice, ["lat", "lon"]] = pending
            success_msg = f"{choice} dipindah ke koordinat baru."

        try:
            df.to_csv(target_file, index=False)
        except PermissionError:
            st.error(
                f"Gak bisa nyimpen ke `{target_file}` — file-nya kemungkinan lagi kebuka di "
                "program lain (Excel/Notepad/dll). Tutup dulu file-nya terus coba Simpan lagi "
                "(perubahan di atas belum hilang)."
            )
            return

        st.success(success_msg)
        st.session_state["edit_map_version"] = st.session_state.get("edit_map_version", 0) + 1
        st.session_state.pop("pending_coord", None)
        st.rerun()
