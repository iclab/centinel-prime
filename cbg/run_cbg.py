import csv
import os
import re  
import sys
import time  
import glob
import argparse
import geopandas as gpd
import pandas as pd  

import multiprocessing as mp
from functools import partial
from datetime import datetime, timedelta

from typing import ( 
    Any, 
    Optional, 
)

import cbg.cbg_core as cbg
 
QUIET = False  # type: bool
START = None  # type: Optional[float]
DEBUG = True


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


def _get_latest_date(base_path: str) -> datetime:
    """Find the most recent date across all providers in pings directory. 
    """
    latest_date = None
 
    for provider in os.listdir(base_path):
        provider_path = os.path.join(base_path, provider)
        if not os.path.isdir(provider_path):
            continue
 
        years = [d for d in os.listdir(provider_path) if d.isdigit()]
        for year in years:
            year_path = os.path.join(provider_path, year)
            if not os.path.isdir(year_path):
                continue
 
            months = [d for d in os.listdir(year_path) if d.isdigit()]
            for month in months:
                month_path = os.path.join(year_path, month)
                if not os.path.isdir(month_path):
                    continue
 
                days = [d for d in os.listdir(month_path) if d.isdigit()]
                for day in days:
                    try:
                        date = datetime(int(year), int(month), int(day))
                        if latest_date is None or date > latest_date:
                            latest_date = date
                    except ValueError:
                        continue

    if latest_date is None:
        raise ValueError(f"No valid date directories found in {base_path}")

    return latest_date

 
def data_frame(ping_files: list, lm_fpath: str) -> dict:
    """Load pings from a list of file paths."""
    vpn_probe_rtt = {}  # (vp_ip, country_code): { (probe_id, lon, lat): rtt }

    # Load anchor locations
    anchor_long_lat = {}    # anchor pid: (longitude, latitude)
    with open(lm_fpath, "r") as anchors:
        anchor_reader = csv.reader(anchors)
        for row in anchor_reader:
            if row[0] == 'addr':
                continue
            anchor_long_lat[int(row[2])] = (
                float(row[3]),  # long
                float(row[4])  # lat
            ) 

    for fpath in ping_files:
        fname = os.path.basename(fpath)
        if not fname.startswith("pings"):
            continue
        
        match = re.search(r'pings_[^_]+_([A-Z]{2})_([^\.]+)', fname)
        if not match:
            continue

        vpn_cc = match.group(1)
        vpn_name = match.group(2) 

        lm_pings = {}
        with open(fpath, "r") as f:
            reader = csv.reader(f) 
            next(reader, None)

            for row in reader: 
                prb_id = int(row[0])
                min_rtt = float(row[2])
                
                if prb_id in anchor_long_lat:
                    lm_pings[(prb_id, anchor_long_lat[prb_id])] = min_rtt
            
        vpn_probe_rtt[(vpn_name, vpn_cc)] = lm_pings.copy()
        
    return vpn_probe_rtt 

 
def process_single_vpn(vpn: list, 
                       pings_dict: dict, 
                       calibrations: dict, 
                       basemap: gpd.GeoDataFrame,
                       iso_fpath: str):
    """Process a single VPN and return its results.
    """
    progress(f"Start running for {vpn[0]}")
    start = time.time()

    row = {'vpn_name': vpn[0], }

    physical_limit_disks = []
    empirical_disks = []
    vpn_pings = pings_dict[vpn]

    # Create physical and empirical disks
    progress("Creating physical and empirical disks")
    for probe in vpn_pings:
        rtt = vpn_pings[probe]
        x, y = probe[1]  # longitude, latitude

        if rtt == "None" or rtt == 0.0:
            continue 

        prb_id = probe[0]

        # Physical disks
        radius = cbg.radius_limit(rtt) # maximum travel distance
        disk = cbg.disk_on_globe(x, y, radius, prb_id, "physical") # create a disk on the globe
        if disk:
            physical_limit_disks.append(disk)

        # Empirical disks
        if prb_id in calibrations: 
            radius = cbg.radius_for_cal(calibrations[prb_id], rtt)
            disk = cbg.disk_on_globe(x, y, radius, prb_id, "empirical")
            if disk:
                empirical_disks.append(disk)

    # Get the best region
    progress("Getting the best region")
    region, _ = cbg.find_plausible_intersection(empirical_disks, physical_limit_disks)

    if region.is_empty:
        print(f"NOTE: region is empty for {vpn[0]}")
        row.update({
            "center": "empty_region",
            "area_km2": None,
            "countries": "empty_region",
            "match": None,
            "duration_sec": f"{time.time() - start:.2f}"})
        return row

    center = region.centroid
    row["center"] = (center.y, center.x)  # get center (lat, long) 

    # Get country of center point
    center_point = gpd.GeoDataFrame(geometry=[center], crs="EPSG:4326")
    center_join = gpd.sjoin(center_point, basemap, how="left", predicate="within")
    row["center_country"] = center_join['iso_a2'].iloc[0] if not center_join.empty else ''

    # Get the area
    #   EPSG:3857 (Web Mercator): strongly distorts area, especially at high latitudes.
    #   EPSG:6933 (World Cylindrical Equal-Area) preserves area everywhere.
    progress("Getting the area")
    gdf = gpd.GeoDataFrame(geometry=[region], crs="EPSG:4326").to_crs("EPSG:6933")
    row["area_km2"] = gdf.area.iloc[0] / 1e6

    progress("Finding likely countreis...")
    # Find likely countries
    region_gdf = gpd.GeoDataFrame(geometry=[region], crs="EPSG:4326")
    joined = gpd.sjoin(region_gdf, basemap, how="inner", predicate="intersects")

    if not joined.empty:
        # Extract the likely country or countries
        isos = joined["iso_a2"].drop_duplicates().to_list()
        countries = joined["name_long"].drop_duplicates().to_list()
        row["countries"] = countries

        df = pd.read_csv(iso_fpath, encoding="ISO-8859-1")
        predicted = False

        for country in isos: 
            iso3 = df.loc[df["ISO_A2"] == country.upper(), "ISO_A3"]
            iso3_val = iso3.iloc[0] if not iso3.empty else None

            if country.upper() == vpn[1].upper() or iso3_val == vpn[1].upper():
                predicted = True
    
        if predicted and len(countries) == 1:
            row['match'] = "Yes"
        elif predicted:
            row['match'] = "Possibly"
        else:
            row['match'] = "No"
    else:
        row["countries"] = "None"
        row["match"] = "No"


    # Check if the disk is onland
    # on_land = not joined.empty
    # if not on_land:
    #     print("Not on land")

    row['duration_sec'] = f"{time.time() - start:.2f}"
    return row
 

def process_batch_parallel(basemap: gpd.GeoDataFrame, 
                           pings: dict, 
                           calibrations: dict, 
                           iso_fpath: str,
                           output_fpath: str, 
                           columns: list,
                           num_processes=4) -> None:
    """Run CBG++ across all VPNs
    """
    with open(output_fpath, "w") as cbg_file:
        writer = csv.DictWriter(cbg_file, fieldnames=columns)
        writer.writeheader() 

        if num_processes is None:
            num_processes = mp.cpu_count() - 1  # Leave one CPU for system processes
 
        process_func = partial(process_single_vpn, 
                               pings_dict=pings, calibrations=calibrations, 
                               basemap=basemap, iso_fpath=iso_fpath)
 
        with mp.Pool(processes=num_processes) as pool: 
            for result in pool.imap_unordered(process_func, pings.keys()):
                writer.writerow(result)
                progress(f"Completed processing VPN: {result['vpn_name']}")
 

def run_cbg(pings_dir: str, lm_fpath: str, cali_fpath: str, output_fpath: str,
            date: datetime, world_map: str, iso_file: str) -> None:

    start = time.time()  

    if date is None:
        date = _get_latest_date(pings_dir)

    date_path = f"{date.year}/{date.month}/{date.day}"
    ping_files = glob.glob(f"{pings_dir}/*/{date_path}/*/pings_*.csv")
    
    progress(f"Found {len(ping_files)} ping files")

    # Load necessary data   
    progress("Loading basemap and ping data")
    basemap = cbg.load_basemap(world_map)   
    dict_pings = data_frame(ping_files, lm_fpath)
            # (vp_ip, country_code): { (probe_id, lon, lat): rtt }
     

    # Retrive calibration
    calibrations = {} 
    with open(cali_fpath, "r") as f:   
        reader = csv.reader(f)
        for row in reader:
            prb_id = int(row[0])
            max_curve = float(row[1])
            calibrations[prb_id] = max_curve

    progress("Running CBG++ in batch mode")

    columns = ["vpn_name", 
                "center",           # center of the estimated region (lat, lon) 
                "area_km2",         # area of the plasuble region in square kilometers
                "countries",        # countries the region intersects 
                "center_country",   #
                "match",            # whether the predicted country matches what vpn claimed 
                "duration_sec"]     # total processing time in seconds

    if DEBUG:
        with open(output_fpath, "w") as cbg_file:
            writer = csv.DictWriter(cbg_file, fieldnames=columns) 
            writer.writeheader()
            for vpn in dict_pings.keys():
                result = process_single_vpn(vpn, dict_pings, calibrations, basemap, iso_file)
                writer.writerow(result)
    else:
        process_batch_parallel(basemap, dict_pings, calibrations, iso_file, output_fpath, columns)  
     
    end = time.time()
    progress(f"CBG completed in {(end - start) / 60} minutes")


if __name__ == "__main__": 
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--pings', type=str, required=True,
                    help='Directory containing collected ping CSV files')
    ap.add_argument('--lm-file', type=str, required=True,
                    help='File containing details for each landmark')
    ap.add_argument('--cali-file', type=str, required=True,
                    help='Per-probe calibration values CSV file')
    ap.add_argument('--output', type=str, required=True,
                    help='Output file path for CBG results')
    ap.add_argument('--date', type=str, default=None,
                    help='Date in YYYY-MM-DD format (default: today)')
    ap.add_argument('--world-map', type=str,
                    default='cbg/data/raw/maps/ne_50m_admin_0_countries.shp',
                    help='Shapefile for country boundaries')
    ap.add_argument('--iso-file', type=str,
                    default='cbg/data/raw/iso3166.csv',
                    help='Country code mapping')
    args = ap.parse_args() 
    
    if args.date:
        date = datetime.strptime(args.date, '%Y-%m-%d')
    else:
        date = datetime.now()

    run_cbg(args.pings, args.lm_file, args.cali_file, args.output, 
            args.world_map, args.iso_file)
