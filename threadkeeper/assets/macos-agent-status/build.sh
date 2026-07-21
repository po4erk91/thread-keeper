#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

app_name="ThreadKeeperAgentStatus"
build_dir="$PWD/build"
app_dir="$build_dir/$app_name.app"
bin_dir="$app_dir/Contents/MacOS"

rm -rf "$app_dir"
mkdir -p "$bin_dir"
cp Info.plist "$app_dir/Contents/Info.plist"

arch="$(uname -m)"
target="${arch}-apple-macos13.0"

swiftc \
  -O \
  -parse-as-library \
  -target "$target" \
  -framework SwiftUI \
  -framework AppKit \
  -framework UserNotifications \
  ThreadKeeperAgentStatus.swift \
  -o "$bin_dir/$app_name"

# UNUserNotificationCenter only delivers from a bundle with a stable code
# signature. Ad-hoc sign (no cert, no entitlements) is enough to give the app a
# durable identity so macOS registers it in Notification settings and shows
# banners. Without this, notification requests are silently dropped.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$app_dir" >/dev/null 2>&1 || true
fi

echo "$app_dir"
