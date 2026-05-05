#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Calculate calibration for CBG++

import os 
import re
import csv 
import sys
import time

import argparse
from datetime import timedelta 

from calibration import CBGLinProg, discard_infeasible_measurements

# Type hints
#
from typing import ( 
    Any, 
    Optional
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

# Helper functions to extract lists when needed
def _get_distances(landmark: dict):
    return [m['distance'] for m in landmark['measurements']]

def _get_rtts(landmark: dict):
    return [m['rtt'] for m in landmark['measurements']]

def _get_dst_pids(landmark: dict):
    return [m['dst_pid'] for m in landmark['measurements']]

def _load_landmarks(lm_path: str) -> dict:
    """Load landmarks info
    """
    landmarks = {}
    with open(lm_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            addr = row["addr"]
            longitude = float(row["longitude"])
            latitude = float(row["latitude"])
            city = row["city"]
            country = row["country"]
            pid = int(row["pid"])
            continent = row["continent"]
            landmarks[pid] = {'longitude': longitude,
                              'latitude': latitude, 
                              'ip_v4': addr,
                              'city': city,
                              'country': country,
                              'continent': continent,
                              'measurements': []} # List of {dst_pid, rtt, distance} dicts
    return landmarks

def _load_mesh_rtts(mesh_fpath: str, landmarks: dict) -> None:
    """Load mesh topo measurements
    """  
    with open(mesh_fpath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader: 
            assert len(row) == 4 
            
            src_pid = int(row['src_pid'])
            dst_pid = int(row['dest_pid'])
            rtt = float(row['min_rtt'])
            distance = float(row['distance'])

            # rtts between anchors only
            if src_pid in landmarks and dst_pid in landmarks:
                landmarks[src_pid]['measurements'].append({
                    'dst_pid': dst_pid,
                    'rtt': rtt,
                    'distance': distance
                })
 

def _filter_out_unreliable_measurements(landmarks: dict) -> list:
    """Filter out landmarks with unreliable RTT measurements.

    Criteria for removal:
    - Infeasible RTTs (e.g., negative or zero values)
    - Too few measurements (if the number of valid RTTs is less than 80% of the total expected)
    - Suspiciously low average rtt (mean rtt<= 10ms)
    """
    unreliable_pids = []
    total_expected = len(landmarks) - 1 # Expected number of measurements per landmark

    for src_pid, src_info in landmarks.items():
        # discard infeasible RTTs
        src_info['measurements'] = discard_infeasible_measurements(src_pid, src_info['measurements'])

        # too few measurements
        if len(src_info['measurements']) <= (total_expected * 0.8):
            unreliable_pids.append(src_pid) 
            continue

        # suspiciously low average rtt
        rtts = [m['rtt'] for m in src_info['measurements']]
        if len(rtts) > 0 and sum(rtts)/len(rtts) <= 10:
            unreliable_pids.append(src_pid) 
            continue
     
    progress(f"Removed {len(unreliable_pids)} unreliable landmarks out of {len(landmarks)}")

    return list(set(unreliable_pids))

def _select_minimum_symmetric_rtts(landmarks: dict):
    """ Choose minimum RTT from both directions (A->B and B->A)
        because min RTT is the closest approximation to the true network distances 
        with minimal queuing delays or congestion
    """
    rtt_by_pair = {}
    for src_pid in landmarks:
        for measurement in landmarks[src_pid]['measurements']:
            dst_pid = measurement['dst_pid']
            rtt = measurement['rtt']
            pair_key = tuple(sorted([src_pid, dst_pid]))
            if pair_key not in rtt_by_pair or rtt < rtt_by_pair[pair_key]:
                rtt_by_pair[pair_key] = rtt
 
    for pair, min_rtt in rtt_by_pair.items():
        pid1, pid2 = pair  

        # Update RTTs in both directions with minimum value
        for measurement in landmarks[pid1]['measurements']:
            if measurement['dst_pid'] == pid2:
                measurement['rtt'] = min_rtt
                
        for measurement in landmarks[pid2]['measurements']:
            if measurement['dst_pid'] == pid1:
                measurement['rtt'] = min_rtt 
 

def _store_reliable_landmarks(lm_path: str, pids_to_discard: list, pids_to_keep: list):
    """ Store a list of unfiltered and filtered landmarks  
    """
    output_fpath = lm_path.replace(".csv", "_unreliable.csv") 
    with open(lm_path, 'r', newline='') as infile, open(output_fpath, 'w', newline='') as outfile:
        reader = csv.reader(infile)
        writer = csv.writer(outfile)
        
        header = next(reader, None)
        if header:
            writer.writerow(header) 
            
        for row in reader:
            if row and int(row[2]) in pids_to_discard:
                writer.writerow(row)
    
    progress(f"Done writing.. {output_fpath}")
 
    output_fpath = lm_path.replace(".csv", "_reliable.csv")  
    with open(lm_path, 'r', newline='') as infile, open(output_fpath, 'w', newline='') as outfile:
        reader = csv.reader(infile)
        writer = csv.writer(outfile)
        
        header = next(reader, None)
        if header:
            writer.writerow(header) 
            
        for row in reader:
            if row and int(row[2]) in pids_to_keep:
                writer.writerow(row)
    
    progress(f"Done writing.. {output_fpath}")

def _calculate_calibrations(landmarks: dict, lm_path: str):
    """Get calibrations based off of obs data
    """ 
    # 1. Filter out unreliable measurements 
    unreliable_pids = _filter_out_unreliable_measurements(landmarks)   
    
    unreliable_set = set(unreliable_pids)
    for src_pid, src_info in landmarks.items():
        if src_pid not in unreliable_set:     # clean for reliable landmarks
            src_info['measurements'] = [m for m in src_info['measurements'] 
                                       if m['dst_pid'] not in unreliable_set]
    for pid in unreliable_pids:
        landmarks.pop(pid, None) 
  
    _store_reliable_landmarks(lm_path, unreliable_pids, list(landmarks.keys()))
    
    # 2. Merge rtts bidirectional from only good landmarks
    #   For calibration, not picking the minimum might actually be better
    #   BC, it might capture the outbound network characteristics better of the landmarks
    #   However, for CBG++, using minimum RTT is generally better
    #   Because CBG++ prioritizes coverage over precision; missing a target (undercoverage)
    #   is worse than having slightly larger disks.
    _select_minimum_symmetric_rtts(landmarks)
    
    # 3. calculate calibrations.
    #   per-landmark calibration line;
    #   each landmark is treated as a source
    calibrations = {}
    skipped = []
    for src_lm in landmarks:
        distances = _get_distances(landmarks[src_lm])  # x
        rtts = _get_rtts(landmarks[src_lm])  # y
        try:
            calibrations[src_lm] = CBGLinProg(distances, rtts)
        except Exception as e:
            skipped.append((src_lm, str(e)))
            continue

    progress(f"Calibration computed for {len(calibrations)} landmarks; "
             f"skipped {len(skipped)} (LP infeasible or data error)")
    for pid, reason in skipped:
        progress(f"  SKIP pid={pid}: {reason}")

    return calibrations


def main() -> None:
    """ 
    """
    ap = argparse.ArgumentParser(description=__doc__)  
    ap.add_argument('--landmarks-file', type=str, default='cbg/data/retrieved/landmarks.csv',
                        help='File containing details for each landmark') 
    ap.add_argument('--mesh-file', type=str,
                        help='File containing the collected mesh anchoring data') 
    ap.add_argument('--cali-file', type=str, default='cbg/data/calibration',
                        help='Folder to store the output files')  

    args = ap.parse_args()  
    
    # Load the data 
    landmarks = _load_landmarks(args.landmarks_file)  
    _load_mesh_rtts(args.mesh_file, landmarks)

    # run calibration
    calibrations = _calculate_calibrations(landmarks, args.landmarks_file)

    # write the result to an output file 

    with open(args.cali_file, "w") as file:
        writer = csv.writer(file, lineterminator="\n")
        for key in calibrations.keys():
            writer.writerow([key, calibrations[key]._curve["max"]])
        
    progress(f"Calibrations saved to: {args.cali_file}")

if __name__ == "__main__":
    main()