#!/bin/bash
set -e

REPO_DIR="/home/pi/pipanel"

echo "Updating pipanel from git..."
cd "$REPO_DIR"

git config --global --add safe.directory "$REPO_DIR"

if timeout 30 git fetch origin; then
    git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
    echo "Update complete. Current commit: $(git rev-parse --short HEAD)"
else
    echo "Git fetch failed or timed out — starting with current version: $(git rev-parse --short HEAD)"
fi
