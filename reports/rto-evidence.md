# RTO/RPO Evidence — Lab 23 (Nguyễn Phúc Bảo — 2A202602925)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). Mọi số liệu lấy từ drill chạy ngày 2026-10-09 (UTC),
bare mode, `--mock`, chaos mode `netblock` (SIGSTOP).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:23:26` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` (ReadTimeout, 2013.9ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage (16/32 request fail) | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

t_outage = `2026-10-09T04:24:23` (ts `1791519863.248`).

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.0 | dòng `ok:false` đầu (ReadTimeout) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +19.1 | `to:UNHEALTHY, region:a` (3 lần ReadTimeout liên tiếp) | `reports/health-events.jsonl:2` |
| Runbook xác nhận outage, bắt đầu failover | +21.2 | `step:1_verify_target` | `reports/failover-events.jsonl:1` |
| Snapshot restore xong | +21.2 | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +27.4 | `step:4_wait_ready` (`waited_s=6.15`) | `reports/failover-events.jsonl:4` |
| DNS cutover | +27.4 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.2** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.2s | 300s (5 phút) | **PASS** (`reports/measure-drill-2.json`) |
| RPO — Vector DB | 8.0s / 4 doc | 300s (5 phút) | **PASS** (`reports/failover-events.jsonl:2`) |

RPO giải thích: snapshot được restore là chu kỳ replicate lúc `04:24:36`
(`reports/replication.jsonl:2`). Lúc restore, primary có doc mới hơn snapshot 8.0s,
tức 4 doc (ingest 0.5 doc/s) không có trong region B.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 19.1 (floor 15.0 + 4.1) | `interval_s=5.0 × threshold=3` trong `reports/health-events.jsonl:2`. Phần dư 4.1s là probe timeout 2s/lần và độ lệch pha giữa lúc kill với chu kỳ poll | Giảm interval (vd 2s × 3 = 6s) hoặc timeout 1s. Đổi lại: nhiều probe hơn, dễ flap hơn |
| Runbook xác nhận + mở incident | 2.1 | t_detect → `1_verify_target` (`reports/failover-events.jsonl:1`). Runbook tự probe lại primary (timeout 2s) trước khi tin alert | Bỏ probe xác nhận lại (chỉ tin alert) — tiết kiệm ~2s nhưng mất 1 lớp chống báo động giả |
| Snapshot restore | 0.0 | 2_restore → 3_scale (`reports/failover-events.jsonl:3`). Copy SQLite + weights trên local FS, chỉ vài ms | Ở cloud thật đây là phần lớn (tải GB weights). Giảm bằng replicate liên tục sang region B (warm standby có sẵn state) |
| GPU pool warm-up | 6.1 | `waited_s=6.15` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) | Giữ region B ở `full` sẵn (hot standby) — tốn chi phí GPU chạy không |
| DNS/LB TTL cache | 0.9 | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:39`). Edge cache TTL 5s, lần này cache tình cờ hết hạn ngay sau cutover; worst case là +5s | Hạ TTL (vd 1s), hoặc edge chủ động invalidate cache khi có cutover |
| **Tổng** | **28.2s** | khớp `rto_measured_s` | |
