#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Analyze the locations of the RIPE landmarks relative to a world map.
# Copyright 2019 Zack Weinberg <zackw@panix.com> &
#                Shinyoung Cho <cho.grace71@gmail.com>
#
# This program is free software:  you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.  See the file LICENSE in the
# top level of the source tree containing this file for further details,
# or consult <https://www.gnu.org/licenses/>.

"""Analyze the locations of the RIPE landmarks relative to a world
political map.  Specifically, we calculate the shortest distance from
each landmark to each country on the map, after combining political
subunits according to configurable rules.
This program could in principle work with any map readable by Fiona,
but it assumes some of the internal structure of the Natural Earth
"ne_10m_admin_0_map_units" map, and may not work correctly with
anything else.

usage: $ python3 analyze_landmarks.py landmarks-and-distances.csv.gz landmarks.csv ne_10m_admin_0_map_units.zip merges.yml iso3166.csv

landmarks.csv comes from retrieve_landmarks.py
"""

import argparse
import contextlib
import csv
import datetime
import ipaddress
import math
import multiprocessing
import os
import sys
import tempfile
import time
from collections import OrderedDict, defaultdict

import fiona
import yaml
from pyproj.crs.crs import CRS, ProjectedCRS
from pyproj.transformer import Transformer
from shapely.geometry.base import BaseGeometry as Geometry
from shapely.geometry.geo import shape
from shapely.geometry.point import Point
from shapely.ops import transform as sh_transform
from shapely.ops import unary_union
from shapely.validation import explain_validity

# some versions of pyproj misspell this name
try:
    from pyproj.crs.coordinate_operation import (
        AzimuthalEquidistantConversion as AEQDConversion,  # type:ignore[attr-defined]
    )
except ImportError:
    from pyproj.crs.coordinate_operation import (
        AzumuthalEquidistantConversion as AEQDConversion,
    )

# Type hints
#
from typing import (
    IO,
    Any,
    AnyStr,
    DefaultDict,
    Dict,
    Iterator,
    List,
    MutableMapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

# somewhat dirty but we don't have much of a choice here
IpAddress = ipaddress._BaseAddress

#
# Utility
#

QUIET = False  # type: bool
START = None  # type: Optional[float]
WORKER_DATA = None  # type: Optional[Tuple[Dict[str, Geometry], CRS, float]]


def progress(message: str, *args: Any, **kwargs: Any) -> None:
    """Print a progress report message with elapsed time.
    Args:
        message: a progress report message
        *args:
        **kwargs:
    Returns:
        None
    """
    global START, QUIET
    if QUIET:
        return

    now = time.monotonic()
    if START is None:
        START = now

    sys.stderr.write(
        "{}: {}\n".format(
            datetime.timedelta(seconds=now - START), message.format(*args, **kwargs)
        )
    )


def open_with_auto_compression(
    name: Any, mode: str = "rt", *, ext: Optional[str] = None, **kwargs: Any
) -> Any:
    """Open a file, automatically compressing or decompressing it when
    compression is indicated by the file extension.  If 'ext' is
    supplied, it overrides the extension on 'name' (this is useful
    when 'name' is actually a file-like object).  All other arguments
    are passed down to open().
    Args:
        None
     Returns:
         Any (?)
    """
    if ext is None:
        assert isinstance(name, str)
        ext = os.path.splitext(name)[1]
    if ext.startswith("."):
        ext = ext[1:]

    if ext == "gz":
        import gzip

        return gzip.open(name, mode, **kwargs)
    elif ext == "bz2":
        import bz2

        return bz2.open(name, mode, **kwargs)
    elif ext == "xz":
        import lzma

        return lzma.open(name, mode, format=lzma.FORMAT_XZ, **kwargs)
    elif ext == "lzma":
        import lzma

        return lzma.open(name, mode, format=lzma.FORMAT_ALONE, **kwargs)
    else:
        assert isinstance(name, str) or isinstance(name, int)
        return open(name, mode, **kwargs)


@contextlib.contextmanager
def atomic_rewrite(name: str, mode: str = "wt", **kwargs: Any) -> Iterator[IO[AnyStr]]:
    """Create a file named NAME, or, if it exists, atomically replace it
    with new contents.
    Args:
        name: the file's name
        mode: the statement must be a 'with'
        **kwargs:
     Returns:
         None
    This function is a context manager and must be used in a 'with'
    statement.  The 'as' value produced by the context manager will
    be a file-like object.  Whatever is written to this file-like
    object will become the new contents of NAME.  NAME is not
    touched until the context is exited; at that point, NAME will
    be created or atomically replaced.  (Technical note: the new
    file itself is fsync'ed before the atomic update to NAME, but
    the containing directory is not fsync'ed afterward. This only
    matters if the computer crashes shortly afterward *and* the
    operating system is buggy... but such bugs have been reported
    to exist.)
    """

    if mode not in ("w", "wb", "wt", "w+", "w+b", "w+t"):
        raise ValueError("inappropriate open mode {!r} for atomic_rewrite".format(mode))

    base, ext = os.path.splitext(os.path.basename(name))

    with tempfile.NamedTemporaryFile(
        suffix=ext, prefix=base + "-", dir=os.path.dirname(name)
    ) as tfp:
        # this nested 'with' ensures that the file compression, if any,
        # is properly finalized
        with open_with_auto_compression(tfp, mode, ext=ext, **kwargs) as fp:
            yield fp
            fp.flush()

        tfp.flush()
        os.fsync(tfp.fileno())

        # We can't just do os.replace(tfp.name, name), then
        # NamedTemporaryFile's context manager would throw an
        # exception upon trying to delete tfp.name.  We can't just do
        # os.link(tfp.name, name) either because os.link does _not_
        # replace an existing file.  And we can't use
        # tempfile.TemporaryFile and then os.link(tfp.fileno(), name,
        # AT_EMPTY_PATH) because (a) that may require special
        # privileges, (b) the os module doesn't expose AT_EMPTY_PATH,
        # and (c) that still wouldn't replace an existing file.
        xname = tfp.name + "."
        os.link(tfp.name, xname)
        os.replace(xname, name)


#
# Reading input files
#


def load_merge_rules(rulesfile: str) -> Dict[str, List[str]]:
    """Load a configuration file giving a set of merge rules to apply
    to map units.
    Args:
        rulesfile: a file giving a set of merge rules to apply to map units.
     Returns:
         Dictionary: sets of merge rules.
    """
    with open(rulesfile, "rt", encoding="utf-8") as fp:
        return {
            # uppercase everything to match the shapefile
            k.upper(): [s.upper() for s in v]
            for k, v in yaml.load(fp, Loader=yaml.BaseLoader).items()
        }


CountryInfo = NamedTuple(
    "CountryInfo", [("ISO_A3", str), ("ISO_A2", str), ("NAME", str)]
)
CountryInfoTable = Dict[str, CountryInfo]


def load_country_codes(ccs_file: str) -> CountryInfoTable:
    """Load a configuration file defining the set of ISO 3166-1 alpha-3 codes
    we care about, and check it for consistency.
    Args:
        ccs_file: a configuration file defining the set of ISO 3166-1 alpha-3 codes
    we care about.
    Returns:
        CountryInfoTable: a table with coutries' codes
    """
    with open(ccs_file, "rt", encoding="utf-8") as fp:
        rd = csv.DictReader(fp)
        if rd.fieldnames is None or sorted(rd.fieldnames) != [
            "ISO_A2",
            "ISO_A3",
            "NAME",
        ]:
            raise SystemExit("{}: wrong set of fields".format(ccs_file))

        rv = {}  # type: Dict[str, CountryInfo]
        success = True
        dupe_A2 = set()  # type: Set[str]
        dupe_NM = set()  # type: Set[str]

        for row in rd:
            ISO_A3 = row["ISO_A3"].strip().upper()
            ISO_A2 = row["ISO_A2"].strip().upper()
            NAME = row["NAME"].strip()

            if NAME in dupe_NM:
                sys.stderr.write("{}: duplicate name {}\n".format(ccs_file, NAME))
                success = False
            else:
                dupe_NM.add(NAME)

            if ISO_A2 in dupe_A2:
                sys.stderr.write(
                    "{}: duplicate ISO_A2 code {}\n".format(ccs_file, ISO_A2)
                )
                success = False
            else:
                dupe_A2.add(ISO_A2)

            if ISO_A3 in rv:
                sys.stderr.write(
                    "{}: duplicate ISO_A3 code {}\n".format(ccs_file, ISO_A3)
                )
                success = False
            else:
                rv[ISO_A3] = CountryInfo(ISO_A3, ISO_A2, NAME)

        if not success:
            raise SystemExit(1)
        return rv


Landmark = Dict[str, Any]
LandmarksTable = Dict[IpAddress, Landmark]


def load_landmarks(lm_file: str) -> LandmarksTable:
    """Load a set of landmarks from an existing CSV file, ensuring that each
    has at least a longitude, a latitude, and an IP address.  All other
    columns are preserved unexamined.  This is used to read both the
    new list of active landmarks, and the previous list of annotated
    landmarks, if any.
    Args:
        lm_file: the csv file name withh a set of landmarks
    Returns:
         LandmarksTable: dictionary of landmarks indexed by IP address.
    """
    rv = {}  # type: Dict[IpAddress, Landmark]

    with open_with_auto_compression(lm_file, "rt", encoding="utf-8") as fp:
        rd = csv.DictReader(fp)

        # Make sure the input has "longitude", "latitude", and "addr" columns.
        fields = set(rd.fieldnames or [])
        if (
            "longitude" not in fields
            or "latitude" not in fields
            or "addr" not in fields
        ):
            raise SystemExit("{}: wrong set of columns".format(lm_file))

        for row in rd:
            xrow = OrderedDict()  # type: Landmark
            addr = None  # type: Optional[IpAddress]
            for k in row.keys():
                # Some columns have expected types, parse them
                # accordingly.  Preserve all other columns as strings.
                if k == "longitude" or k == "latitude":
                    xrow[k] = float(row[k])
                elif k == "addr":
                    xrow[k] = addr = ipaddress.ip_address(row[k])
                else:
                    xrow[k] = row[k]

            assert addr is not None
            if addr in rv:
                raise SystemExit(
                    "{}: duplicate entry for landmark {}".format(lm_file, row["addr"])
                )

            rv[addr] = xrow

    return rv


def load_maps(
    mapfile: str, rules: MutableMapping[str, List[str]], ccs: CountryInfoTable
) -> Tuple[Dict[str, Geometry], CRS]:
    """Load and preprocess a set of maps for individual countries.
    MAPFILE should be a the name of a file containing a political
    map of the entire world, divided into "geographic units"; RULES
    specifies how to combine those geographic units into
    per-country maps; CCS lists all of the country codes we care
    about.  Returns a dictionary keyed by country code, whose
    values are shapely Shape objects, except that the special key
    "_CRS" gives a CRS object defining the coordinate system
    of the map.
    Args:
        mapfile: the name of a file containing a political
    map of the entire world, divided into "geographic units"
        rules: specifies how to combine those geographic units into
    per-country maps
        ccs: lists all country codes we care about
    Returns:
        maps: a dictionary keyed by country code
        map_crs:? map of crs
    """

    success = True
    all_gus = {}  # type: Dict[str, Any]
    add_rules = defaultdict(list)  # type: DefaultDict[str, List[str]]

    # type hints for reused loop induction variables:
    gus = []  # type: Sequence[str]

    # confirm we know the alpha-3 codes and names for all of the
    # existing rules
    for iso in rules.keys():
        if iso not in ccs:
            sys.stderr.write("{}: unknown ISO_A3 code {}\n".format(mapfile, iso))
            success = False

    mapfile = os.path.abspath(mapfile)
    if mapfile.endswith(".zip"):
        mapfile = "zip://" + mapfile
    try:
        with fiona.open(mapfile) as mapcoll:
            map_crs = CRS.from_wkt(mapcoll.crs_wkt)

            for feature in mapcoll.values():
                props = feature["properties"]

                GU_A3 = props["GU_A3"].strip().upper()
                SOV_A3 = props["SOV_A3"].strip().upper()

                if GU_A3 in all_gus:
                    sys.stderr.write(
                        "{}: duplicate GU_A3 code {}\n".format(mapfile, GU_A3)
                    )
                    success = False
                    continue

                all_gus[GU_A3] = feature["geometry"]

                # Add this GU_A3 to an implicit rule for its SOV_A3
                # if the SOV_A3 is an official ISO_A3 code
                # and there isn't an explicit rule for the SOV_A3.
                if SOV_A3 in ccs and SOV_A3 not in rules:
                    add_rules[SOV_A3].append(GU_A3)

    except Exception as e:
        sys.stderr.write("{}: {}\n".format(mapfile, e))
        success = False

    for iso, gus in add_rules.items():
        assert iso not in rules
        rules[iso] = gus

    for iso in ccs.keys():
        if iso not in rules:
            sys.stderr.write(
                "{}: no features for ISO_A3 code {} ({})\n".format(
                    mapfile, iso, ccs[iso].NAME
                )
            )
            success = False

    progress("checking geographic units")

    groups = {}
    for iso, gus in rules.items():
        grp = []
        for gu in gus:
            try:
                geo = all_gus.pop(gu)
            except KeyError:
                sys.stderr.write("{}: GU '{}' not found in map\n".format(mapfile, gu))
                success = False
            sh = shape(geo)
            if sh.is_valid:
                grp.append(sh)
            else:
                sys.stderr.write(
                    "{}: GU '{}' is invalid: {}\n".format(
                        mapfile, gu, explain_validity(sh)
                    )
                )
                success = False

        groups[iso] = grp

    if not success:
        raise SystemExit(1)

    progress("merging geographic units")
    maps = OrderedDict()  # type: Landmark
    for iso, gus in sorted(groups.items()):
        sh = unary_union(gus)
        if not sh.is_valid:
            sh = sh.buffer(0)
            if not sh.is_valid():
                sys.stderr.write(
                    "{}: map for '{}' is invalid after merge: {}\n".format(
                        mapfile, iso, explain_validity(sh)
                    )
                )
                success = False
                continue
        maps[iso] = sh

    if not success:
        raise SystemExit(1)

    return maps, map_crs


#
# Core analysis process
#


def reconcile_landmarks(
    old_landmarks: LandmarksTable, new_landmarks: LandmarksTable, ccs: CountryInfoTable
) -> Tuple[List[Landmark], List[Landmark]]:
    """OLD_LANDMARKS is a set of landmarks that were analyzed by a
    previous run of this program.  NEW_LANDMARKS is the current set
    of active landmarks.  CCS.keys() is the complete set of
    ISO 3166-1 alpha-3 codes we care about.
    For each landmark in NEW_LANDMARKS, add keys to its record for
    each of the ISO 3166-1 alpha-3 codes we care about.  If the
    same IP address appears in OLD_LANDMARKS, and the locations
    match, copy over all of the old analysis results.
    Returns two lists of landmark records.  COMPLETE contains all
    of the records from NEW_LANDMARKS for which OLD_LANDMARKS
    provided all of the data we need.  INCOMPLETE contains the
    remaining records; each of these has at least one alpha-3 key
    with None for its value, corresponding to a distance yet to be
    computed.

    Args:
        old_landmarks: a set of landmarks that were analyzed by a
    previous run of this program
        new_landmarks: the current set of active landmarks
        ccs: the complete set of ISO 3166-1 alpha-3 codes we care about
    Return:
        complete:  all of the records from NEW_LANDMARKS for which OLD_LANDMARKS
    provided all of the data we need
        incomplete: the remaining records
    """

    complete = []  # type: List[Landmark]
    incomplete = []  # type: List[Landmark]

    def not_same_location(lm: Landmark, old_lm: Landmark) -> bool:

        # haversine distance on the IUGG spherical-Earth approximation
        cos = math.cos
        sin = math.sin
        TO_RAD = math.pi / 180
        R1 = 6371.0088  # km

        n_lon = lm["longitude"] * TO_RAD
        n_lat = lm["latitude"] * TO_RAD
        o_lon = lm["longitude"] * TO_RAD
        o_lat = lm["latitude"] * TO_RAD

        slat = sin((n_lat - o_lat) / 2)
        slon = sin((n_lon - o_lon) / 2)

        d = (
            2
            * R1
            * math.asin(math.sqrt(slat * slat + cos(o_lat) * cos(n_lat) * slon * slon))
        )

        # "not the same location" if old and new locations are more
        # than 10km apart
        return d > 10

    EMPTY_LM = OrderedDict()  # type: Landmark
    for addr, lm in new_landmarks.items():
        old_lm = old_landmarks.get(addr, EMPTY_LM)
        if not_same_location(lm, old_lm):
            old_lm = EMPTY_LM

        dest_list = complete
        for iso in ccs.keys():
            v = old_lm.get(iso)
            lm[iso] = v
            if v is None:
                dest_list = incomplete

        dest_list.append(lm)

    return complete, incomplete


def rewrite_results(
    results_file: str, results: List[Landmark], ccs: CountryInfoTable
) -> None:
    """Sort the list of analyzed landmarks, RESULTS, perform some
    last-ditch consistency checks, and write the results to
    RESULTS_FILE.
    Args:
        results_file: the file we create to store all the results
        results: the results that we already have
        ccs: the table with all country information
        ? double check if I understand the flow
    Return:
        None
    """

    expected_keys = frozenset(results[0].keys())
    assert "addr" in expected_keys
    assert "longitude" in expected_keys
    assert "latitude" in expected_keys

    for row in results:
        r_keys = frozenset(row.keys())
        if r_keys != expected_keys:
            extra = r_keys - expected_keys
            missing = expected_keys - r_keys
            msg = "internal error: bad record for {}:\n".format(row["addr"])
            if extra:
                msg += "    extra keys: " + ", ".join(sorted(extra)) + "\n"
            if missing:
                msg += "  missing keys: " + ", ".join(sorted(missing)) + "\n"
            raise SystemExit(msg)

    results.sort(key=lambda row: row["addr"])  # type:ignore[no-any-return]

    # Write out columns in this order:
    # addr first
    # then all metadata columns (identified by not being a key of 'ccs'
    #   nor "latitude" nor "longitude") in alphabetical order
    # then longitude and latitude, in that order
    # then all distance columns (identified by being a key of 'ccs'),
    #   in alphabetical order
    dist_columns = []
    meta_columns = []
    for k in expected_keys:
        if k in ("addr", "latitude", "longitude"):
            pass
        elif k in ccs:
            dist_columns.append(k)
        else:
            meta_columns.append(k)
    dist_columns.sort()
    meta_columns.sort()

    columns = ["addr"]
    columns.extend(meta_columns)
    columns.append("longitude")
    columns.append("latitude")
    columns.extend(dist_columns)

    with atomic_rewrite(results_file, mode="wt", encoding="utf-8") as fp:
        wr = csv.DictWriter(fp, columns, dialect="unix", quoting=csv.QUOTE_MINIMAL)
        wr.writeheader()
        for row in results:
            wr.writerow(row)


# Data shared among all worker processes, initialized in main and then
# copied into the workers via fork()


def analyze_landmark(landmark: Landmark) -> Landmark:
    """Multiprocessing worker function.  Calculates distances from the
    landmark LANDMARK to each country defined by the map set, and
    updates the landmark record.
     Args:
         the landmark of a server
     Return: ?
         the shortest distance ( seems like a landmark to me but not sure if this
         shold be the distance)
    """

    # WORKER_DATA
    assert WORKER_DATA is not None
    maps, map_crs, resolution = WORKER_DATA

    lm_lon = landmark["longitude"]
    lm_lat = landmark["latitude"]

    # For each country, we want to calculate the shortest distance
    # from the landmark to any point in the land area of the country.
    # We do this by transforming the country's map to an
    # azimuthal-equidistant projection centered on the landmark's
    # location.  The closest point of the transformed map to (0,0), as
    # calculated by the usual Euclidean-plane closest-point algorithm,
    # is the correct closest point on the globe.
    #
    # Use the same datum as the map, instead of PROJ.4's default GRS80.
    aeqd_crs = ProjectedCRS(
        AEQDConversion(
            longitude_natural_origin=lm_lon,
            latitude_natural_origin=lm_lat,
        ),
        geodetic_crs=map_crs,
    )
    to_aeqd = Transformer.from_crs(map_crs, aeqd_crs, always_xy=True).transform

    lm_ploc = Point(0, 0)  # location of landmark on the _projected_ map

    for iso, mapg in maps.items():
        # Only calculate distances if they haven't already been calculated.
        if landmark[iso] is not None:
            continue

        aeqd_mapg = sh_transform(to_aeqd, mapg)
        if not aeqd_mapg.is_valid:
            try:
                aeqd_mapg = aeqd_mapg.buffer(0)
                if not aeqd_mapg.is_valid:
                    sys.stderr.write(
                        "*** aeqd({},{}) map for {} invalid: {}\n".format(
                            lm_lon, lm_lat, iso, explain_validity(aeqd_mapg)
                        )
                    )
                    continue
            except Exception as e:
                sys.stderr.write(
                    "*** aeqd({},{}) map for {} invalid: {}\n".format(
                        lm_lon, lm_lat, iso, explain_validity(aeqd_mapg)
                    )
                )
                sys.stderr.write("*** Fixup failed: {}\n".format(e))
                continue

        dist = lm_ploc.distance(aeqd_mapg)

        # round distance to nearest multiple of resolution
        dist = round(dist / resolution) * resolution
        # if it's an integer, make it typed so (avoids printing .0 suffixes)
        idist = int(dist)
        if idist == dist:
            dist = idist

        # if the landmark is inside the country, record this by making
        # the distance negative
        if lm_ploc.intersects(aeqd_mapg):
            dist = -dist

        landmark[iso] = dist

    return landmark


def initialize_worker_data(maps, crs, resolution):
    global WORKER_DATA
    WORKER_DATA = (maps, crs, resolution)


def main() -> None:
    """Command line main function.
    Args:
        None
    Returns:
        None
    """

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "results",
        help="File to record analysis results in."
        " If it already exists, it will be reread and updated."
        " Will be a CSV file with all of the columns from the"
        " 'landmarks' file, plus one column per country from the"
        " map (after merging), giving the distance in meters from"
        " that country to that landmark.  Automatically compressed"
        " if the file extension so indicates (e.g. .csv.gz).",
    )
    ap.add_argument(
        "landmarks",
        help="List of landmarks to analyze."
        " Should be a CSV file with at least two columns,"
        " named 'latitude' and 'longitude'.  All other columns"
        " are copied verbatim to the results.",
    )
    ap.add_argument(
        "map", help="Map to use for analysis" " (e.g. ne_10m_admin_0_map_units.zip)"
    )
    ap.add_argument("rules", help="Rules for merging map subunits.")
    ap.add_argument(
        "countrycodes",
        help="List of country codes and names."
        " Should have three columns, ISO_A3, ISO_A2, and NAME,"
        " containing respectively ISO 3166-1 alpha-3 and alpha-2"
        " codes, and an informal English name for the country.",
    )
    ap.add_argument(
        "-q", "--quiet", action="store_true", help="Don't print progress messages."
    )
    ap.add_argument(
        "-p",
        "--parallel",
        type=int,
        default=1,
        help="How many parallel worker processes to run.",
    )
    ap.add_argument(
        "-r",
        "--resolution",
        type=float,
        default=10,
        help="Map resolution in meters.  Distances will be rounded"
        " to a multiple of this number.",
    )
    args = ap.parse_args()

    if args.parallel < 1:
        ap.error("argument to -p/--parallel must be a positive integer")

    global QUIET
    QUIET = args.quiet

    progress("loading configuration")
    rules = load_merge_rules(args.rules)
    ccs = load_country_codes(args.countrycodes)

    progress("loading landmarks")
    new_landmarks = load_landmarks(args.landmarks)
    try:
        old_landmarks = load_landmarks(args.results)
    except FileNotFoundError:
        old_landmarks = {}

    lm_complete, lm_incomplete = reconcile_landmarks(old_landmarks, new_landmarks, ccs)

    assert len(lm_complete) + len(lm_incomplete) == len(new_landmarks)

    progress(
        "{} landmarks already analyzed, {} dropped, {} to analyze",
        len(lm_complete),
        max(0, len(old_landmarks) - len(new_landmarks)),
        len(lm_incomplete),
    )

    if lm_incomplete:
        progress("loading maps")
        maps, crs = load_maps(args.map, rules, ccs)
        # copy maps into workers via fork()

        total = len(lm_incomplete)
        processed = 0
        progress("analyzing {} landmarks...", total)

        last_report = time.monotonic()
        with multiprocessing.Pool(
            processes=args.parallel,
            initializer=initialize_worker_data,
            initargs=(maps, crs, args.resolution),
        ) as pool:
            # we use imap_unordered with chunksize=1 here because the
            # individual tasks are so slow that the communication overhead
            # is negligible, and it makes the progress reporting easier
            for result in pool.imap_unordered(
                analyze_landmark, lm_incomplete, chunksize=1
            ):
                lm_complete.append(result)
                processed += 1
                if not QUIET:
                    now = time.monotonic()
                    if processed == total or now - last_report > 10:
                        progress("{}/{}", processed, total)
                        last_report = now

            pool.close()
    else:
        progress("no new analysis required")
        if set(old_landmarks.keys()) == set(new_landmarks.keys()):
            progress("no update required")
            return

    assert len(lm_complete) == len(new_landmarks)

    progress("updating {}", args.results)
    rewrite_results(args.results, lm_complete, ccs)

    progress("done")


if __name__ == "__main__":
    main()
 
