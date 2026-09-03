#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h}"
label="com.kaoyan.wrongbook.due"
launch_agents_dir="$HOME/Library/LaunchAgents"
plist_path="$launch_agents_dir/$label.plist"
db_path="$project_dir/library.sqlite"
python_bin="$(command -v python3)"
user_id="$(id -u)"

mkdir -p "$launch_agents_dir"

cat > "$plist_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python_bin</string>
    <string>$project_dir/notify_due.py</string>
    <string>--db</string>
    <string>$db_path</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
</dict>
</plist>
PLIST

if command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$user_id" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$user_id" "$plist_path"
  echo "已安装并加载 $plist_path"
else
  echo "已生成 $plist_path（当前系统没有 launchctl，跳过加载）"
fi
