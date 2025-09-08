#!/bin/bash

# Check if docker is installed
if ! [ -x "$(command -v docker)" ]; then
  echo "Docker is not installed. Please install docker and try again."
  exit 1
fi

# Check if docker daemon is running
if ! docker info > /dev/null 2>&1; then
  echo "Docker daemon is not running. Please start docker daemon and try again."
  exit 1
fi

# build the image
docker build -t iclab:exp . -f infra/Dockerfile

