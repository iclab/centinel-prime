import socket

HOSTNAME = "whois.cymru.com"
PORT = 43

def get_cymru_info(ip) -> tuple[str, str, str]:
    """
    Returns a tuple of (asn, country_code, organization)
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOSTNAME, PORT))
        s.sendall(f" -v {ip}\n".encode())
        data = s.recv(1024)
        data = data.decode().split("\n")
        ip_data = data[1].split("|")
        return (ip_data[0].strip(), ip_data[3].strip(), ip_data[6].strip())


if __name__ == "__main__":
    print(get_cymru_info("108.39.252.194"))
