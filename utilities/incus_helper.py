import os
import subprocess
import time

debug = os.environ.get("DEBUG", "false").lower() == "true"

def print_debug(message: str):
    if debug:
        print(message)

def check_for_image(instance_name: str) -> bool:
    output = subprocess.run(["incus", "image", "list"], capture_output=True)
    return instance_name in output.stdout.decode()


def start_instance(instance_name: str, image_name: str = "iclab-alpine", optional_args: dict = None) -> None:
    if optional_args is None:
        optional_args = {}

    is_running = check_for_instance(instance_name)

    opt_args = {
        "limits.memory": "4GiB",
        "security.secureboot": "false",
    }

    opt_args.update(optional_args)

    match image_name:
        case "iclab-ubuntu":
            network_adapter = "enp5s0"
        case "iclab-alpine":
            network_adapter = "eth0"
        case _:
            network_adapter = "eth0"

    if is_running:
        print(f"Instance {instance_name} is already running")
        return
    command = f"incus launch {image_name} --vm {instance_name}"
    for key, value in opt_args.items():
        command += f" -c {key}={value}"

    print_debug(f"command: {command}")
    output = subprocess.run(command, shell=True, capture_output=True)
    print_debug(f"output: {output.stdout.decode()}")
    print_debug(f"stderr: {output.stderr.decode()}")

    while True:
        output = subprocess.run(["incus", "list"], capture_output=True)
        for line in output.stdout.decode().split("\n"):
            if instance_name in line:
                if network_adapter in line:
                    # This sleep is a hack to ensure the instance is fully started
                    # Sometimes the instance is not fully started when the command returns
                    # and the metadata command fails with an error "VM agent isn't currently running"
                    time.sleep(5)
                    return
        time.sleep(3)


def check_for_instance(instance_name: str) -> bool:
    output = subprocess.run(["incus", "list"], capture_output=True)
    return instance_name in output.stdout.decode()


def stop_and_remove_instance(instance_name: str) -> None:
    output = subprocess.run(["incus", "stop", instance_name], capture_output=True)
    if output.returncode != 0:
        print(f"Error stopping instance {instance_name}")

    output = subprocess.run(["incus", "delete", instance_name], capture_output=True)
    if output.returncode != 0:
        print(f"Error deleting instance {instance_name}")

def execute_command(command: str, instance_name: str, env: dict = {}, login_override: bool = False): # noqa: B006
    base_env = {"SSLKEYLOGFILE": "/sslkeylog"}
    
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, *rest = line.split('=', 1)
                if rest:  # Only process if there was a '=' found
                    base_env[key.strip()] = rest[0].strip()

    base_env.update(env)

    login_flag = "--login"
    if login_override:
        # this super hacky workaround is because ubuntu has `sh` symlink to `dash`
        # and dash doesn't support `--login`.

        # WHY.
        login_flag = "-l"

    process = f'incus exec {instance_name} -- sh {login_flag} -c "{command}"'
    output = subprocess.run(process, capture_output=True, shell=True, env=base_env)
    return output

def execute_command_in_background(command: str, instance_name: str, env: dict = {}): # noqa: B006
    # base_env = {"SSLKEYLOGFILE": "/sslkeylog"}
    # base_env.update(env)
    process = f'incus exec {instance_name} -- sh -c "{command}"'
    output = subprocess.Popen(process, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output

def pull_file(instance_name: str, file_path: str, local_path: str):
    process = f'incus file pull {instance_name}{file_path} {local_path}'
    print_debug(f"{instance_name}: Pulling file {file_path} to {local_path}")
    print_debug(f"{instance_name}: Command: {process}")
    output = subprocess.run(process, capture_output=True, shell=True)
    if output.returncode != 0:
        print(f"Error pulling file {file_path} from instance {instance_name}")
        print(output.stderr.decode())

def check_incus_installed():
    try:
        subprocess.run(["incus", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

if __name__ == "__main__":
    output = check_incus_installed()
    print(f"Incus installed: {output}")
    # import time
    # # print(check_for_instance("iclab-alpine"))
    # start_instance("vm3")
    # time.sleep(5)
    # out1 = execute_command_in_background("tcpdump -n -w /tmp/capture.pcap -U -s 0 -i eth0", "vm3")
    # out = execute_command("curl https://www.google.com", "vm3")
    # print(out.stdout.decode())
    # print(out.stderr.decode())
    # execute_command("killall tcpdump", "vm3") 
    # print(out1.stdout.read().decode())
    # print(out1.stderr.read().decode())
    # time.sleep(1)
    # pull_file("vm3", "/sslkeylog", "sslkeylog")
    # pull_file("vm3", "/tmp/capture.pcap", "capture.pcap")
    # execute_command("rm /tmp/capture.pcap", "vm3")
    # execute_command("rm /sslkeylog", "vm3")
    # # stop_and_remove_instance("vm3")
