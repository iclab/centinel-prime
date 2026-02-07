from utilities import vpn_location_mapping
import threading
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from utilities.http_utils import get_curr_ip
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
