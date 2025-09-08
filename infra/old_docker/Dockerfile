FROM python:3.12.0-bullseye

# This is for getting openvpn installed. This will be moved
# to a separate container later

# This is to ensure that the apt-get commands do not ask for user input
ENV DEBIAN_FRONTEND=noninteractive

# Install the required packages
RUN apt update && apt install -y openvpn unzip curl tcpdump

# Create the required directories for openvpn
RUN mkdir -p /dev/net
RUN mkdir /run/sshd

COPY infra/prep_docker.sh /
RUN chmod +x /prep_docker.sh

COPY vpns /vpns


# Install poetry and the dependencies
RUN mkdir /app
COPY pyproject.toml /app
WORKDIR /app
RUN pip install poetry
RUN poetry config virtualenvs.create false && poetry install --no-interaction

# We are doing this later to cache as many docker layers as possible
COPY .   /app/


# This is to ensure that the container does not exit

CMD ["tail", "-f","/dev/null"]
