"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước, đúng thứ tự. Bước 4 timeout -> ABORT, không đụng vào edge/active_region."""
    primary = "a" if target == "b" else "b"
    result = {"ok": False, "target": target, "primary": primary}

    emit(step="1_verify_target", target=target, state=state_of(target))

    try:
        meta = snapshot.get(target, backend)
        r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                         pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    except BaseException as e:  # snapshot.get dùng SystemExit khi chưa có snapshot
        emit(step="2_restore_snapshot", target=target, ok=False, error=str(e))
        result["error"] = f"restore_failed: {e}"
        return result
    emit(step="2_restore_snapshot", target=target, ok=True, backend=backend,
         snapshot_at=meta.get("snapshot_at"), embed_model_version=meta.get("embed_model_version"),
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"])
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))

    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full")

    t0, ready, body = time.time(), False, None
    while time.time() - t0 < wait:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            body = resp.json()
            if resp.status_code == 200:
                ready = True
                break
        except Exception as e:
            body = {"error": type(e).__name__}
        time.sleep(0.5)
    waited = round(time.time() - t0, 2)
    emit(step="4_wait_ready", target=target, ready=ready, waited_s=waited, readyz=body)
    result["waited_s"] = waited
    if not ready:
        emit(step="abort", target=target, reason=f"target chua ready sau {wait}s -> KHONG cutover")
        result["error"] = "target_not_ready"
        return result

    ACTIVE.write_text(target)
    emit(step="5_dns_cutover", target=target, active_region=target)
    st = state_of(target)
    result.update(ok=True, cutover=True, vectors=st.get("count"), weights=st.get("weights"))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
