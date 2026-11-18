"""Adim 2: runs.json'daki her AIWP netCDF dosyasi icin t2 chunk (offset,size) + lon/lat eksenlerini
h5py uzerinden cikar, scripts/catalog/cat_<code>_<yyyymmddhh>.json olarak yazar.

Kullanim:
  python scripts/_aiwp_catalog_build.py            # hepsi
  AIWP_WORKERS=12 AIWP_LIMIT=6 python scripts/_aiwp_catalog_build.py   # smoke test (ilk 6 run)
"""

import os
import sys
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
import h5py
import fsspec

OUT = os.environ.get("HEAT_CATALOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "catalog"
)
RUNS_FILE = os.environ.get("AIWP_RUNS_FILE") or os.path.join(OUT, "runs.json")
RUNS_FILE = os.path.abspath(RUNS_FILE)
WORKERS = int(os.environ.get("AIWP_WORKERS", "12"))
LIMIT = int(os.environ.get("AIWP_LIMIT", "0"))
BUCKET = "noaa-oar-mlwp-data"


def build_one(item):
    code, date, hh, file = item["code"], item["date"], item["hh"], item["file"]
    path = f"{BUCKET}/{file}"
    dest = os.path.join(OUT, f"cat_{code}_{date.replace('-', '')}{hh}.json")
    if os.path.exists(dest):
        return (code, date, hh, "cached")
    t0 = time.time()
    fs = fsspec.filesystem("s3", anon=True)
    for attempt in range(3):
        try:
            # cache_type='none': tum dosyayi (~4.6GB) indirip 435s/file yerine Range GET'lerle ~5s/file
            with h5py.File(fs.open(path, "rb", cache_type="none"), "r") as hf:
                ds = hf["t2"]
                n = ds.id.get_num_chunks()
                steps = []
                offs = []
                for i in range(n):
                    c = ds.id.get_chunk_info(i)
                    steps.append([i, int(c.byte_offset), int(c.size)])
                    offs.append(int(c.byte_offset))
                mono = all(offs[i] < offs[i + 1] for i in range(len(offs) - 1))
                lon = hf["longitude"][:].astype(float).tolist()
                lat = hf["latitude"][:].astype(float).tolist()
                shape = list(ds.shape)
                chunks = list(ds.chunks)
                out = {
                    "code": code,
                    "date": date,
                    "hh": hh,
                    "file": file,
                    "shape": shape,
                    "chunks": chunks,
                    "n": n,
                    "offset_monotonic": mono,
                    "lon": lon,
                    "lat": lat,
                    "steps": steps,
                    "build_s": round(time.time() - t0, 1),
                }
                with open(dest, "w") as f:
                    json.dump(out, f)
                return (code, date, hh, f"ok {time.time() - t0:.0f}s n={n}")
        except Exception as e:
            if attempt == 2:
                return (code, date, hh, f"ERR {type(e).__name__}: {str(e)[:120]}")
            time.sleep(3)
    return (code, date, hh, "ERR unknown")


def main():
    runs = json.load(open(RUNS_FILE))["runs"]
    if LIMIT:
        runs = runs[:LIMIT]
    print(f"{len(runs)} run islenecek, workers={WORKERS}", flush=True)
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(build_one, r): r for r in runs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            results.append(res)
            if done % 20 == 0 or res[3].startswith("ERR"):
                print(
                    f"[{done}/{len(runs)}] {res[0]} {res[1]}{res[2]} -> {res[3]} ({time.time() - t0:.0f}s)", flush=True
                )
    errs = [r for r in results if r[3].startswith("ERR")]
    oks = [r for r in results if r[3] != "cached" and not r[3].startswith("ERR")]
    print(
        f"Bitti: {len(results)} is, {len(oks)} yeni, {sum(1 for r in results if r[3] == 'cached')} cache, {len(errs)} hata, {time.time() - t0:.0f}s",  # noqa: E501
        flush=True,
    )
    for e in errs[:20]:
        print("  HATA:", e, flush=True)
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
