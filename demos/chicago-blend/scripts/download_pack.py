"""Download a folder from the private demo bucket, skipping what is already there.

Usage: python download_pack.py gs://BUCKET/PATH LOCAL_DIR

Uses the service-account key in GOOGLE_APPLICATION_CREDENTIALS (or this machine's
gcloud login). The folder appears under LOCAL_DIR only once complete: it is copied
to LOCAL_DIR.tmp first and renamed, so an interrupted copy is redone, not half-used.
"""

import shutil
import sys
import time
from pathlib import Path

import gcsfs

src, dst = sys.argv[1].removeprefix("gs://").rstrip("/"), Path(sys.argv[2])
if dst.exists():
    print(f"{dst}: already downloaded, skipped")
    sys.exit(0)
t0 = time.time()
fs = gcsfs.GCSFileSystem()
tmp = dst.with_name(dst.name + ".tmp")
shutil.rmtree(tmp, ignore_errors=True)
fs.get(src + "/", str(tmp) + "/", recursive=True)
tmp.rename(dst)
size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file()) / 1e6
print(f"downloaded gs://{src} to {dst}: {size:.1f} MB in {time.time() - t0:.0f} s")
