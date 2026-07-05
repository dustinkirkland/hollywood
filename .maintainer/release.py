#!/usr/bin/env python3
"""
release.py — hollywood release pipeline

Usage:
    ./release.py [rc]               # smoke-test + build source packages (default)
    ./release.py [rc] --interactive # same, but prompt for confirmation at each step
    ./release.py final              # cut a full release: tag, GitHub release, upload
    ./release.py open-dev           # bump debian/changelog and open next cycle
    ./release.py salsa-ci           # local Docker gbp buildpackage dry-run

RC phases (run all in parallel after preflight):
    1   Pre-flight checks
    2   Determine versions
    3   Smoke test (Docker ubuntu:noble — build, install, verify)  ┐ parallel
    3b  Salsa CI dry-run  (Docker debian:sid — gbp buildpackage)   ┘
    4   PPA source builds (Docker ubuntu:noble, one container per series)
    5   Debian source build (Docker ubuntu:noble)

Final skips phase 3 (smoke already passed on RC commit):
    6   Git tag + GitHub release
    7   Sign and upload (GPG sign + dput — interactive)
    8   Salsa push (push tag + HEAD to salsa games-team)
    9   Chainguard reminder
"""

import argparse
import concurrent.futures
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOLLYWOOD_SRC = Path(__file__).resolve().parent.parent

# Launchpad PPA target — change if Hollywood gets its own PPA.
PPA_TARGET = "ppa:hollywood/ppa"

# Salsa remote name — add with:
#   git remote add salsa git@salsa.debian.org:games-team/hollywood.git
SALSA_REMOTE = "salsa"
SALSA_BRANCH = "master"
SALSA_UPSTREAM_BRANCH = "upstream"


# ── thread-local stdout (parallel output capture) ──────────────────────────

_tls = threading.local()


class _TLSStdout:
    def __init__(self, real):
        self._real = real

    def _buf(self):
        return getattr(_tls, "buf", None)

    def write(self, s):
        b = self._buf()
        (b if b is not None else self._real).write(s)

    def flush(self):
        if self._buf() is None:
            self._real.flush()

    def isatty(self):
        return False if self._buf() is not None else self._real.isatty()

    def fileno(self):
        return self._real.fileno()


sys.stdout = _TLSStdout(sys.stdout)


# ── helpers ────────────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, **kwargs):
    kw = dict(check=check, text=True, **kwargs)
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
    buf = getattr(_tls, "buf", None)
    if buf is not None:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        result = subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
        buf.write(result.stdout or "")
        if check and result.returncode != 0:
            short = (cmd[0] if isinstance(cmd, list) else str(cmd).split()[0])
            buf.write(f"\n[exit {result.returncode}] {short}\n")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout)
        return result
    return subprocess.run(cmd, shell=isinstance(cmd, str), **kw)


def run_parallel(labeled_fns, log_dir=None):
    if not labeled_fns:
        return
    if len(labeled_fns) == 1:
        labeled_fns[0][1]()
        return

    labels = [l for l, _ in labeled_fns]
    print(f"\n  ⟳ Launching in parallel: {', '.join(labels)}")
    outcomes = {}

    def _run_one(label, fn):
        buf = io.StringIO()
        _tls.buf = buf
        try:
            fn()
            outcomes[label] = (buf.getvalue(), None)
        except BaseException as exc:
            outcomes[label] = (buf.getvalue(), exc)
        finally:
            _tls.buf = None

    threads = [threading.Thread(target=_run_one, args=(l, f)) for l, f in labeled_fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    any_failed = False
    for label in labels:
        out, exc = outcomes.get(label, ("", RuntimeError("phase did not run")))
        marker = "✓" if exc is None else "✗"
        print(f"\n── {marker} {label} " + "─" * max(0, 55 - len(label)))
        for line in out.rstrip().splitlines():
            print(f"  {line}")
        if exc is not None:
            print(f"  ✗ {label}: {exc}")
            any_failed = True
            if log_dir:
                slug = label.replace(" ", "-")
                log_path = Path(log_dir) / f"{slug}.log"
                log_path.write_text(out + f"\n--- FAILED: {exc}\n")
                print(f"  (full log: {log_path})")
    if any_failed:
        die("One or more parallel phases failed (see output above).")


def die(msg):
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


_interactive = False


def confirm(prompt, skippable=False):
    if not _interactive:
        print(f"\n  (auto-proceeding: {prompt[:70]}{'…' if len(prompt) > 70 else ''})")
        return True
    opts = "[y/s/N]" if skippable else "[y/N]"
    ans = input(f"\n{prompt} {opts} ").strip().lower()
    if ans in ("y", "yes"):
        return True
    if skippable and ans in ("s", "skip"):
        print("  (skipped)")
        return False
    die("Aborted.")


def banner(msg):
    w = 62
    print(f"\n{'━' * w}\n  {msg}\n{'━' * w}")


def section(msg):
    print(f"\n── {msg} " + "─" * max(0, 58 - len(msg)))


# ── phase 1: pre-flight ────────────────────────────────────────────────────

def load_identity():
    section("Phase 1: Pre-flight checks")
    bashrc = Path("~/.bashrc").expanduser().read_text()

    def extract(key):
        m = re.search(rf"export {key}=['\"]?([^'\"#\n]+)['\"]?", bashrc)
        return m.group(1).strip() if m else ""

    identity = {
        "DEBEMAIL":    extract("DEBEMAIL"),
        "DEBFULLNAME": extract("DEBFULLNAME"),
        "GPGKEY":      extract("GPGKEY"),
    }
    for k, v in identity.items():
        print(f"  {k}={v}")

    if not all(identity.values()):
        die(
            "Missing identity in ~/.bashrc. Add:\n"
            "  export DEBFULLNAME='Dustin Kirkland'\n"
            "  export DEBEMAIL='kirkland@ubuntu.com'\n"
            "  export GPGKEY='<your GPG key fingerprint>'"
        )
    return identity


def check_tools(mode):
    required = ["dput", "debsign", "git", "docker", "gh"]
    if mode == "final":
        required.append("gbp")
    missing = [t for t in required if not shutil.which(t)]
    if missing:
        die(
            f"Missing tools: {' '.join(missing)}\n"
            "  sudo apt install devscripts dput git-buildpackage gh"
        )
    print(f"  Tools OK: {' '.join(required)}")



def check_clean():
    r = run(["git", "-C", str(HOLLYWOOD_SRC), "status", "--porcelain"], capture=True)
    if r.stdout.strip():
        die(
            "Working tree has uncommitted changes:\n"
            + r.stdout
            + "\n  Commit or stash before releasing."
        )
    print("  Working tree clean.")


def check_salsa_sync(mode):
    r = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "remote", "get-url", SALSA_REMOTE],
        capture=True, check=False,
    )
    if r.returncode != 0:
        return  # no salsa remote

    run(["git", "-C", str(HOLLYWOOD_SRC), "fetch", SALSA_REMOTE], check=False)

    salsa_ref = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "rev-parse",
         f"{SALSA_REMOTE}/{SALSA_BRANCH}"],
        capture=True, check=False,
    )
    if salsa_ref.returncode != 0:
        return  # salsa branch not yet created

    merge_base = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "merge-base",
         "HEAD", f"{SALSA_REMOTE}/{SALSA_BRANCH}"],
        capture=True, check=False,
    )
    if (merge_base.returncode == 0 and
            merge_base.stdout.strip() != salsa_ref.stdout.strip()):
        msg = (
            f"salsa/{SALSA_BRANCH} has commits not in local master.\n"
            f"  Merge first: git fetch salsa && git merge salsa/{SALSA_BRANCH}\n"
            f"  Then re-run."
        )
        if mode == "final":
            die(msg)
        else:
            print(f"  WARNING: {msg}")
    else:
        print(f"  \u2713 salsa/{SALSA_BRANCH} in sync")


def prewarm_gpg(identity):
    import tempfile
    section("Phase 1b: GPG pre-warm")
    gpgkey = identity["GPGKEY"]
    print(f"  Key: {gpgkey}")
    print("  Enter passphrase now — gpg-agent will serve it automatically in phase 7.")
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"hollywood release gpg pre-warm\n")
        tmpfile = Path(f.name)
    sig = Path(str(tmpfile) + ".asc")
    try:
        run(["gpg", "--armor", "--detach-sign", "-u", gpgkey,
             "--output", str(sig), str(tmpfile)])
        print("  ✓ GPG agent unlocked.")
    finally:
        tmpfile.unlink(missing_ok=True)
        sig.unlink(missing_ok=True)


# ── phase 2: versions ─────────────────────────────────────────────────────

def determine_versions(mode):
    section("Phase 2: Determine versions")

    cl = (HOLLYWOOD_SRC / "debian" / "changelog").read_text()
    m = re.search(r"^hollywood \(([^)]+)\)", cl)
    if not m:
        die("Cannot parse version from debian/changelog")

    raw_ver = m.group(1).strip()
    # Strip Debian revision (-N) to get the upstream version
    base_ver = re.sub(r"-\d+$", "", raw_ver)
    print(f"  changelog version: {raw_ver}  →  upstream: {base_ver}")

    deb_version   = f"{base_ver}-1"
    ubuntu_ver    = f"{base_ver}-0ubuntu1"

    # Ubuntu series
    try:
        r = run(["ubuntu-distro-info", "--supported"], capture=True, check=False)
        series = r.stdout.split() if r.returncode == 0 else []
        if not series:
            raise RuntimeError
    except (FileNotFoundError, RuntimeError):
        print("  (ubuntu-distro-info unavailable — querying Launchpad)")
        d = json.loads(urllib.request.urlopen(
            "https://api.launchpad.net/1.0/ubuntu/series"
        ).read())
        active = {"Active Development", "Current Stable Release", "Supported"}
        series = [
            e["name"] for e in d["entries"]
            if e["status"] in active and float(e.get("version") or "0") >= 22.04
        ]

    try:
        devel_series = run(
            ["ubuntu-distro-info", "--devel"], capture=True, check=False
        ).stdout.strip()
    except FileNotFoundError:
        devel_series = series[-1] if series else "oracular"

    print(f"  Debian:       {deb_version}")
    print(f"  Ubuntu:       {ubuntu_ver} → {devel_series}")
    print(f"  PPA series:   {' '.join(series)}")

    outdir = Path(f"/tmp/hollywood-release-{base_ver}")
    if outdir.exists():
        shutil.rmtree(outdir)
    for sub in ["debs", "debian", "ppa", "ubuntu", "logs"]:
        (outdir / sub).mkdir(parents=True)
    print(f"  Output dir:   {outdir}")

    return dict(
        base_ver=base_ver,
        deb_version=deb_version,
        ubuntu_ver=ubuntu_ver,
        series=series,
        devel_series=devel_series,
        outdir=outdir,
    )


# ── phase 3: smoke test ───────────────────────────────────────────────────

_SMOKE_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper devscripts \
  tmux byobu 2>&1 | tail -5

WORKDIR=$(mktemp -d)
cp -a /src "$WORKDIR/hollywood"
cd "$WORKDIR/hollywood"

echo "--- Build ---"
dpkg-buildpackage -us -uc -b 2>&1 | tail -5

echo "--- Install ---"
apt-get install -y "$WORKDIR"/*.deb 2>&1 | tail -5

echo "--- Verify files ---"
test -x /usr/games/hollywood  && echo "  ✓ /usr/games/hollywood"
test -x /usr/games/wallstreet && echo "  ✓ /usr/games/wallstreet"
test -d /usr/lib/hollywood    && echo "  ✓ /usr/lib/hollywood"
test -f /usr/share/man/man1/hollywood.1 && echo "  ✓ man page"

NWIDGETS=$(ls /usr/lib/hollywood/ | wc -l)
echo "  ✓ $NWIDGETS widgets installed"

echo "--- Widget shebang + executable check ---"
for w in /usr/lib/hollywood/*; do
  test -x "$w" || { echo "  ✗ NOT executable: $w"; exit 1; }
  head -1 "$w" | grep -q "^#!" || { echo "  ✗ Missing shebang: $w"; exit 1; }
done
echo "  ✓ All widgets OK"

echo "=== Smoke test PASSED ==="
"""


def run_smoke_test():
    section("Phase 3: Smoke test (Docker ubuntu:noble)")
    run([
        "docker", "run", "--rm",
        "-v", f"{HOLLYWOOD_SRC}:/src:ro",
        "ubuntu:noble", "bash", "-c", _SMOKE_SCRIPT,
    ])
    print("  ✓ Smoke test PASSED")


# ── phase 3b: Salsa CI dry-run ────────────────────────────────────────────

_SALSA_CI_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  git git-buildpackage debhelper devscripts lintian 2>&1 | tail -5

git config --global --add safe.directory '*'
git clone /src /build/hollywood
cd /build/hollywood

git checkout -b debian/latest
git config user.email "ci@salsa.local"
git config user.name "Salsa CI"
git add -f debian/
git commit -q -m "debian: add packaging (Salsa CI simulation)"

echo "--- gbp buildpackage ---"
gbp buildpackage \
  --git-export-dir=/tmp/build-area \
  --git-no-sign-tags \
  --git-ignore-branch \
  -us -uc -d -sa 2>&1 | tail -10

ls -lh /tmp/build-area/
echo "=== Salsa CI PASSED ==="
"""


def run_salsa_ci():
    section("Phase 3b / salsa-ci: gbp buildpackage (Docker debian:sid)")
    run([
        "docker", "run", "--rm",
        "-v", f"{HOLLYWOOD_SRC}:/src:ro",
        "debian:sid", "bash", "-c", _SALSA_CI_SCRIPT,
    ])
    print("  ✓ Salsa CI simulation PASSED")


# ── phase 4: PPA source builds ────────────────────────────────────────────

_PPA_SERIES_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper devscripts git 2>&1 | tail -5

git config --global --add safe.directory /src

SRCDIR=$(mktemp -d)
git -C /src archive --format=tar HEAD | tar -x -C "$SRCDIR"

PPA_VER="${BASE_VER}~${CODENAME}1"
echo "=== Building $PPA_VER ==="

BUILDDIR=$(mktemp -d)
cp -a "$SRCDIR" "$BUILDDIR/${PKG}-${PPA_VER}"
cp -a /src/debian "$BUILDDIR/${PKG}-${PPA_VER}/"
cd "$BUILDDIR/${PKG}-${PPA_VER}"

echo "3.0 (native)" > debian/source/format

DATESTAMP=$(date -R)
{
  printf "%s (%s) %s; urgency=medium\n\n" "$PKG" "$PPA_VER" "$CODENAME"
  printf "  * PPA build %s\n\n" "$PPA_VER"
  printf " -- %s <%s>  %s\n\n" "$DEBFULLNAME" "$DEBEMAIL" "$DATESTAMP"
  cat debian/changelog
} > debian/changelog.new
mv debian/changelog.new debian/changelog

dpkg-buildpackage -S -us -uc -d 2>&1 | tail -3

cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc \
      "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
chown -R $(stat -c '%u:%g' /out) /out/
echo "=== $CODENAME done ==="
"""


def build_ppa_packages(v, identity):
    section("Phase 4: PPA source builds (parallel, all series)")
    ppa_series = [s for s in v["series"] if s != v["devel_series"]]
    print(f"  Series ({len(ppa_series)}): {' '.join(ppa_series)}")

    errors = {}

    def _build_one(codename):
        buf = io.StringIO()
        prev = getattr(_tls, "buf", None)
        _tls.buf = buf
        try:
            run([
                "docker", "run", "--rm",
                "-v", f"{HOLLYWOOD_SRC}:/src:ro",
                "-v", f"{v['outdir']}/ppa:/out",
                "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
                "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
                "-e", "PKG=hollywood",
                "-e", f"BASE_VER={v['base_ver']}",
                "-e", f"CODENAME={codename}",
                "ubuntu:noble", "bash", "-c", _PPA_SERIES_SCRIPT,
            ])
        except Exception as exc:
            errors[codename] = exc
        finally:
            _tls.buf = prev

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ppa_series)) as ex:
        futures = [ex.submit(_build_one, s) for s in ppa_series]
        concurrent.futures.wait(futures)

    for codename in ppa_series:
        marker = "✗" if codename in errors else "✓"
        print(f"  {marker} {codename}" + (f": {errors[codename]}" if codename in errors else ""))

    if errors:
        die(f"PPA build failed for: {', '.join(errors)}")

    changes = sorted((v["outdir"] / "ppa").glob("*.changes"))
    print(f"  ✓ PPA source packages built ({len(changes)} series)")
    for f in changes:
        print(f"    {f.name}")


# ── phase 5: Debian source build ─────────────────────────────────────────

_DEB_SOURCE_SCRIPT = r"""
set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential dpkg-dev debhelper devscripts git 2>&1 | tail -5

git config --global --add safe.directory /src

BUILDDIR=$(mktemp -d)

git -C /src archive --format=tar.gz --prefix="hollywood-${BASE_VER}/" HEAD \
  -o "$BUILDDIR/hollywood_${BASE_VER}.orig.tar.gz"

mkdir "$BUILDDIR/hollywood-${BASE_VER}"
tar -xzf "$BUILDDIR/hollywood_${BASE_VER}.orig.tar.gz" \
    -C "$BUILDDIR/hollywood-${BASE_VER}" --strip-components=1
cp -a /src/debian "$BUILDDIR/hollywood-${BASE_VER}/"

cd "$BUILDDIR/hollywood-${BASE_VER}"
echo "3.0 (quilt)" > debian/source/format

sed -i "1s/^hollywood ([^)]*)/hollywood (${DEB_VERSION})/" debian/changelog
sed -i "1s/) [^;]*;/) unstable;/" debian/changelog

dpkg-buildpackage -S -us -uc -d -sa 2>&1 | tail -3

cp -v "$BUILDDIR"/*.changes "$BUILDDIR"/*.dsc \
      "$BUILDDIR"/*.tar.* "$BUILDDIR"/*.buildinfo /out/ 2>/dev/null || true
chown -R $(stat -c '%u:%g' /out) /out/
echo "=== Debian unstable source built ==="
ls -lh /out/
"""


def build_debian_source(v, identity):
    section("Phase 5: Debian unstable source build (Docker)")
    run([
        "docker", "run", "--rm",
        "-v", f"{HOLLYWOOD_SRC}:/src:ro",
        "-v", f"{v['outdir']}/debian:/out",
        "-e", f"DEBEMAIL={identity['DEBEMAIL']}",
        "-e", f"DEBFULLNAME={identity['DEBFULLNAME']}",
        "-e", f"BASE_VER={v['base_ver']}",
        "-e", f"DEB_VERSION={v['deb_version']}",
        "ubuntu:noble", "bash", "-c", _DEB_SOURCE_SCRIPT,
    ])
    changes = sorted((v["outdir"] / "debian").glob("*.changes"))
    if not changes:
        die(f"Debian build produced no .changes files in {v['outdir']}/debian/")
    print(f"  ✓ Debian source package built")
    for f in changes:
        print(f"    {f.name}")


# ── phase 6: git tag + GitHub release ────────────────────────────────────

def create_github_release(v):
    section("Phase 6: Git tag + GitHub release")
    tag = v["base_ver"]

    local = run(["git", "-C", str(HOLLYWOOD_SRC), "tag", "--list", tag], capture=True)
    if tag in local.stdout.split():
        print(f"  (tag {tag} already exists locally — skipping creation)")
    else:
        run(["git", "-C", str(HOLLYWOOD_SRC), "tag", "-s", tag, "-m", f"hollywood {tag}"])
        print(f"  ✓ Tag {tag} created")

    remote = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "ls-remote", "--tags", "origin", tag],
        capture=True,
    )
    if remote.stdout.strip():
        print(f"  (tag {tag} already on origin — skipping push)")
    else:
        run(["git", "-C", str(HOLLYWOOD_SRC), "push", "origin", tag])
        print(f"  ✓ Tag {tag} pushed to origin")

    result = run(
        ["gh", "release", "view", tag, "--repo", "dustinkirkland/hollywood"],
        check=False, capture=True,
    )
    if result.returncode == 0:
        print(f"  (GitHub release {tag} already exists — skipping)")
    else:
        run([
            "gh", "release", "create", tag,
            "--repo", "dustinkirkland/hollywood",
            "--title", f"hollywood {tag}",
            "--generate-notes",
        ])
        print(f"  ✓ GitHub release {tag} created")


# ── phase 7: sign and upload ─────────────────────────────────────────────

def sign_and_upload(v, identity):
    section("Phase 7: Sign and upload")
    gpgkey = identity["GPGKEY"]
    outdir = v["outdir"]

    print(f"\n── Step 1: GPG signing  (key: {gpgkey})")
    signed = 0
    for subdir in ["ppa", "debian"]:
        for f in sorted((outdir / subdir).glob("*_source.changes")):
            print(f"  Signing: {f.name}")
            run(["debsign", "-k", gpgkey, str(f)])
            signed += 1
    print(f"  ✓ {signed} file(s) signed.")

    print(f"\n── Step 2: PPA  {PPA_TARGET}")
    if confirm(f"  Upload all series to {PPA_TARGET}?", skippable=True):
        for f in sorted((outdir / "ppa").glob("*_source.changes")):
            print(f"  dput {PPA_TARGET} {f.name}")
            run(["dput", PPA_TARGET, str(f)])
        print(f"  ✓ PPA uploads done.")
        print(f"    Monitor: https://launchpad.net/~hollywood/+archive/ubuntu/ppa")

    print("\n── Step 3: Debian unstable  (mentors.debian.net)")
    deb_changes = sorted((outdir / "debian").glob("*_source.changes"))
    if deb_changes:
        if confirm("  Upload to mentors.debian.net?", skippable=True):
            run(["dput", "mentors", str(deb_changes[0])])
            print(f"\n  ✓ Uploaded. Email Andreas Tille <tille@debian.org>:")
            print(f"    Subject: hollywood {v['deb_version']} sponsorship request")
            print(f"    Body:    https://mentors.debian.net/package/hollywood")
    else:
        print("  (no .changes files found — skipping)")

    print("\n  ✓ Sign and upload complete.")


# ── phase 8: Salsa push ───────────────────────────────────────────────────

def push_salsa(v):
    section("Phase 8: Push to Debian Salsa (games-team)")
    tag = v["base_ver"]

    r = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "remote", "get-url", SALSA_REMOTE],
        capture=True, check=False,
    )
    if r.returncode != 0:
        print(
            f"  Salsa remote '{SALSA_REMOTE}' not configured. Add it with:\n"
            f"    git remote add {SALSA_REMOTE} "
            f"git@salsa.debian.org:games-team/hollywood.git\n"
            f"  Skipping Salsa push."
        )
        return

    print("  Fetching salsa to check for upstream commits…")
    run(["git", "-C", str(HOLLYWOOD_SRC), "fetch", SALSA_REMOTE])

    salsa_ref = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "rev-parse",
         f"{SALSA_REMOTE}/{SALSA_BRANCH}"],
        capture=True, check=False,
    )
    if salsa_ref.returncode == 0:
        merge_base = run(
            ["git", "-C", str(HOLLYWOOD_SRC), "merge-base",
             "HEAD", f"{SALSA_REMOTE}/{SALSA_BRANCH}"],
            capture=True, check=False,
        )
        if (merge_base.returncode == 0 and
                merge_base.stdout.strip() != salsa_ref.stdout.strip()):
            die(
                f"salsa/{SALSA_BRANCH} has commits not in local master.\n"
                f"  Merge first: git merge {SALSA_REMOTE}/{SALSA_BRANCH}\n"
                f"  Then re-run."
            )

    run(["git", "-C", str(HOLLYWOOD_SRC), "push", SALSA_REMOTE,
         f"HEAD:refs/heads/{SALSA_BRANCH}"])
    print(f"  ✓ salsa/{SALSA_BRANCH} updated")

    remote_tag = run(
        ["git", "-C", str(HOLLYWOOD_SRC), "ls-remote", "--tags", SALSA_REMOTE, tag],
        capture=True,
    )
    if remote_tag.stdout.strip():
        print(f"  (tag {tag} already on salsa — skipping)")
    else:
        run(["git", "-C", str(HOLLYWOOD_SRC), "push", SALSA_REMOTE, tag])
        print(f"  ✓ Tag {tag} pushed to salsa")

    print(f"    https://salsa.debian.org/games-team/hollywood")

    # Phase 8b: seed upstream branch via gbp import-orig
    section("Phase 8b: Seed upstream branch (gbp import-orig)")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="hollywood-salsa-") as tmpdir_s:
        tmpdir = Path(tmpdir_s)
        orig_name = f"hollywood_{tag}.orig.tar.gz"
        orig_path = tmpdir / orig_name

        print(f"  Generating {orig_name} from tag {tag}\u2026")
        run(["git", "-C", str(HOLLYWOOD_SRC), "archive",
             "--format=tar.gz", f"--prefix=hollywood-{tag}/",
             f"--output={orig_path}", tag])

        salsa_clone = tmpdir / "hollywood-salsa"
        print("  Cloning Salsa into temp dir\u2026")
        run(["git", "clone",
             "git@salsa.debian.org:games-team/hollywood.git",
             str(salsa_clone)])

        run(["git", "-C", str(salsa_clone), "config",
             "user.email", "dustin.kirkland@gmail.com"])
        run(["git", "-C", str(salsa_clone), "config",
             "user.name", "Dustin Kirkland"])

        print("  Running gbp import-orig\u2026")
        run(["gbp", "import-orig",
             f"--upstream-branch={SALSA_UPSTREAM_BRANCH}",
             f"--debian-branch={SALSA_BRANCH}",
             "--no-merge",
             str(orig_path)],
            cwd=str(salsa_clone))

        print(f"  Pushing {SALSA_UPSTREAM_BRANCH} to Salsa\u2026")
        run(["git", "-C", str(salsa_clone), "push", "origin",
             SALSA_UPSTREAM_BRANCH])
        print(f"  \u2713 upstream branch seeded on Salsa")


# ── phase 9: Chainguard reminder ─────────────────────────────────────────

def chainguard_reminder(v):
    section("Phase 9: Chainguard")
    print(
        f"\n  The cgr.dev/chainguard/hollywood image is built from the Chainguard\n"
        f"  images repository. After the Debian upload is accepted, verify that\n"
        f"  the image has picked up hollywood {v['base_ver']}:\n"
        f"\n"
        f"    docker run --rm cgr.dev/chainguard/hollywood --version 2>/dev/null || \\\n"
        f"      docker run --rm cgr.dev/chainguard/hollywood\n"
        f"\n"
        f"  If the image has not updated within a few days of the Debian upload,\n"
        f"  file an issue or PR at:\n"
        f"    https://github.com/chainguard-images/images\n"
        f"  and reference the Debian tracker:\n"
        f"    https://tracker.debian.org/pkg/hollywood\n"
    )


# ── open-dev ──────────────────────────────────────────────────────────────

def open_dev(identity):
    banner("open-dev: bump to next development version")

    cl_path = HOLLYWOOD_SRC / "debian" / "changelog"
    cl = cl_path.read_text()
    m = re.search(r"^hollywood \(([^)]+)\)", cl)
    if not m:
        die("Cannot parse version from debian/changelog")
    raw_ver = m.group(1).strip()
    base_ver = re.sub(r"-\d+$", "", raw_ver)

    parts = base_ver.rsplit(".", 1)
    try:
        next_ver = f"{parts[0]}.{int(parts[1]) + 1}"
    except (IndexError, ValueError):
        die(f"Cannot auto-increment version '{base_ver}' — edit debian/changelog manually.")

    print(f"  {base_ver}  →  {next_ver}")

    datestamp = subprocess.check_output(["date", "-R"]).decode().strip()
    new_stanza = (
        f"hollywood ({next_ver}-1) UNRELEASED; urgency=medium\n"
        f"\n"
        f"  * Open {next_ver} for development\n"
        f"\n"
        f" -- {identity['DEBFULLNAME']} <{identity['DEBEMAIL']}>  {datestamp}\n"
        f"\n"
    )
    cl_path.write_text(new_stanza + cl)
    print("\n  debian/changelog top:")
    for line in cl_path.read_text().splitlines()[:5]:
        print(f"    {line}")

    run(["git", "-C", str(HOLLYWOOD_SRC), "add", "debian/changelog"])
    run(["git", "-C", str(HOLLYWOOD_SRC), "commit",
         "-m", f"bump version to {next_ver} and open for development"])
    print(f"  ✓ Committed: bump version to {next_ver}")


# ── summary ───────────────────────────────────────────────────────────────

def print_summary(v, mode):
    outdir = v["outdir"]
    label = "RC build" if mode == "rc" else "Release"
    banner(f"{label} complete: hollywood {v['base_ver']}")
    print(
        f"\n  PPA:     {PPA_TARGET} — {v['base_ver']}~{{series}}1\n"
        f"  Debian:  hollywood {v['deb_version']} → unstable\n"
        f"  Chainguard: check cgr.dev/chainguard/hollywood after Debian accept\n"
    )
    if mode == "final":
        print(
            f"  GitHub:  https://github.com/dustinkirkland/hollywood/releases/tag/{v['base_ver']}\n"
            f"  Salsa:   https://salsa.debian.org/games-team/hollywood\n"
        )
    print(f"  Artifacts: {outdir}")
    if mode == "rc":
        print("\n  When ready to cut the final: ./release.py final")
    else:
        print("\n  Next: ./release.py open-dev")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="hollywood release pipeline",
        epilog="Modes: rc (default), final, open-dev, salsa-ci",
    )
    parser.add_argument(
        "mode", nargs="?",
        choices=["rc", "final", "open-dev", "salsa-ci"],
        default="rc",
    )
    parser.add_argument("--interactive", "-i", action="store_true", default=False)
    args = parser.parse_args()
    mode = args.mode

    global _interactive
    _interactive = args.interactive

    if mode == "open-dev":
        identity = load_identity()
        open_dev(identity)
        return

    if mode == "salsa-ci":
        run_salsa_ci()
        return

    banner(f"hollywood release pipeline — {mode.upper()}")

    identity = load_identity()
    check_salsa_sync(mode)
    check_tools(mode)
    check_clean()
    prewarm_gpg(identity)
    v = determine_versions(mode)

    # Phases 3 + 3b run in parallel (both are Docker, independent)
    run_parallel([
        ("smoke test",  run_smoke_test),
        ("salsa CI",    run_salsa_ci),
    ], log_dir=v["outdir"] / "logs")

    # PPA builds (per-series parallelism handled inside)
    build_ppa_packages(v, identity)

    # Debian source build
    build_debian_source(v, identity)

    if mode == "final":
        create_github_release(v)
        sign_and_upload(v, identity)
        push_salsa(v)
        chainguard_reminder(v)

    print_summary(v, mode)


if __name__ == "__main__":
    main()
