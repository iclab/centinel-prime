import requests
from utilities.incus_helper import execute_command

ip_resolvers = [
    "https://ident.me",
    "https://icanhazip.com",
    "https://ipinfo.io/ip",
    "https://api.ipify.org",
    "https://ipecho.net/plain",
]

def get_curr_ip():
    for url in ip_resolvers:
        try:
            request = requests.get(url)
            return request.text.strip()
        except Exception:
            pass

def get_instance_ip(instance_name: str) -> str | None:
    for url in ip_resolvers:
        result = execute_command(f"curl -s {url}", instance_name, login_override=True)
        ip = result.stdout.decode().strip()
        if ip:
            return ip
    return None