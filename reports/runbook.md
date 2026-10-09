# Runbook 1 trang — Region chính (A) down → failover sang B

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Mọi lệnh chạy từ thư mục gốc repo.

**Đường nhanh (khuyến nghị):** `python3 dr/runbook.py --primary a --target b --backend fs`
tự làm bước 1–7 và hỏi `y/N` trước khi failover. Log ở `reports/runbook-run.jsonl`.
Bảng dưới là các bước làm tay tương đương, dùng khi script lỗi.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `grep '"region": "a"' reports/health-events.jsonl \| tail -1` rồi `curl -s -m 2 localhost:8001/readyz` | Health log có dòng `"to": "UNHEALTHY"` cho region a (3 fail liên tiếp) **và** curl timeout/503. Region B vẫn trả `/healthz` 200: `curl -s localhost:8002/healthz` | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `echo "{\"ts\": $(date +%s.%N), \"name\": \"thong_bao_incident\", \"msg\": \"SEV1 region-a down\"}" >> reports/runbook-run.jsonl` | Có dòng `thong_bao_incident` trong `reports/runbook-run.jsonl`. Đã post SEV1 lên kênh incident, chỉ định Incident Commander (IC) | On-call SRE |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | `curl -s localhost:8002/v1/state` cho `count > 0`, `weights: true`. Ghi lại `rpo_seconds` (lệnh `lag`) vào incident | On-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi `until curl -sf localhost:8002/readyz; do sleep 1; done` | `/readyz` của b trả 200, `"ready": true` (sau khoảng 6s warm-up) | On-call SRE |
| 5 | DNS/LB cutover (**chỉ sau khi bước 4 xong**) | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `"active_region":"b"`. Chờ tối đa TTL 5s, `curl -s localhost:8080/v1/infer` trả `"edge_region":"b"` | On-call SRE (IC duyệt) |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả `200`; p95 < 200ms, error rate < 1% | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid": true`, `rto_verdict` != null (PASS khi ≤ 300s). Điền `reports/postmortem.md` trong 48h | IC |

**Không bao giờ** làm bước 5 trước khi bước 4 trả 200. Nếu làm vậy, user nhận 503 từ cả hai region.
Nếu bước 4 không ready sau 60s: **ABORT**, giữ `active_region=a`, escalate lên IC.

## Rollback (failover ngược về A)

**Điều kiện, phải đủ cả 4:**
1. Region A trả `/readyz` 200 liên tục ≥ **15 phút**, tức `curl -sf localhost:8001/readyz`
   và health log có dòng `"to": "HEALTHY"` cho region a, không flap lại.
2. Đã replicate ngược dữ liệu ghi vào B trong lúc incident sang A (B→A). Lag ≤ 1 chu kỳ
   (`python3 state/snapshot.py lag`). Cách làm: `put --region b` rồi `get --region a`.
3. Root cause của outage A đã được xác định và xử lý.
4. Nằm trong khung giờ ít traffic. Không rollback khi đang có incident khác.

**Ai quyết định:** chỉ **Incident Commander** được duyệt rollback. On-call không tự
ý failback. Rollback chạy cùng runbook theo chiều ngược lại:
`python3 dr/runbook.py --primary b --target a --backend fs` (có bước confirm y/N).

**Circuit breaker chống flapping:** sau một lần failover, không tự động failover thêm
lần nào trong **30 phút**. Mọi failover tiếp theo trong khoảng đó đều phải có IC confirm
bằng tay (§4 Anti-Patterns: full-auto không có circuit breaker thì 2 region sẽ flap qua lại).
