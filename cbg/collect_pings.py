#!/usr/bin/python3
# -*- coding: utf-8 -*-

import re
import sys
import csv
import time
import json
import argparse
import subprocess
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# Type hints
#
from typing import ( 
    Any, 
    Optional, 
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


def ping_ip(ip, prb_id, count=10):
    cmd = ["ping", "-c", str(count), ip]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=5)      
        rtts = re.findall(r'time[=<](\d+(?:\.\d+)?)', output)
        rtt = min(map(float, rtts)) if rtts else None 
        # progress(f"DEBUG ping_ip: ip={ip}, prb_id={prb_id}, raw_rtt={rtt}, rtts_found={rtts[:3] if rtts else []}")
        return (prb_id, rtt)
    except subprocess.CalledProcessError as e:
        progress(f"Ping to {ip} failed. Output: {e.output[:100] if e.output else 'None'}")
        if "0 received" in e.output or "Destination Host Unreachable" in e.output:
            progress(f"Ping to {ip} unreachable.")
            return (prb_id, None)
    except subprocess.TimeoutExpired:
        progress(f"Ping to {ip} timed out.")
 
    return (prb_id, None)


def write_to_file(ping_results, path_to_output: str) -> None:
    with open(path_to_output, "w") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(['prb_id', 'ip', 'min_rtt']) 
        for prb_id, values in ping_results.items():
            writer.writerow([prb_id, values['ip'], values['rtt']]) 


def send_ping(ips: list, path_to_output, peer_rtt: float, count=3, max_workers=50) -> None:
    ping_results = {}
    progress("Start sending pings to all IPs.")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ip = {executor.submit(ping_ip, ip, prb_id, count): (ip, prb_id) for ip, prb_id in ips}
        for future in as_completed(future_to_ip):
            ip, prb_id = future_to_ip[future]
            prb_id, rtt = future.result()  
            if rtt is not None: 
                est_rtt = max(0.0, rtt - peer_rtt) if peer_rtt is not None else None
                ping_results[prb_id] = {'ip': ip, "rtt": est_rtt}

    progress("Finished sending pings")
 
    write_to_file(ping_results, path_to_output) 
     

def get_ips(args):
    """Get IPs from stdin if available, otherwise from args"""
    if not sys.stdin.isatty(): 
        ips_input = sys.stdin.read().strip()
        if ips_input: 
            try:
                pairs = json.loads(ips_input)
                return [(ip, probe_id) for ip, probe_id in pairs]
            except json.JSONDecodeError:
                print("ERROR: Invalid JSON format for IP/probe_id pairs")
                return []
    else: 
        return args.ips or []

def main():  
    ap = argparse.ArgumentParser(description=__doc__) 
    ap.add_argument("--ips", nargs='+', help="List of IP addresses")
    ap.add_argument("--path-to-output", type=str, 
                        default="pings.csv", help="File path to store the output JSON")
    ap.add_argument("--peer-rtt", type=float, required=True,
                        help="RTT to VPN tunnel peer (in ms), measured in caller")
    args = ap.parse_args()   

    ips = get_ips(args) 

    if not ips:
        print("ERROR: No IPs provided via stdin or --ips argument")
        sys.exit(1)
 
    send_ping(ips, args.path_to_output, args.peer_rtt)     


if __name__ == "__main__":
    main()