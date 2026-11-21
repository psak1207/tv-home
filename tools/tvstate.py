#!/usr/bin/env python3
"""Back up the TV's state before debloating, and put it back exactly as it was.

    tvstate.py backup                              save backup/<timestamp>/ plus a screenshot
    tvstate.py restore [BACKUP] [--dry-run] [--remove-launcher]
                                                   undo everything since BACKUP (default: the oldest)
    tvstate.py write-restore [BACKUP]              write BACKUP/restore.sh for the TV's current state
    tvstate.py setting NS KEY VALUE                change a setting and remember it for restore

Restore only touches what differs from the backup: packages we disabled are re-enabled, packages
that were already disabled stay disabled, the original home screen comes back, settings changed
with `tvstate.py setting` get their old values. New apps you installed yourself are left alone.
"""
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKUPS = os.path.join(ROOT, "backup")
LAUNCHER_PKG = "com.mihir.tvhome"
HOME_ARGS = "-a android.intent.action.MAIN -c android.intent.category.HOME"
PROPS = ("ro.product.manufacturer", "ro.product.brand", "ro.product.model", "ro.product.device",
         "ro.build.version.release", "ro.build.version.sdk", "ro.build.fingerprint")


# ---------------------------------------------------------------- pure helpers (unit-tested)

def parse_packages(out):
    return sorted({l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")})


def parse_settings(out):
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v
    return d


def parse_component(out):
    """Last line of `cmd package resolve-activity --brief`, e.g. com.x/.Home."""
    lines = [l.strip() for l in out.splitlines() if "/" in l and " " not in l.strip()]
    return lines[-1] if lines else ""


def quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def plan_restore(before, now, settings_touched=(), remove_launcher=False):
    """Shell commands (one per call) that take the TV from `now` back to `before`."""
    cmds = []
    b_inst, n_inst = set(before["installed"]), set(now["installed"])
    b_dis, n_dis = set(before["disabled"]), set(now["disabled"])
    for p in sorted(n_dis - b_dis):
        if p in b_inst:
            cmds.append(f"pm enable {p}")
    for p in sorted(b_dis - n_dis):
        if p in n_inst:
            cmds.append(f"pm disable-user --user 0 {p}")
    for ns, key in sorted(set(map(tuple, settings_touched))):
        old = before["settings"].get(ns, {}).get(key)
        cur = now["settings"].get(ns, {}).get(key)
        if old != cur:
            cmds.append(f"settings put {ns} {key} {quote(old)}" if old is not None
                        else f"settings delete {ns} {key}")
    home = before.get("home", "")
    if home and not home.startswith("android/") and home != now.get("home"):
        cmds.append(f"cmd package set-home-activity --user 0 {home}")
    if remove_launcher and LAUNCHER_PKG in n_inst and LAUNCHER_PKG not in b_inst:
        cmds.append(f"pm uninstall {LAUNCHER_PKG}")
    if cmds:
        cmds.append("input keyevent KEYCODE_HOME")
    return cmds


# ---------------------------------------------------------------- device side

def _tv():
    spec = importlib.util.spec_from_file_location("tv", os.path.join(HERE, "tv.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sh(dev, cmd):
    return dev.shell(cmd, read_timeout_s=120)


def capture(dev, raw_dir=None):
    raw = {
        "getprop": sh(dev, "getprop"),
        "packages-installed": sh(dev, "pm list packages"),
        "packages-all-incl-uninstalled": sh(dev, "pm list packages -u"),
        "packages-disabled": sh(dev, "pm list packages -d"),
        "packages-system": sh(dev, "pm list packages -s"),
        "packages-third-party": sh(dev, "pm list packages -3"),
        "packages-with-paths": sh(dev, "pm list packages -f"),
        "home": sh(dev, f"cmd package resolve-activity --brief {HOME_ARGS}"),
        "home-candidates": sh(dev, f"cmd package query-activities --brief {HOME_ARGS}"),
    }
    for ns in ("global", "secure", "system"):
        raw[f"settings-{ns}"] = sh(dev, f"settings list {ns}")
    if raw_dir:
        for name, text in raw.items():
            with open(os.path.join(raw_dir, name + ".txt"), "w") as f:
                f.write(text)
    installed = parse_packages(raw["packages-installed"])
    return {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "props": {p: sh(dev, f"getprop {p}").strip() for p in PROPS},
        "installed": installed,
        "disabled": parse_packages(raw["packages-disabled"]),
        "uninstalled_for_user": sorted(set(parse_packages(raw["packages-all-incl-uninstalled"])) - set(installed)),
        "system": parse_packages(raw["packages-system"]),
        "third_party": parse_packages(raw["packages-third-party"]),
        "home": parse_component(raw["home"]),
        "home_candidates": [l.strip() for l in raw["home-candidates"].splitlines() if "/" in l],
        "settings": {ns: parse_settings(raw[f"settings-{ns}"]) for ns in ("global", "secure", "system")},
    }


def screenshot(dev, path):
    dev.shell("screencap -p /sdcard/_shot.png")
    dev.pull("/sdcard/_shot.png", path)
    dev.shell("rm /sdcard/_shot.png")


def backups():
    if not os.path.isdir(BACKUPS):
        return []
    return sorted(d for d in os.listdir(BACKUPS) if os.path.isfile(os.path.join(BACKUPS, d, "state.json")))


def pick_backup(arg):
    if arg:
        return arg if os.path.isabs(arg) or os.path.isdir(arg) else os.path.join(BACKUPS, arg)
    found = backups()
    if not found:
        sys.exit("No backup yet. Run: tools/venv/bin/python -I tools/tvstate.py backup")
    return os.path.join(BACKUPS, found[0])  # the oldest one is the TV as it came


def load(bdir):
    with open(os.path.join(bdir, "state.json")) as f:
        before = json.load(f)
    touched = []
    jpath = os.path.join(bdir, "settings-journal.jsonl")
    if os.path.exists(jpath):
        with open(jpath) as f:
            touched = [(e["ns"], e["key"]) for e in map(json.loads, f) if e]
    return before, touched


def cmd_backup(tv):
    bdir = os.path.join(BACKUPS, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(bdir, "raw"))
    dev = tv.connect()
    state = capture(dev, os.path.join(bdir, "raw"))
    with open(os.path.join(bdir, "state.json"), "w") as f:
        json.dump(state, f, indent=1)
    screenshot(dev, os.path.join(bdir, "before.png"))
    dev.close()
    print(f"backup saved to {os.path.relpath(bdir, ROOT)}: {len(state['installed'])} packages "
          f"({len(state['disabled'])} already disabled), home = {state['home']}")


def cmd_restore(tv, bdir, dry_run, remove_launcher):
    before, touched = load(bdir)
    dev = tv.connect()
    cmds = plan_restore(before, capture(dev), touched, remove_launcher)
    if not cmds:
        print("Nothing to restore: the TV already matches the backup.")
    for c in cmds:
        print(f"$ {c}")
        if not dry_run:
            out = sh(dev, c).strip()
            if out:
                print(f"  {out}")
    if cmds and not dry_run:
        left = plan_restore(before, capture(dev), touched, remove_launcher)
        left = [c for c in left if not c.startswith("input ")]
        print("Restored: TV matches the backup." if not left else "Still different:\n  " + "\n  ".join(left))
        screenshot(dev, os.path.join(bdir, "after-restore.png"))
        print(f"screenshot: {os.path.relpath(os.path.join(bdir, 'after-restore.png'), ROOT)}")
    dev.close()


def cmd_write_restore(tv, bdir):
    before, touched = load(bdir)
    dev = tv.connect()
    cmds = plan_restore(before, capture(dev), touched, remove_launcher=True)
    dev.close()
    path = os.path.join(bdir, "restore.sh")
    body = "\n".join(cmds)
    with open(path, "w") as f:
        f.write(f"""#!/usr/bin/env bash
# Puts the TV back the way it was in backup {os.path.basename(bdir)} (written {datetime.date.today()}).
# Re-enables exactly what the debloat disabled, brings back the original home screen and removes the
# new launcher. Run from anywhere:  {os.path.relpath(path, ROOT)}
# Without Python: run each line below as `adb shell <line>`.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${{PY:-tools/venv/bin/python}}"
"$PY" -I tools/tv.py --batch <<'EOF'
{body}
EOF
echo "Restored. Press Home on the remote."
""")
    os.chmod(path, 0o755)
    print(f"wrote {os.path.relpath(path, ROOT)} ({len(cmds)} commands)")


def cmd_setting(tv, ns, key, value):
    found = backups()
    if not found:
        sys.exit("Back up first: tools/venv/bin/python -I tools/tvstate.py backup")
    dev = tv.connect()
    old = sh(dev, f"settings get {ns} {key}").strip()
    sh(dev, f"settings put {ns} {key} {quote(value)}")
    dev.close()
    with open(os.path.join(BACKUPS, found[0], "settings-journal.jsonl"), "a") as f:
        f.write(json.dumps({"ns": ns, "key": key, "old": old, "new": value}) + "\n")
    print(f"{ns} {key}: {old} -> {value}")


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return
    flags = {x for x in a if x.startswith("--")}
    args = [x for x in a if not x.startswith("--")]
    if args[0] == "backup":
        cmd_backup(_tv())
    elif args[0] == "restore":
        cmd_restore(_tv(), pick_backup(args[1] if len(args) > 1 else None),
                    "--dry-run" in flags, "--remove-launcher" in flags)
    elif args[0] == "write-restore":
        cmd_write_restore(_tv(), pick_backup(args[1] if len(args) > 1 else None))
    elif args[0] == "setting" and len(args) == 4:
        cmd_setting(_tv(), *args[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
