import requests

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
