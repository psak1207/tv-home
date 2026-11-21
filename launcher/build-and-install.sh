#!/usr/bin/env bash
# Build the launcher APK with the bare SDK tools (no Gradle) and install it on the TV as the home screen.
#   launcher/build-and-install.sh            build + install
#   launcher/build-and-install.sh --build    build only
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export PATH="$JAVA_HOME/bin:$PATH"
SDK="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
# build-tools 34's d8 crashes on classes from JDK 21; point D8_BUILD_TOOLS at 35+ if you build with it.
D8="$SDK/build-tools/${D8_BUILD_TOOLS:-34.0.0}/d8"
# Android 11 by default; the code needs API 26 at least (getChangedPackages).
MIN_SDK="${MIN_SDK:-30}"
# The stock launcher to switch off: whatever was home in the oldest backup, else Google TV's.
backup_home=$(ls ../backup/*/state.json 2>/dev/null | head -1 | xargs -I{} python3 -I -c \
  'import json,sys; h=json.load(open(sys.argv[1]))["home"]; print("" if h.startswith("android/") else h.split("/")[0])' \
  {} 2>/dev/null || true)
STOCK_LAUNCHER="${STOCK_LAUNCHER:-${backup_home:-com.google.android.apps.tv.launcherx}}"
B=build

rm -rf "$B" && mkdir -p "$B/gen" "$B/classes" "$B/dex"
"$BT/aapt2" compile --dir res -o "$B/res.zip"
"$BT/aapt2" link -o "$B/base.apk" -I "$JAR" --manifest AndroidManifest.xml --java "$B/gen" \
  -A assets --min-sdk-version "$MIN_SDK" --target-sdk-version 30 "$B/res.zip"
javac -nowarn -Xlint:-options --release 11 -classpath "$JAR" -d "$B/classes" \
  $(find src "$B/gen" -name '*.java')
"$D8" --min-api "$MIN_SDK" --lib "$JAR" --output "$B/dex" $(find "$B/classes" -name '*.class')
cp "$B/base.apk" "$B/unsigned.apk"
(cd "$B/dex" && zip -q -u ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 "$B/unsigned.apk" "$B/aligned.apk"
[ -f debug.keystore ] || keytool -genkeypair -keystore debug.keystore -storepass android -keypass android \
  -alias tvhome -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=tvhome" >/dev/null 2>&1
"$BT/apksigner" sign --ks debug.keystore --ks-pass pass:android --out "$B/tvhome.apk" "$B/aligned.apk"
echo "built $B/tvhome.apk ($(wc -c < "$B/tvhome.apk") bytes)"
[ "${1:-}" = "--build" ] && exit 0

ls ../backup/*/state.json >/dev/null 2>&1 || { echo "Back up first: tools/venv/bin/python -I tools/tvstate.py backup"; exit 1; }
echo "Stock launcher to disable: $STOCK_LAUNCHER"
PY=../tools/venv/bin/python
"$PY" -I ../tools/tv.py --push-b64 "$B/tvhome.apk" /data/local/tmp/tvhome.apk
printf '%s\n' \
  "pm install -r /data/local/tmp/tvhome.apk" \
  "rm -f /data/local/tmp/tvhome.apk" \
  "pm grant com.mihir.tvhome android.permission.ACCESS_FINE_LOCATION" \
  "cmd package set-home-activity --user 0 com.mihir.tvhome/.MainActivity" \
  "pm disable-user --user 0 $STOCK_LAUNCHER" \
  "pm disable-user --user 0 com.google.android.tungsten.setupwraith" \
  "cmd package set-home-activity --user 0 com.mihir.tvhome/.MainActivity" \
  "am force-stop com.android.tv.settings" \
  "input keyevent KEYCODE_HOME" \
  | "$PY" -I ../tools/tv.py --batch
echo "Installed. Press Home on the remote."
