"""Build step: fetch sqlglot at the pinned commit (allowed by the rules) and unpack it to /opt/sqlglot."""
import io, os, shutil, sys, tarfile, urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DEST = sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot"
data = None
for url in (f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz",
            f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{SHA}"):
    for attempt in range(3):
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            break
        except Exception as e:  # noqa: BLE001
            print("fetch failed", url, e, file=sys.stderr)
    if data:
        break
if not data:
    sys.exit("could not fetch sqlglot")
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
    tf.extractall("/tmp/sqlglot_src", filter="data")
root = os.path.join("/tmp/sqlglot_src", os.listdir("/tmp/sqlglot_src")[0])
os.makedirs(DEST, exist_ok=True)
for name in ("sqlglot", "tests", "pyproject.toml", "setup.cfg", "setup.py"):
    src = os.path.join(root, name)
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(DEST, name))
    elif os.path.exists(src):
        shutil.copy(src, os.path.join(DEST, name))
shutil.rmtree("/tmp/sqlglot_src")
print("sqlglot", SHA, "->", DEST)
