import signal
import sys
import time 
import fiona
import geopandas as gpd
import pandas as pd
import pyproj
import antimeridian
from shapely.geometry import Point
from shapely.geometry import box as Box
from shapely.geometry import shape as Shape
from datetime import timedelta
 
from typing import ( 
    Any, 
    Optional, 
)

EMPIRICAL_SPEED_KM_PER_MS = 100.0  # RTT -> one-way: 200 km/ms fiber propagation / 2
HALF_EARTH_CIRCUMFERENCE_M = 19975000 # Earth's circumference ~ 40,075 km
MAP_BOUNDS = Box(-179.9, -60, 179.9, 85) # a usable map area, excluding extreme regions
            # min_lon: -179.9 # west of the International Date Line
            # min_lat: -60 # southern hemisphere, but not all the way to Antarctica
            # max_lon: 179.9 # east of the Date Line
            # max_lat: 85 # near the North Pole
MIN_RADIUS_M = 5000 # meters
# Get the area
    #   EPSG:3857 (Web Mercator): strongly distorts area, especially at high latitudes.
    #   EPSG:6933 (World Cylindrical Equal-Area) preserves area everywhere.
MAP_BOUNDS_AREA_M2 = (
    gpd.GeoDataFrame(geometry=[MAP_BOUNDS], crs="EPSG:4326")
    .to_crs("EPSG:6933")
    .area.iloc[0]
) 
QUIET = False  # type: bool
START = None  # type: Optional[float]


def progress(message: str, *args: Any, **kwargs: Any) -> None:
    """Print a progress report message with elapsed time."""
    global START, QUIET
    if QUIET:
        return

    now = time.monotonic()
    if START is None:
        START = now

    sys.stderr.write(
        "{}: {}\n".format(
            timedelta(seconds=now - START), message.format(*args, **kwargs)
        )
    ) 
    

def timeout_handler(signum, frame):
    raise TimeoutError


def load_basemap(path_to_map: str) -> gpd.GeoDataFrame:
    """ Loads in basemap of earth (all land regions)
    """
    def country_code(props):
        cc = props.get("iso_a2")
        if not cc or cc == "-99":
            cc = "X" + "".join(c for c in props["name_long"] if "A" <= c <= "Z")[:2]
        return cc.lower()

    def clean_shape(shp):
        geom = Shape(shp)
        if not geom.is_valid:
            geom = geom.buffer(0)
            assert geom.is_valid
        return geom

    # Collect country data into lists
    countries = []
    geometries = []
   
    with fiona.open(path_to_map) as fp:
        for rec in fp:
            name = rec["properties"]["name_long"]
            cc = country_code(rec["properties"])
            geom = clean_shape(rec["geometry"])

            countries.append({"name_long": name, "iso_a2": cc})
            geometries.append(geom)

    # Create a GeoDataFrame with country geometries and properties
    basemap_gdf = gpd.GeoDataFrame(countries, geometry=geometries, crs="EPSG:4326")

    return basemap_gdf

 
def radius_limit(minrtt: float) -> float:
    """Gets radius based on physical limits

    Assumption: a single propagation speed of 100km/ms
    The maximum distance a signal could travel given a round-trip time
    """    
    slope = EMPIRICAL_SPEED_KM_PER_MS * 1000  # meters per millisecond
    # RTT/2 is baked into the 100 constant (= propagation_speed / 2)
    max_dist = slope * minrtt 
    return max(max_dist, 0) 


def radius_for_cal(cal, minrtt):
    """Gets radius based on calibration and rtt
    """ 
    # calibration regression learned full-RTT → one-way distance directly
    max_dist = cal(minrtt)
    return max(max_dist, 0)

def disk_on_globe(long: float, lat: float, radius: float, src_pid, disk_type: str):
    """ Given lat, long, and furthest a ping could travel in that time, 
    get a disk on the globe
    """ 
    # if disk_type == "empirical":
    #     radius = radius * 1.15 # add 15% buffer
 
    radius = max(radius, MIN_RADIUS_M)

    if radius >= HALF_EARTH_CIRCUMFERENCE_M:
        return None  # Disk wraps globe, provides no constraint
        # return MAP_BOUNDS

    # Define azimuthal equidistant projection centered on point
    aeqd_crs = pyproj.CRS(
        proj="aeqd", ellps="WGS84", datum="WGS84", lat_0=lat, lon_0=long
    )
 
    point = gpd.GeoSeries([Point(long, lat)], crs="EPSG:4326")
    point_aeqd = point.to_crs(aeqd_crs)

    disk = point_aeqd.buffer(radius)  # Buffer in meters

    disk_area_m2 = disk.area.iloc[0]  # Area in m²
 
    if disk_area_m2 > (MAP_BOUNDS_AREA_M2 / 5):
        return None  # Disk too large

    # Convert the disk back to WGS84 (EPSG:4326) for return
    disk_geo = disk.to_crs("EPSG:4326").iloc[0]

    # Fix antimeridian-crossing polygons (large disks that cross ±180 longitude)
    if not disk_geo.is_valid:
        disk_geo = antimeridian.fix_polygon(disk_geo)
        # Sanity check: a disk must contain its own center
        if not disk_geo.contains(Point(long, lat)):
            return None

    # # Repair self-intersections from AEQD→WGS84 projection (large disks)
    # if not disk_geo.is_valid:
    #     disk_geo = disk_geo.buffer(0)

    return disk_geo
 

def max_subset_with_nonempty_intersection(disks: list, base_region):
    """Depth first search to find best region that intersects most disks.

    Returns:
        (best_region, n_disks_in_subset) — n_disks_in_subset is the number of
        disks whose pairwise intersection produced ``best_region``.  Equals 0
        when no disks were provided or when the DFS fell back to ``base_region``
        (timeout / no improving subset).
    """
    if base_region.is_empty:
        raise ValueError("base_region must not be empty")

    n_disks = len(disks)
    if n_disks == 0:
        return base_region, 0

    # trying to find region that intersects the most disks
    best_region = base_region
    # disks that intersect the best_region
    best_subset = ()
    # area of the best region
    best_area = base_region.area
    disks = sorted(disks, key=lambda d: d.area)

    # Notionally, we begin by discovering that the base_region
    # intersected with nothing is the base_region, which is already
    # known not to be empty.  Stack the subtrees in reverse order
    # so that the deeper trees will be processed first.
    stack = [((i,), base_region) for i in reversed(range(n_disks))]

    # For profiling, count number of subsets considered.
    subsets_considered = 0
    empty_subsets = 0
    max_depth = 50

    try:
        while stack:
            candidate, parent_region = stack.pop()

            # The largest candidate set that is a superset of "candidate" is
            # "candidate" plus all of the disks that have not yet been
            # considered for inclusion in "candidate", which is all of the
            # disks numbered greater than the largest index in "candidate"
            # (which is always the last index in "candidate").  If that set is
            # _smaller_ than best_subset, this subtree of candidates
            # cannot possibly beat it; we don"t even need to build the
            # intersections.

            if len(candidate) + (n_disks - candidate[-1]) < len(best_subset):
                continue

            # The parent_region is already the intersection of the
            # base_region with the disks labeled cand[0] through cand[-2].
            # It remains to intersect it with cand[-1].
            subsets_considered += 1

            cand_region = parent_region.buffer(0).intersection(
                disks[candidate[-1]].buffer(0)
            )

            if cand_region.is_empty:
                empty_subsets += 1
                continue

            if len(candidate) > len(best_subset) or (
                len(candidate) == len(best_subset) and cand_region.area < best_area
            ):

                best_subset = candidate
                best_region = cand_region
                best_area = cand_region.area

            # Early stopping: Check if the depth exceeds the threshold
            if len(candidate) >= max_depth:
                break

            # Queue all of the children of this node.
            stack.extend(
                (candidate + (i,), cand_region) for i in range(max(candidate) + 1, n_disks)
            )

    except TimeoutError:
        progress("TimeoutError: stopped run, returning base region")
        return base_region, 0

    return best_region, len(best_subset)


def find_plausible_intersection(empirical_disks: list, physical_limit_disks: list):
    """First search for best region with physical
    and then use that as bounds for intersection of empirical.

    Returns:
        (region, n_empirical_in_subset) — number of empirical disks the DFS
        included in the final intersection (excludes physical-limit disks).
    """
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(1800)
    phy_region, _ = max_subset_with_nonempty_intersection(physical_limit_disks, MAP_BOUNDS)
    signal.alarm(0)

    signal.alarm(60)
    best, n_emp_in_subset = max_subset_with_nonempty_intersection(empirical_disks, phy_region)
    signal.alarm(0)
    return best, n_emp_in_subset
  