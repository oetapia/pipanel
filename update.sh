#!/bin/bash
set -e

REPO_DIR="/home/pi/pipanel"

echo "Updating pipanel from git..."
cd "$REPO_DIR"

git fetch origin
git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)

echo "Update complete. Current commit: $(git rev-parse --short HEAD)"
