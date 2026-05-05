#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Retrieve measurements of anchor topology from RIPE.
# It typically takes around 20 minutes to complete this process.

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
import multiprocessing as mp
import csv

from ripe.atlas.cousteau import (
  AtlasRequest,
  AtlasResultsRequest,
  MeasurementRequest
)

from geo_utils import calculate_geodesic

# Type hints
#
from typing import ( 
    Any, 
    Optional 
)

#
# Utility
#

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

def _store_mesh_data(data: dict, distances: dict, fpath: str):
    """
    """  
    with open(fpath, 'w') as file:
        csv_writer = csv.writer(file, lineterminator='\n')
        csv_writer.writerow(["src_pid", "dest_pid", "min_rtt", "distance"])

        for src_pid, dst_pid in data.keys():
            key = tuple(sorted([src_pid, dst_pid]))
            dist = distances[key]
            csv_writer.writerow([src_pid, dst_pid, data[(src_pid, dst_pid)], dist])

def _load_landmarks(lm_path: str) -> dict:
    """Load landmarks info
    """
    landmarks = {}
    with open(lm_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader: 
            longitude = float(row["longitude"])
            latitude = float(row["latitude"]) 
            pid = int(row["pid"]) 
            landmarks[pid] = {'longitude': longitude,
                              'latitude': latitude,  
                              'distance': None}  
    return landmarks

def _calculate_distances(anchors: list, landmarks: dict) -> None:
    """Calculate distances between landmarks
    """
    progress("Start calculating distances... ")
     
    dists = {}
    for i in range(len(anchors)):
        for j in range(i, len(anchors)):
            src_pid = anchors[i]
            dst_pid = anchors[j]
            dist_km = calculate_geodesic(
                    landmarks[src_pid]["latitude"], 
                    landmarks[src_pid]["longitude"],
                    landmarks[dst_pid]["latitude"], 
                    landmarks[dst_pid]["longitude"])
            key = tuple(sorted([src_pid, dst_pid]))
            dists[key] = dist_km 

    return dists 

def retrieve_meas(msm: dict, current_time: datetime, anchors: list) -> dict:
    """per measurement
    """ 
    min_rtts = {}
    url_path = "/api/v2/anchors/?search=" + msm['target']
    request = AtlasRequest(**{"url_path": url_path})
    (is_success, response) = request.get()   
    if not is_success:
        progress(f"fail to get anchor info: {url_path}")
        return None
    if 'results' not in response or not response['results']:
        progress(f"no results: {url_path}")
        return None 
    if response['results'][0]['type'] != "Anchor":
        progress(f"not an anchor: {url_path}")
        return None
    dst_pid = int(response['results'][0]['probe'])
    if dst_pid not in anchors:
        progress(f"skipping anchor {dst_pid}: inactive")
        return None
    dst_ipv4 = response['results'][0]['ip_v4'] 

    start_time = current_time - timedelta(hours=1)
    filters2 = {"msm_id": msm['id'],
                "start": start_time,
                "stop": current_time}

    progress(f"fetching request for msm_id: {msm['id']}, "
             f"from {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')} "
             f"to {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    
    # all pings to the destination anchor at the given time
    is_success, results = AtlasResultsRequest(**filters2).create()
    if not is_success:
        progress(f"fail to get measurements on: {msm['id']}")
        return None

    seen_ip_mismatches = set()  
    for result in results:
        if isinstance(result, dict):  
            result_src_ipv4 = result['src_addr']  # typically dynamic; not a stable identifier
            result_dst_ipv4 = result['dst_addr']  # should match the above dest_ipv4
            pair_key = (dst_ipv4, result_dst_ipv4) 
            if dst_ipv4 != result_dst_ipv4 and pair_key not in seen_ip_mismatches: 
                progress(f"Unique mismatch found: {dst_ipv4} != {result_dst_ipv4}") 
                with open("ip-not-matching.txt", "a") as f: 
                    f.write(f"dst_ipv4: {dst_ipv4}, result_dst_ipv4: {result_dst_ipv4}\n") 
                seen_ip_mismatches.add(pair_key)
            
            src_pid = int(result['prb_id'])
            if src_pid not in anchors:
                progress(f"skipping source anchor {src_pid}: inactive; but dest anchor {dst_pid} is valid")
                continue

            rtt = result['min']
 
            if rtt > 0:
                min_rtts[(src_pid, dst_pid)] = min(rtt, min_rtts.get((src_pid, dst_pid), rtt)) 

    return min_rtts

def main() -> None:
    """
    Retrieve measurements for each day going back the specified number of days.
    Save the data in a folder with files named accordingly.
    """
    ap = argparse.ArgumentParser(description=__doc__) 
    ap.add_argument('--parallel', type=int, default=1,
                        help='Number of parallel processes to use')
    ap.add_argument('--days-back', type=int, default=1,
                        help='How many days back to process')
    ap.add_argument('--start-offset', type=int, default=0,
                        help='Start from this day offset (skip earlier offsets)')
    ap.add_argument('--landmark-file', type=str,
                        help='Path to landmark file')
    ap.add_argument('--output-folder', type=str, default='cbg/data/retrieved/mesh',
                        help='Folder to store the output files') 

    args = ap.parse_args()  

    # Calculate distances between landmark files
    landmarks = _load_landmarks(args.landmark_file) 
    anchors = list(landmarks.keys())
    distances = _calculate_distances(anchors, landmarks)
 
    for day_offset in range(args.start_offset, args.days_back): # To track calibration changes, we can collect data over the past 30 days.
        current_time = datetime.now(timezone.utc) - timedelta(days=day_offset)
        
        progress(f"day offset: {day_offset}, current time: "
                 f"{current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        filters = {"tags": ["anchoring", "mesh"],
                   "type": "ping",
                   "af": 4,
                   "status": 2}
        measurements = MeasurementRequest(**filters)

        args_iter = ((meas, current_time, anchors) for meas in measurements)
        final = {}
        with mp.Pool(processes=args.parallel) as pool:
            for result in pool.starmap(retrieve_meas, args_iter):
                if result:
                    final.update(result) 

        os.makedirs(args.output_folder, exist_ok=True)   
        date_str = current_time.strftime("%Y-%m-%d")
        filename = f'mesh_{date_str}.csv'
        fpath = os.path.join(args.output_folder, filename)
        _store_mesh_data(final, distances, fpath)

        progress(f"data for {date_str} saved in {os.path.join(args.output_folder, filename)}")
 
if __name__ == "__main__":
    main()


 