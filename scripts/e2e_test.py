#!/usr/bin/env python3
"""End-to-end test: drive the REAL cloud+ingest flow like the browser does.

  POST /api/videos (cloud, X-API-KEY) -> ticket
  -> /ingest/init (up.osmike.com, ticket)  [chunk_hash_algo=sha256]
  -> PUT chunks -> finalize (ingest fires callback -> cloud enqueues encode)
  -> poll GET /api/videos/{id} until ready
  -> fetch the HLS master over the public URL, assert #EXTM3U

Memory: hashes chunks with sha256 (browser algo), streamed 1 MB reads.

Usage:
  e2e_test.py --cloud https://video.osmike.com --api-key KEY --file test.mp4 [--insecure]
"""
import argparse, hashlib, json, os, ssl, sys, urllib.request, urllib.error

BUF = 1024 * 1024
CHUNK = 4 * 1024 * 1024


def _ctx(insecure):
    if not insecure:
        return None
    c = ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE
    return c


def _req(method, url, headers=None, data=None, ctx=None):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def build_manifest(path, upload_id):
    total = os.path.getsize(path)
    count = (total + CHUNK - 1) // CHUNK or 1
    chunks = {}
    whole = hashlib.sha256()
    with open(path, "rb") as f:
        for i in range(count):
            f.seek(i * CHUNK)
            remaining = min(CHUNK, total - i * CHUNK)
            h = hashlib.sha256(); read = 0
            while read < remaining:
                b = f.read(min(BUF, remaining - read))
                if not b: break
                h.update(b); whole.update(b); read += len(b)
            chunks[str(i)] = "sha256:" + h.hexdigest()
    return {"upload_id": upload_id, "filename": os.path.basename(path),
            "content_type": "video/mp4", "total_size": total, "chunk_size": CHUNK,
            "count": count, "chunk_hash_algo": "sha256",
            "file_hash": "sha256:" + whole.hexdigest(), "chunks": chunks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()
    ctx = _ctx(a.insecure)
    cloud = a.cloud.rstrip("/")

    m = build_manifest(a.file, "pending")
    print(f"[*] {a.file}: size={m['total_size']} count={m['count']} file_hash={m['file_hash']}")

    # 1. ticket
    st, body = _req("POST", cloud + "/api/videos",
                    {"X-API-KEY": a.api_key, "Content-Type": "application/json"},
                    json.dumps({"filename": m["filename"], "content_type": m["content_type"],
                                "total_size": m["total_size"], "file_hash": m["file_hash"],
                                "chunk_size": CHUNK, "count": m["count"]}).encode(), ctx)
    if st != 200:
        print(f"[!] POST /api/videos -> {st}: {body.decode(errors='replace')}"); sys.exit(1)
    tk = json.loads(body)
    print(f"[*] ticket ok video_id={tk['video_id']} upload_id={tk['upload_id']} ingest={tk['ingest_url']}")
    m["upload_id"] = tk["upload_id"]
    ingest = tk["ingest_url"].rstrip("/"); ticket = tk["ticket"]
    auth = {"Authorization": "Bearer " + ticket}

    # 2. init
    st, body = _req("POST", ingest + "/ingest/init",
                    dict(auth, **{"Content-Type": "application/json"}),
                    json.dumps(m).encode(), ctx)
    if st != 200:
        print(f"[!] init -> {st}: {body.decode(errors='replace')}"); sys.exit(1)
    print(f"[*] init ok: {json.loads(body)}")

    # 3. status -> missing
    st, body = _req("GET", f"{ingest}/ingest/{m['upload_id']}/status", auth, None, ctx)
    missing = json.loads(body)["missing"]
    print(f"[*] missing {len(missing)}/{m['count']}")

    # 4. chunks
    with open(a.file, "rb") as f:
        for i in missing:
            f.seek(i * CHUNK)
            data = f.read(min(CHUNK, m["total_size"] - i * CHUNK))
            st, resp = _req("PUT", f"{ingest}/ingest/{m['upload_id']}/chunk/{i}",
                            dict(auth, **{"Content-Type": "application/octet-stream"}),
                            data, ctx)
            if st != 200:
                print(f"[!] chunk {i} -> {st}: {resp.decode(errors='replace')[:200]}"); sys.exit(2)
    print("[*] all chunks sent")

    # 5. finalize
    st, body = _req("POST", f"{ingest}/ingest/{m['upload_id']}/finalize", auth, None, ctx)
    fin = json.loads(body)
    print(f"[*] finalize {st}: {fin}")
    if st != 200 or not fin.get("complete"):
        print("[!] finalize did not complete"); sys.exit(3)
    cb = fin.get("callback", {})
    print(f"[*] callback: {cb}")

    # 6. poll cloud for ready
    import time
    vid = tk["video_id"]; hls_url = None
    for _ in range(120):
        st, body = _req("GET", f"{cloud}/api/videos/{vid}", {"X-API-KEY": a.api_key}, None, ctx)
        v = json.loads(body)
        print(f"    status={v.get('status')} dur={v.get('duration_sec')}")
        if v.get("status") == "ready":
            hls_url = v.get("hls_url"); break
        if v.get("status") == "failed":
            print(f"[!] encode FAILED: {v.get('error')}"); sys.exit(4)
        time.sleep(3)
    if not hls_url:
        print("[!] never reached ready"); sys.exit(5)
    print(f"[*] READY hls_url={hls_url}")

    # 7. fetch HLS master
    st, body = _req("GET", cloud + hls_url, {"X-API-KEY": a.api_key}, None, ctx)
    text = body.decode(errors="replace")
    print(f"[*] master.m3u8 -> {st}\n{text[:300]}")
    if st == 200 and text.startswith("#EXTM3U"):
        print("[✓] END-TO-END PASS: valid HLS master served.")
        sys.exit(0)
    print("[!] master not valid HLS"); sys.exit(6)


if __name__ == "__main__":
    main()
