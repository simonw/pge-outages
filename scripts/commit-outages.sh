#!/usr/bin/env bash
set -euo pipefail

mv outages-new.json outages.json
git config user.name "Automated"
git config user.email "actions@users.noreply.github.com"
git add outages.json
if git diff --cached --quiet -- outages.json; then
    echo "No outage changes"
    exit 0
fi
git commit -F message.txt
git pull --rebase
git push
