#!/bin/bash

# Sync rugpt to remote server
# Usage: ./sync.sh

SERVER="5090"
REMOTE_PATH="~/rugpt"

rsync -avz --progress \
  -e "ssh" \
  --exclude 'venv' \
  --exclude '.git' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '__pycache__' \
  ./ ${SERVER}:${REMOTE_PATH}/

echo "Sync complete!"
