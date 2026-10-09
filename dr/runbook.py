"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def _jsonl(p: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (bán tự động, chống flapping)."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() == "y"


def wait_for_alert(primary: str, since: float, timeout: float) -> dict | None:
    """Chờ health checker bắn alert UNHEALTHY cho primary -- không tin 1 lần probe fail."""
    end = time.time() + timeout
    while time.time() < end:
        for e in _jsonl(HEALTH):
            if (e.get("event") == "state_change" and e.get("region") == primary
                    and e.get("to") == "UNHEALTHY" and e["ts"] >= since):
                return e
        time.sleep(0.5)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    t_start = time.time()
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == primary]
    t_outage = kills[-1]["ts"] if kills else None

    # 1. Xác nhận outage: alert từ health checker + tự probe lại cả 2 region
    alert = wait_for_alert(primary, t_outage or t_start - 60, timeout=120)
    probes = {r: hc.probe(r, 2.0) for r in (primary, target)}
    outage = alert is not None and not probes[primary][0]
    step(1, "xac_nhan_outage", confirmed=outage, alert=alert,
         probes={r: {"ready": ok, "reason": why} for r, (ok, why) in probes.items()})
    if not outage:
        step(7, "post_incident", aborted=True, reason="khong xac nhan duoc outage",
             elapsed_s=round(time.time() - t_start, 2))
        return {"ok": False, "reason": "outage_not_confirmed"}

    # 2. Thông báo incident + bấm giờ
    t_known = time.time()
    step(2, "thong_bao_incident", t_outage=t_outage, t_detect=alert["ts"],
         t_operator_known=t_known,
         notify_delay_s=None if t_outage is None else round(t_known - t_outage, 2),
         msg=f"SEV1: region-{primary} down, failover sang region-{target}")

    # 3. Failover (gọi đúng 1 lần)
    if not confirm(auto, f"Failover tu region-{primary} sang region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator tu choi")
        return {"ok": False, "reason": "operator_declined"}
    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", failover_ok=res.get("ok"), waited_s=res.get("waited_s"))

    # 4-5. Chỉ đọc lại kết quả failover
    step(4, "verify_state_replica", vectors=res.get("vectors"), weights=res.get("weights"),
         rpo_seconds=res.get("rpo_seconds"), docs_lost=res.get("docs_lost"),
         embed_model_version=res.get("embed_model_version"))
    step(5, "dns_cutover", ok=res.get("ok"), cutover=res.get("cutover", False),
         error=res.get("error"))

    # 6. Golden signals: 10 request thật vào region phụ
    lat, errs = [], 0
    for _ in range(10):
        t = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=2.0)
            if r.status_code != 200 or "error" in r.json():
                errs += 1
        except Exception:
            errs += 1
        lat.append((time.time() - t) * 1000)
    lat.sort()
    step(6, "verify_golden_signals", requests=10, p95_ms=round(lat[int(0.95 * (len(lat) - 1))], 1),
         error_rate=errs / 10)

    # 7. Post-incident
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", elapsed_s=elapsed,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": bool(res.get("ok")), "elapsed_s": elapsed, "failover": res,
            "error_rate": errs / 10}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
