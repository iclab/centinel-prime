from geopy.distance import geodesic 
from math import radians, sin, cos, sqrt, asin

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """ Function to compute Haversine distance in km
    """
    lon1, lon2, lat1, lat2 = map(radians, [lon1, lon2, lat1, lat2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    r = 6371  # Earth's radius in km
    return c * r


def calculate_geodesic(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dist_km = geodesic((lat1, lon1), (lat2, lon2)).km
    return dist_km