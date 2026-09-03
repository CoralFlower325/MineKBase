#!/bin/zsh
set -euo pipefail

label="com.kaoyan.wrongbook.due"
plist_path="$HOME/Library/LaunchAgents/$label.plist"
user_id="$(id -u)"

if command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
fi

if [[ -f "$plist_path" ]]; then
  rm -f "$plist_path"
  echo "已卸载 $label"
else
  echo "未找到 $label"
fi
