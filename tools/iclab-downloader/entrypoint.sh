#!/bin/bash

mkdir /dev/net
mknod /dev/net/tun c 10 200
chmod 600 /dev/net/tun

/bin/bash
