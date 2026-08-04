"""ORS helpers: distance/duration matrix and real-street route geometry."""
import openrouteservice
import streamlit as st
from openrouteservice.exceptions import ApiError


@st.cache_resource
def get_client():
    return openrouteservice.Client(key=st.secrets["ORS_API_KEY"])


@st.cache_data
def get_matrix(coords: list[tuple[float, float]]):
    """coords: list of (lon, lat). Returns (durations, distances) in seconds/meters."""
    client = get_client()
    result = client.distance_matrix(
        locations=coords,
        profile="driving-car",
        metrics=["distance", "duration"],
    )
    return result["durations"], result["distances"]


@st.cache_data
def get_route_geometry(start: tuple[float, float], end: tuple[float, float]):
    """start/end: (lon, lat). Returns (points, is_real_road).

    Falls back to a straight line between start/end when ORS can't find a
    routable road near one of the coordinates (bad/remote data point), instead
    of failing the whole app."""
    client = get_client()
    try:
        result = client.directions(
            coordinates=[start, end],
            profile="driving-car",
            format="geojson",
        )
    except ApiError:
        return [(start[1], start[0]), (end[1], end[0])], False

    coords = result["features"][0]["geometry"]["coordinates"]
    return [(lat, lon) for lon, lat in coords], True
