import os
import re
import sys
import json
import glob
import time
import socket 
import subprocess
from datetime import timezone, datetime

from cbg.run_cbg import run_cbg
from cbg.add_continent import add_continent_column
from cbg.compare_cbg_metadata import run_comparison

from utilities import vpn_location_mapping
import threading
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from utilities.http_utils import get_curr_ip, get_instance_ip
from utilities.experiment_helpers import (
    ExperimentSpec,
    build_instance_name,
    build_metadata_command,
    build_results_dir,
    cleanup_instance,
    connect_and_verify_vpn,
    generate_metadata_hash,
    pull_results_files, 
    print_debug,
)
from utilities.incus_helper import (
    execute_command,
    start_instance,
    execute_command_in_background, 
    pull_file,
    push_file,
)



exp_queue: List[ExperimentSpec] = []


def queue_exp(url_file_name: str, is_vpn: bool, vpn_provider: str, location: str, config: str) -> None:
    exp_queue.append(
        ExperimentSpec(
            url_file_name=url_file_name,
            is_vpn=is_vpn,
            vpn_provider=vpn_provider,
            location=location,
            config=config,
        )
    )


def show_queue() -> None:
    print(exp_queue)


def _process_url(url: str, instance_name: str, is_vpn: bool) -> str:
    """Process a single URL download in a thread-safe manner."""
    try:
        output = execute_command(f"bash /experiments/parquet-downloader-nftables.sh --url {url.strip()} --vpn {is_vpn}", instance_name)
        print_debug(output)
        return f"Successfully processed URL: {url.strip()}"
    except Exception as e:
        return f"Failed to process URL {url.strip()}: {str(e)}"

def _start_exp(spec: ExperimentSpec, max_url_threads: int = 3) -> str:
    instance_name = build_instance_name(spec)
    try:
        curr_ip = get_curr_ip()
        start_instance(instance_name)

        vpn_ip = None
        if spec.is_vpn:
            vpn_ip = connect_and_verify_vpn(spec, instance_name, curr_ip)

        metadata_cmd = build_metadata_command(spec, curr_ip, vpn_ip)
        metadata_hash = generate_metadata_hash(instance_name, metadata_cmd)

        execute_command("python3 /experiments/parquet-writer/write_metadata.py", instance_name)

        # Read all URLs from the file
        with open(f"lists/{spec.url_file_name}") as f:
            urls = [url.strip() for url in f]
        
        # Process URLs in parallel using ThreadPoolExecutor
        print(f"Processing {len(urls)} URLs with {max_url_threads} threads for {instance_name}")
        with ThreadPoolExecutor(max_workers=max_url_threads) as executor:
            # Submit all URL processing tasks
            future_to_url = {
                executor.submit(_process_url, url, instance_name, spec.is_vpn): url 
                for url in urls
            }
            
            # Create progress bar for this VM's URL processing
            with tqdm(total=len(urls), desc=f"{instance_name} URLs", unit="url") as pbar:
                # Collect results as they complete
                for future in as_completed(future_to_url):
                    url = future_to_url[future]
                    try:
                        result = future.result()
                        print_debug(f"{instance_name}: {result}")
                    except Exception as e:
                        error_msg = f"{instance_name}: Exception occurred while processing {url}: {str(e)}"
                        print_debug(error_msg)
                        print(f"ERROR: {error_msg}")
                        # Stop the entire process on any error
                        executor.shutdown(wait=False)
                        raise Exception(f"URL processing failed for {url}: {str(e)}")
                    finally:
                        pbar.update(1)
        
        # Merge all thread-specific parquet files into final output
        print(f"Merging parquet files for {instance_name}")
        execute_command("python3 /experiments/parquet-writer/merge_parquet.py", instance_name)
        
        results_dir = build_results_dir(spec, metadata_hash)
        pull_results_files(instance_name, results_dir)

        cleanup_instance(instance_name)
        return f"Completed {instance_name}"
    except Exception as e:
        try:
            cleanup_instance(instance_name)
        except Exception:
            print(f"Failed to cleanup instance {instance_name}")
            print(e)
        raise

def run_exp(max_instances: int, max_url_threads: int = 20) -> None:
    print(f"Starting experiments with {max_instances} max instances and {max_url_threads} URL threads per instance")
    print(f"Queue length: {len(exp_queue)}")
    
    # Use BoundedSemaphore to strictly enforce the limit
    semaphore = threading.BoundedSemaphore(value=max_instances)
    completed = 0
    total = len(exp_queue)
    threads: List[threading.Thread] = []
    results: List[str] = []
    
    def wrapped_start_exp(spec: ExperimentSpec):
        nonlocal completed
        semaphore.acquire()
        try:
            result = _start_exp(spec, max_url_threads)
            completed += 1
            print(f"Experiment completed ({completed}/{total}): {result}")
            results.append(result)
        except Exception as e:
            completed += 1
            print(f"Experiment failed ({completed}/{total}) with error: {e}")
            results.append(f"Failed: {str(e)}")
        finally:
            semaphore.release()
    
    # Start all threads
    for exp in exp_queue:
        print(f"Creating thread for experiment: {exp}")
        thread = threading.Thread(target=wrapped_start_exp, args=(exp,))
        threads.append(thread)

    for thread in threads:
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    print(f"All experiments completed. Results:\n {'\n'.join(results)}")

def run_baseline(num_threads: int = 5, max_url_threads: int = 5):
    queue_exp(
        url_file_name="citizenlab_global_100.txt",
        is_vpn=False,
        vpn_provider=None,
        location=None,
        config=None,
    )

    # for location in list(vpn_location_mapping.vpn_locations["ipvanish"].keys()):
    #     for config in vpn_location_mapping.vpn_locations["ipvanish"][location]:
    #         queue_exp(
    #             url_file_name="citizenlab_global.txt",
    #             is_vpn=True,
    #             vpn_provider="ipvanish",
    #             location=location,
    #             config=config,
    #         )

    run_exp(num_threads, max_url_threads)



# ============================================================
# vpn validator
# ============================================================

def _run_or_exit(cmd: list[str], *, stdout=None) -> None:
    print_debug(cmd)
    try:
        subprocess.run(cmd, check=True, stdout=stdout)
    except subprocess.CalledProcessError as e:
        print(f"Command failed ({e.returncode}): {' '.join(cmd)}")
        sys.exit(1)

def _is_data_recent(fpath: str, days=7):
    """Check if the collected_on date in JSON is within the specified days"""
    with open(fpath, 'r') as f:
        metadata = json.load(f)
     
    collected_on_str = metadata.get('collected_on')
    if not collected_on_str:
        return False
         
    collected_on = datetime.fromisoformat(collected_on_str)
    now = datetime.now(timezone.utc) 
    time_diff = (now - collected_on).total_seconds() 
    return time_diff < days * 86400
    
def _retrieve_anchor_info(base_dir: str, raw_data_dir: str, output_dir: str, fname: str, meta_file_path: str) -> None:
     
    # Retrieve RIPE anchor information from RIPE ATLAS
    lm_file_path = os.path.join(output_dir, fname)
    with open(lm_file_path, "w") as f:
        _run_or_exit(["python3", f"{base_dir}/retrieve_landmarks.py"], stdout=f)
        
    # Add continent (for selection algorithm)
    result = add_continent_column(lm_file_path)
    print_debug(result)

    # Analyze the locations of the RIPE landmarks relative to a world political map
    _run_or_exit([
    "python3", f"{base_dir}/analyze_landmarks.py",
    f"{output_dir}/landmarks-and-distances.csv.gz",
    f"{output_dir}/landmarks.csv",
    f"{raw_data_dir}/ne_10m_admin_0_map_units.zip",
    f"{raw_data_dir}/merges.yml",
    f"{raw_data_dir}/iso3166.csv",
    ])

    # This file is checked by _is_data_recent() to skip re-fetching if data is < 7 days old.
    metadata = {"collected_on": datetime.now(timezone.utc).isoformat()}
    with open(meta_file_path, "w") as jsonfile:
        json.dump(metadata, jsonfile, indent=2)

def _collect_anchor_mesh(base_dir: str, mesh_path: str,
                         retrieved_data_path: str, landmarks_file: str) -> None: 
    # Retrive mesh anchoring data
    _run_or_exit([
    "python3", f"{base_dir}/retrieve_topo.py",
    "--parallel", "4",
    "--days-back", "1",
    "--landmark-file", os.path.join(retrieved_data_path, landmarks_file),
    "--output-folder", mesh_path,
    ])
     
def _calculate_calibration(base_dir: str, mesh_file_path: str, cali_file_path: str, 
                                 retrieved_data_path: str, landmarks_file: str) -> None:  
    # Calculate calibration
    _run_or_exit([
    "python3", f"{base_dir}/get_calibrations.py",
    "--mesh-file", mesh_file_path,
    "--cali-file", cali_file_path,
    "--landmarks-file", f"{retrieved_data_path}/{landmarks_file}",
    ])  
 
def _get_anchor_ips(landmarks_fpath: str) -> list:  
    """Get anchor ips to collect pings"""
    anchors_ips = []
    with open(landmarks_fpath, "r") as file:
        next(file)  # Skip header
        for line in file:
            parts = line.strip().split(",")
            ip_address = parts[0] 
            probe_id = parts[2] # pid not aid
            anchors_ips.append((ip_address, probe_id)) 
    return anchors_ips 

def _parse_vpn_hostname(vpn_path: str, instance_name: str):
    """Parse VPN hostname from config file to measure VM <-> VPN node RTT"""
    try:
        output = subprocess.check_output(["incus", "exec", instance_name, "--", "cat", vpn_path], 
                                         text=True,
                                         stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None, None
    
    pattern = re.compile(r'^\s*remote\s+([^\s#]+)(?:\s+(\d+))?', re.IGNORECASE)
    for line in output.splitlines():
        m = pattern.match(line.strip())
        if m:
            host = m.group(1)
            port = int(m.group(2)) if m.group(2) else None
            return host, port
    return None, None
    
def _ping_single_host(ip: str, instance_name: str, count=10) -> float: 
    """Measure VM <-> VPN node RTT"""
    cmd = f"ping -c {count} -i 0.2 {ip}"
    try:
        result = execute_command(cmd, instance_name, login_override=True)
        output = result.stdout.decode() if hasattr(result.stdout, 'decode') else str(result.stdout)
        rtts = re.findall(r'time[=<](\d+(?:\.\d+)?)', output)
        rtt = min(map(float, rtts)) if rtts else None
        return rtt

    except Exception as e:
        print_debug(f"Ping to {ip} failed with error: {e}")
        return None 

def _get_tunnel_peer_ip(instance_name: str):
    """Get the tunnel peer IP for precise latency measurement."""
    try:
        out = subprocess.check_output(["incus", "exec", instance_name, "--", "ip", "route"], text=True)
        for line in out.splitlines():
            if re.match(r'^(0\.0\.0\.0/1|128\.0\.0\.0/1)', line):
                m = re.search(r'via\s+(\d+\.\d+\.\d+\.\d+)', line)
                if m:
                    return m.group(1)
    except subprocess.CalledProcessError:
        pass
    return None

def _collect_pings_from_vpn(instance_name: str, anchors_ips: list, pings_dir: str,
                            year: int, month: int, day: int) -> None:
     
    # Iterate through all VPNs and connect to VPN
    for vpn_provider in list(vpn_location_mapping.vpn_locations.keys()):
        for vpn_location in list(vpn_location_mapping.vpn_locations[vpn_provider].keys()):
            for vpn_config in vpn_location_mapping.vpn_locations[vpn_provider][vpn_location]:
                # Check if pings already collected for this VPN config    
                cur_pings_base_dir = f"{pings_dir}/{vpn_provider}/{year}/{month}/{day}"   
                result_pattern = f"{cur_pings_base_dir}/*/pings_{vpn_provider}_{vpn_location}_{vpn_config}.csv"
                existing_results = glob.glob(result_pattern)
                if existing_results:
                    print_debug(f"Skipping {vpn_provider}/{vpn_location}/{vpn_config}: pings already collected")
                    continue

                # Measure VM <-> VPN node using public IP
                vpn_hostname, vpn_port = _parse_vpn_hostname(f"/vpns/{vpn_provider}/{vpn_config}", instance_name)               
                if vpn_hostname is None:
                    print_debug(f"Skipping {vpn_provider} - {vpn_config}: Could not parse hostname from config")
                    continue
                try: 
                    addrs = socket.getaddrinfo(vpn_hostname, vpn_port, family=socket.AF_INET, type=socket.SOCK_STREAM)               
                except socket.gaierror as e:
                    print_debug(f"Skipping {vpn_provider} - {vpn_config}: DNS resolution failed for {vpn_hostname}: {e}")
                    continue

                vpn_public_ip = list({ip[0] for *_, ip in addrs}) 
                rtt_incus_to_vpn_public = _ping_single_host(vpn_public_ip[0], instance_name)
                print_debug(f"VPN public IP: {vpn_public_ip}, RTT from incus to VPN public IP: {rtt_incus_to_vpn_public}")

                # Get local IP
                curr_ip = get_curr_ip()  
                print_debug(f"get local IP: {curr_ip}")

                # Setup for metadata
                CLOUDFLARE_TOKEN = os.environ.get("CLOUDFLARE_TOKEN")  
                url_file_name = "citizenlab_global.txt"
                METADATA_COMMAND = f"python3 /experiments/metadata/metadata.py --ip {curr_ip} --url_list /lists/{url_file_name} --cloudflare_token {CLOUDFLARE_TOKEN}"

                cmd = f"openvpn --config /vpns/{vpn_provider}/{vpn_config} --daemon"
                if vpn_location_mapping.vpn_credentials[vpn_provider]["type"] == "creds":
                    cmd += f" --auth-user-pass /vpns/{vpn_provider}/{vpn_location_mapping.vpn_credentials[vpn_provider]['filename']}"
                if "ca_filename" in vpn_location_mapping.vpn_credentials[vpn_provider]:
                    cmd += f" --ca /vpns/{vpn_provider}/{vpn_location_mapping.vpn_credentials[vpn_provider]['ca_filename']}"
                print_debug(cmd)

                # setup tun0 for vpn  
                execute_command("sh /vpns/vpn_prep.sh", instance_name, login_override=True)
                print_debug(f"run {cmd}")
                vpn_output = execute_command_in_background(cmd, instance_name)
                time.sleep(10) 
 
                # Check if the vpn connection is up 
                vpn_connected = False
                vpn_ip = get_instance_ip(instance_name)
                if vpn_ip and vpn_ip != curr_ip:  # Found a different IP - VPN is working
                    print(f"{instance_name}: VPN IP: {vpn_ip}, Current IP: {curr_ip}, match: {vpn_ip == curr_ip}")
                    METADATA_COMMAND += f" --is_vpn --vpn_provider {vpn_provider} --vpn_config {vpn_config} --vpn_ip {vpn_ip}"
                    vpn_connected = True              
 
                if not vpn_connected:
                    print("--------------------------------")
                    print("VPN connection failed")
                    print(f"VPN Name: {vpn_provider}") 
                    print(f"VPN Location: {vpn_location}")
                    print(vpn_output.stdout.read().decode())
                    print(vpn_output.stderr.read().decode())
                    print("--------------------------------")

                    # raise Exception("VPN connection failed")
                    # Clean up any potential OpenVPN processes before moving on
                    execute_command("pkill openvpn", instance_name, login_override=True)
                    time.sleep(3)

                    result = execute_command("pgrep openvpn", instance_name, login_override=True)
                    if result.returncode == 0:  
                        print("WARNING: OpenVPN still running after pkill")
      
                    continue
                
                # Measure VM <-> VPN node using VPN peer IP
                vpn_tunnel_peer_ip = _get_tunnel_peer_ip(instance_name)
                rtt_incus_to_peer = _ping_single_host(vpn_tunnel_peer_ip, instance_name) 
                print_debug(f"VPN tunnel peer IP: {vpn_tunnel_peer_ip}, RTT from incus to VPN tunnel peer IP: {rtt_incus_to_peer}")
                if rtt_incus_to_peer is None and rtt_incus_to_vpn_public is None:
                    print("WARNING: no RTT between VM and VPN node")
                elif rtt_incus_to_peer is None:
                    rtt_incus_to_peer = rtt_incus_to_vpn_public

                # Run metadata validation 
                print_debug("Meta started")
                print_debug(f"{instance_name}: metadata command: {METADATA_COMMAND}")
                result = execute_command(METADATA_COMMAND, instance_name, login_override=True) 
                metadata_hash = result.stdout.decode().strip()
                print_debug(f"{instance_name}: Hash: {metadata_hash}")
                print_debug("Meta done")

                match len(metadata_hash):
                    case 64:
                        pass
                    case 0:
                        raise Exception("Hash is empty, metadata command failed.")
                    case _:
                        raise Exception("Hash is not 64 characters. This should never happen. Heat death of the universe imminent.")

                # Send pings to landmarks
                print_debug(f"rtt_incus_to_peer: {rtt_incus_to_peer}")
                ping_file = f"pings_{vpn_provider}_{vpn_location}_{vpn_config}.csv" 
                pairs_json = json.dumps(anchors_ips)  
                result = subprocess.run([
                    "incus", "exec", instance_name, "--",
                    "sh", "-l", "-c",
                    f"python3 /cbg/collect_pings.py --path-to-output /{ping_file} --peer-rtt {rtt_incus_to_peer}"
                ], 
                input=pairs_json, 
                text=True 
                )

                if result.returncode != 0: 
                    print(f"Command failed with return code: {result.returncode}") 
                else: 
                    print("Success!") 
                 
                os.makedirs(f"{cur_pings_base_dir}/{metadata_hash}", exist_ok=True)     
                pull_file(instance_name, "/metadata.json", f"{cur_pings_base_dir}/{metadata_hash}/metadata.json")
                pull_file(instance_name, f"/{ping_file}", f"{cur_pings_base_dir}/{metadata_hash}/{ping_file}")

                execute_command(f"rm /metadata.json", instance_name, login_override=True) 
                execute_command(f"rm /{ping_file}", instance_name, login_override=True) 
            
                # Kill VPN and retrieve metadata 
                execute_command("pkill openvpn", instance_name, login_override=True)
                time.sleep(5)

                # Check if still running and force kill if necessary
                result = execute_command("pgrep openvpn", instance_name, login_override=True)
                if result.returncode == 0:  
                    print("WARNING: OpenVPN still running after pkill")
                    execute_command("pkill -9 openvpn", instance_name, login_override=True)
                    time.sleep(2)

                    # Final check
                    result = execute_command("pgrep openvpn", instance_name, login_override=True)
                    if result.returncode == 0:
                        print("ERROR: OpenVPN still running after SIGKILL")
                    else:
                        print("OpenVPN successfully terminated with SIGKILL")

                # Kill VPN and retrieve metadata 
                execute_command("pkill openvpn", instance_name, login_override=True)
                time.sleep(3)

                result = execute_command("pgrep openvpn", instance_name, login_override=True)
                if result.returncode == 0:  
                    print("WARNING: OpenVPN still running after pkill")


def run_vpn_validator() -> None: 
     

    instance_name = "vpn-validator"
    image_name = "iclab-ubuntu"

    # Start instance
    start_instance(instance_name, image_name, optional_args={
        "limits.memory": "4GiB",
        "security.secureboot": "false",
        "limits.cpu": "4", # upper: 30
    })  

    # Prepare all necessary data for active geolocation:
    #   Retrieve and update anchor information and pings between anchors,
    #   and complete calibration calculations before running VPNs
    base_dir = "cbg"
    raw_data_path = f"{base_dir}/data/raw"
    retrieved_data_path = f"{base_dir}/data/retrieved" 
    os.makedirs(retrieved_data_path, exist_ok=True)

    landmarks_file = "landmarks.csv"
    mesh_path = f"{base_dir}/data/mesh"
    cali_path = f"{base_dir}/data/calibration"
    os.makedirs(mesh_path, exist_ok=True)
    os.makedirs(cali_path, exist_ok=True)

    # Check if file exists and was modified within the last 7 days  
    meta_file = f"{landmarks_file.split('.')[0]}-metadata.json"
    meta_file_path = os.path.join(retrieved_data_path, meta_file)
    is_recent = os.path.exists(meta_file_path) and _is_data_recent(meta_file_path) 
  
    if not is_recent:
        _retrieve_anchor_info(base_dir, raw_data_path, retrieved_data_path, landmarks_file, meta_file_path)
            
        # Retrive mesh anchoring data
        _collect_anchor_mesh(base_dir, mesh_path, retrieved_data_path, landmarks_file)
    
    latest_mesh_file = max(
    (f for f in os.listdir(mesh_path) if f.startswith('mesh_') and f.endswith('.csv')),
    key=lambda x: datetime.strptime(x[5:-4], '%Y-%m-%d'))

    match = re.search(r'mesh_(\d{4}-\d{2}-\d{2})\.csv', latest_mesh_file)
    date_str = match.group(1) if match else "unknown"
    cali_file = f"calibrations_{date_str}.csv"
 
    if not is_recent: 
        # Calculate calibration 
        _calculate_calibration(base_dir, os.path.join(mesh_path, latest_mesh_file), 
                               os.path.join(cali_path, cali_file), retrieved_data_path, landmarks_file)

    reliable_landmarks_file = landmarks_file.replace(".csv", "_reliable.csv")
    reliable_landmarks_fpath = os.path.join(retrieved_data_path, reliable_landmarks_file)

    # Push reliable landmarks and latest calibration files to incus 
    execute_command(f"mkdir -p /{raw_data_path}", instance_name, login_override=True) 
    execute_command(f"mkdir -p /{retrieved_data_path}", instance_name, login_override=True)
    push_file(instance_name, f"{reliable_landmarks_fpath}", 
                           f"/{retrieved_data_path}/{reliable_landmarks_file}")
    
    execute_command(f"mkdir -p /{cali_path}", instance_name, login_override=True) 
    push_file(instance_name, f"{os.path.join(cali_path, cali_file)}", 
                           f"/{cali_path}/{cali_file}") 
    
    # Collect pings from VPN to all anchors
    anchors_ips = _get_anchor_ips(reliable_landmarks_fpath) 
    print_debug(f"landmarks: {len(anchors_ips)} anchors.")

    # Push collect_pings file
    push_file(instance_name, f"{base_dir}/collect_pings.py", f"/{base_dir}/collect_pings.py")
    timestamp = datetime.now()
    year, month, day = timestamp.year, timestamp.month, timestamp.day  
    pings_base_dir = f'{base_dir}/data/pings'
    _collect_pings_from_vpn(instance_name, anchors_ips, pings_base_dir, year, month, day)
  
    # Runs the CBG geolocation analysis, ensuring the required files exist.
    cali_files = [f for f in os.listdir(cali_path) 
              if f.startswith('calibrations_') and f.endswith('.csv') and len(f) == 27]
    latest_cali_file = max(cali_files, key=lambda x: datetime.strptime(x[13:-4], '%Y-%m-%d'))
        
    result_path = f"{base_dir}/data/results"
    os.makedirs(result_path, exist_ok=True) 
    output_fpath = f"{base_dir}/data/results/cbg_results_{year}-{month:02d}-{day:02d}.csv"
    
    run_cbg(pings_base_dir, 
            reliable_landmarks_fpath, 
            os.path.join(cali_path, latest_cali_file), 
            output_fpath,
            datetime(year, month, day),
            f"{raw_data_path}/maps/ne_50m_admin_0_countries.shp", 
            f"{raw_data_path}/iso3166.csv")
 
    # Run comparison between CBG results and metadata sources
    comparison_output_fpath = f"{base_dir}/data/comparison/comparison_{year}-{month:02d}-{day:02d}.csv"
    run_comparison(
        cbg_results_fpath=output_fpath,
        pings_dir=pings_base_dir,
        date=datetime(year, month, day),
        output_fpath=comparison_output_fpath)

    # Stop the instance 
    cleanup_instance(instance_name)