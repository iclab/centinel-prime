FROM ubuntu:22.04

RUN DEBIAN_FRONTEND="noninteractive" apt update && apt install -y \
    build-essential \
    libncurses5-dev \
    libssl-dev \
    unzip \
    libnghttp2-dev

# This is to cache the curl compilation
RUN mkdir -p /app/curl
COPY curl/curl-8.5.0.zip /app/curl/curl-8.5.0.zip

WORKDIR /app/curl
RUN unzip curl-8.5.0.zip
WORKDIR /app/curl/curl-8.5.0
RUN ./configure --with-openssl --prefix=/app/curl && make && make install

RUN DEBIAN_FRONTEND='noninteractive' apt update && apt install -y \
    tcpdump \
    openvpn

COPY . /app

WORKDIR /app
RUN make
ENV LD_LIBRARY_PATH=/app/curl/lib

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT [ "/app/entrypoint.sh" ]
# CMD [ "lists/short.txt" ]
