#!/bin/bash
#
# Assemble Radar.app from the SPM executable.
#
# A menu bar app has to be a bundle, not a bare binary: LSUIElement is what
# keeps it out of the Dock, and UNUserNotificationCenter refuses to talk to a
# process without a bundle identifier. Ad-hoc signing is enough for both.
#
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${1:-release}"
APP="build/Radar.app"

swift build -c "$CONFIG"
BIN="$(swift build -c "$CONFIG" --show-bin-path)/Radar"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Radar"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Радар</string>
  <key>CFBundleDisplayName</key><string>Радар</string>
  <key>CFBundleExecutable</key><string>Radar</string>
  <key>CFBundleIdentifier</key><string>agency.blackbloom.radar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <!-- No Dock icon, no menu bar of its own: the app is the status item. -->
  <key>LSUIElement</key><true/>
  <key>NSSupportsAutomaticTermination</key><false/>
  <key>NSSupportsSuddenTermination</key><false/>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "подписать не удалось: уведомления могут не работать"

echo "$APP"
