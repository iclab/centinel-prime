import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from utilities import incus_helper, vpn_location_mapping
from utilities.http_utils import ip_resolvers


debug = os.environ.get("DEBUG", "false").lower() == "true"


def print_debug(message: str) -> None:
    if debug:
        print(message)


@dataclass(frozen=True)
class ExperimentSpec:
    url_file_name: str
    is_vpn: bool
    vpn_provider: Optional[str]
    location: Optional[str]
    config: Optional[str]


def build_instance_name(spec: ExperimentSpec) -> str:
    suffix = (
        vpn_location_mapping.vpn_locations[spec.vpn_provider][spec.location].index(spec.config)
        if spec.location
        else "Baseline"
    )
    return f"iclab-exp-{spec.is_vpn}-{spec.vpn_provider}-{spec.location}-{suffix}"


def start_instance_and_tcpdump(instance_name: str) -> None:
    incus_helper.start_instance(instance_name)
    incus_helper.execute_command_in_background(
        "tcpdump -n -w /tmp/capture.pcap -U -s 0 -i any", instance_name
    )


def connect_and_verify_vpn(spec: ExperimentSpec, instance_name: str, curr_ip: str) -> str:
    cmd = f"openvpn --config /vpns/{spec.vpn_provider}/{spec.config} --daemon"
    if vpn_location_mapping.vpn_credentials[spec.vpn_provider]["type"] == "creds":
        cmd += (
            f" --auth-user-pass /vpns/{spec.vpn_provider}/"
            f"{vpn_location_mapping.vpn_credentials[spec.vpn_provider]['filename']}"
        )
    if "ca_filename" in vpn_location_mapping.vpn_credentials[spec.vpn_provider]:
        cmd += (
            f" --ca /vpns/{spec.vpn_provider}/"
            f"{vpn_location_mapping.vpn_credentials[spec.vpn_provider]['ca_filename']}"
        )

    incus_helper.execute_command("sh /vpns/vpn_prep.sh", instance_name)
    vpn_output = incus_helper.execute_command_in_background(cmd, instance_name)
    time.sleep(15)

    for resolver in ip_resolvers:
        result = incus_helper.execute_command(f"curl {resolver}", instance_name)
        vpn_ip = result.stdout.decode().strip()
        print_debug(
            f"{instance_name}: VPN IP: {vpn_ip}, Current IP: {curr_ip}, match: {vpn_ip == curr_ip}"
        )
        if vpn_ip and vpn_ip != curr_ip:
            return vpn_ip

    print("--------------------------------")
    print("VPN connection failed")
    print(f"VPN Name: {spec.vpn_provider}")
    print(f"VPN Location: {spec.location}")
    print(vpn_output.stdout.decode())
    print(vpn_output.stderr.decode())
    print("--------------------------------")
    raise Exception("VPN connection failed")


def build_metadata_command(spec: ExperimentSpec, curr_ip: str, vpn_ip: Optional[str]) -> str:
    cloudflare_token = os.environ.get("CLOUDFLARE_TOKEN")
    base_cmd = (
        "python3 /experiments/metadata/metadata.py "
        f"--ip {curr_ip} --url_list /lists/{spec.url_file_name} "
        f"--cloudflare_token {cloudflare_token}"
    )
    if spec.is_vpn and vpn_ip:
        base_cmd += (
            f" --is_vpn --vpn_provider {spec.vpn_provider} "
            f"--vpn_config {spec.config} --vpn_ip {vpn_ip}"
        )
    return base_cmd


def generate_metadata_hash(instance_name: str, metadata_command: str) -> str:
    print_debug(f"{instance_name}: metadata command: {metadata_command}")
    result = incus_helper.execute_command(metadata_command, instance_name)
    metadata_hash = result.stdout.decode().strip()
    print_debug(f"{instance_name}: Hash: {metadata_hash}")

    match len(metadata_hash):
        case 64:
            return metadata_hash
        case 0:
            raise Exception("Hash is empty, metadata command failed.")
        case _:
            raise Exception(
                "Hash is not 64 characters. This should never happen. Heat death of the universe imminent."
            )

def stop_tcpdump(instance_name: str) -> None:
    incus_helper.execute_command("killall tcpdump", instance_name)
    time.sleep(1)


def build_results_dir(spec: ExperimentSpec, metadata_hash: str) -> str:
    print_debug("Making results directory")
    timestamp = datetime.now()
    month = timestamp.month
    year = timestamp.year
    results_dir = f"results/{spec.vpn_provider}/{spec.location}/{year}/{month}/{metadata_hash}"
    os.makedirs(results_dir, exist_ok=True)
    return results_dir


def pull_results_files(instance_name: str, results_dir: str) -> None:
    incus_helper.pull_file(instance_name, "/output.parquet", f"{results_dir}/output.parquet")


def cleanup_instance(instance_name: str) -> None:
    print_debug(f"{instance_name}: Stopping instance")
    incus_helper.stop_and_remove_instance(instance_name)


