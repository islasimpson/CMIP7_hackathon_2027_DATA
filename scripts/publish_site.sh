#!/bin/bash
# Commit and push catalog/ and docs/ if they changed (refreshes the GitHub Pages site).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git add catalog docs
if git diff --cached --quiet; then
    echo "catalog/site unchanged; nothing to push"
    exit 0
fi
git commit -q -m "Update catalog and site ($(date +%Y-%m-%d))"
git pull -q --rebase --autostash origin main
git push -q origin main
echo "pushed $(git log --oneline -1)"
