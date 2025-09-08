"""
Docker helper functions
"""

import concurrent.futures
import os
import queue
import time

import docker

from utilities.vpn_location_mapping import vpn_credentials, vpn_locations

container_queue = queue.Queue()

try:
    client = docker.from_env()
except docker.errors.DockerException:
    print("Docker is not running")
    exit(1)


def _start_single_container(
    is_vpn: bool,
    vpn_name: str | None = None,
    location: str | None = None,
    container: str = "iclab:exp",
) -> docker.models.containers.Container:
    if is_vpn:
        if vpn_name is None or location is None:
            raise ValueError(
                "vpn_name and location must be specified if is_vpn is True"
            )
        else:
            return client.containers.run(
                container,
                detach=True,
                cap_add=["NET_ADMIN"],
                environment=["VPN_NAME=" + vpn_name, "LOCATION=" + location],
                volumes={f"{os.getcwd()}/results": {"bind": "/results", "mode": "rw"}},
            )
    else:
        return client.containers.run(
            container,
            detach=True,
            cap_add=["NET_ADMIN"],
            volumes={f"{os.getcwd()}/results": {"bind": "/results", "mode": "rw"}},
        )


def _start_exp(
    exp_name: str,
    is_vpn: bool,
    vpn_name: bool | None = None,
    location: str | None = None,
    container: str = "iclab:exp",
) -> str:
    container = _start_single_container(is_vpn, vpn_name, location, container)

    # wait for container to start
    time.sleep(10)
    (_, output) = container.exec_run("/prep_docker.sh")

    if is_vpn:
        cmd = f"openvpn --config /vpns/{vpn_name}/{vpn_locations[vpn_name][location]} --daemon"
        if vpn_credentials[vpn_name]["type"] == "creds":
            cmd += f" --auth-user-pass /vpns/{vpn_name}/{vpn_credentials[vpn_name]['filename']}"
        else:
            cmd += f" --ca /vpn/{vpn_name}/{vpn_credentials[vpn_name]['filename']}"

        (_, output) = container.exec_run(cmd)
        # waiting for the vpn to connect
        time.sleep(20)

    # testing the IP address
    (_, output) = container.exec_run("curl ident.me")
    print(f"{output.decode('utf-8')}")

    (_, output) = container.exec_run(f"python3 experiments/{exp_name}.py")
    print(f"{output.decode('utf-8')}")
    # print(f"{is_vpn} - {vpn_name} - {location} - {output.decode("utf-8")}")
    container.stop()
    # container.remove()
    return f"Completed {is_vpn} - {vpn_name} - {location}"


def run_exp(max_containers: int, exp_name: str) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_containers) as executor:
        futures = []
        while not container_queue.empty():
            is_vpn, vpn_name, location, container = container_queue.get()
            futures.append(executor.submit(_start_exp, exp_name, is_vpn, vpn_name, location, container))

        for future in concurrent.futures.as_completed(futures):
            print(future.result())


def queue_container(
    is_vpn: bool,
    vpn_name: str | None = None,
    location: str | None = None,
    container: str = "iclab:exp",
):
    container_queue.put((is_vpn, vpn_name, location, container))


def show_queue():
    print(container_queue.queue)
