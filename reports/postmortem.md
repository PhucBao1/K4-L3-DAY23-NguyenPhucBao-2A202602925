# Postmortem — DR Drill Lab 23 (Nguyễn Phúc Bảo — 2A202602925)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** Region A bị netblock (SIGSTOP) lúc 04:24:23 UTC ngày 2026-10-09. Health
checker phát hiện sau 19.1s. Runbook `--auto` failover sang region B. User có request
thành công đầu tiên từ B sau **28.2s**. Mất **4 doc** (RPO 8.0s).

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T04:24:23 | outage bắt đầu (kill region-a, netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T04:24:23 | user đầu tiên bị ảnh hưởng (ReadTimeout 2015ms) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T04:24:42 | health check alert: region-a UNHEALTHY sau 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T04:24:44 | operator (runbook) xác nhận outage, mở incident SEV1, confirm cutover — failover bắt đầu (`1_verify_target`) | `reports/failover-events.jsonl:1` |
| 2026-10-09T04:24:44 | restore snapshot vào region-b (RPO 8.0s, 4 doc) | `reports/failover-events.jsonl:2` |
| 2026-10-09T04:24:50 | region-b ready sau warm-up 6.15s, DNS cutover sang b | `reports/failover-events.jsonl:5` |
| 2026-10-09T04:24:51 | resolved — request đầu tiên OK từ region-b | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.2s` · gap: `-271.8s` (còn dư 271.8s so với mục tiêu)
- RPO mục tiêu: 300s · đo được: `8.0s` (`4` doc bị mất) · gap: `-292.0s`
- **Bước tốn nhiều giây nhất:** `health-check detection` (19.1s, chiếm 68% RTO). Lý do:
  floor `interval × threshold = 5 × 3 = 15s` cộng thêm ~4.1s. Mỗi probe vào region bị
  netblock phải treo đủ timeout 2s mới tính là fail. Kill lại rơi vào giữa chu kỳ poll.
  Ngoài ra checker probe tuần tự a rồi b, nên mỗi vòng thực tế dài hơn 5s.
- Bước thứ hai là GPU warm-up (6.1s): region B ở trạng thái `warm` (passive), phải lên
  `full` rồi chờ warm-up.

Gap analysis: RTO và RPO đều đạt mục tiêu với biên độ lớn. Rủi ro thật nằm ở những phần
lab **không** mô phỏng. Restore ở đây chỉ mất 0.0s vì copy file local. Ở cloud, tải vài
GB model weights + index có thể mất vài phút và ăn gần hết budget 300s.

## 3. Root cause (5 whys)

Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user lỗi 28s?** Vì edge vẫn route sang region-a cho tới khi có cutover.
2. **Vì sao cutover không xảy ra sớm hơn?** Vì phải chờ health checker có đủ 3 lần
   fail liên tiếp (15s+), sau đó region B còn phải restore state và warm-up 6s.
3. **Vì sao region B phải restore + warm-up?** Vì kiến trúc là active-passive kiểu
   "pilot light". B không giữ data và pool không ở `full` để tiết kiệm chi phí.
4. **Vì sao vẫn mất 4 doc?** Vì replication chạy theo chu kỳ 30s (snapshot), không
   liên tục. Mọi doc ingest sau lần `put` gần nhất đều nằm ngoài bản restore.
5. **Bước sẽ thất bại nếu là outage thật:** `2_restore_snapshot` đọc snapshot từ
   `state/_replica`. Nếu object store cũng nằm cùng region với primary (không thật sự
   cross-region), hoặc chưa từng có `put` nào thành công, failover sẽ chết ở bước 2 và
   RTO = vô hạn. Ngoài ra ingest vẫn ghi vào region-a sau khi bị cô lập (split-brain
   ghi dữ liệu). Chưa có cơ chế nào chặn ghi vào primary hay replay lại các doc đó.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Probe 2 region song song + giảm probe timeout 2s→1s, interval 5s→3s (giữ threshold=3) | SRE on-call lead | 2026-10-23 | RTO: ~19s → ~10s (giảm ~9s) |
| 2 | Replicate liên tục (CDC/WAL streaming) thay cho snapshot 30s | Data platform | 2026-11-06 | RPO: 8s (worst case 30s) → < 2s |
| 3 | Alert khi `replication.jsonl` không có `put` mới quá 2 chu kỳ (RPO monitor) | SRE | 2026-10-16 | Ngăn trường hợp RPO vô hạn / failover chết ở bước 2 |
| 4 | Edge invalidate cache ngay khi `active_region` đổi (không chờ TTL 5s) | Platform/Edge | 2026-10-30 | RTO worst case: giảm tới 5s |
| 5 | Fence primary: chặn ingest vào region-a sau khi đã cutover, replay doc lệch khi failback | Data platform | 2026-11-13 | Tránh split-brain, giữ `docs_lost` đúng nghĩa |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` là bao nhiêu giây? Chiếm bao nhiêu % RTO?**
   5s × 3 = **15s** là detect floor, tức 15.0 / 28.2 = **53%** RTO. Thời điểm detect
   thực tế là 19.1s, chiếm **68%** RTO. Muốn RTO 5 phút, về lý thuyết interval có thể
   lên tới ~90s (90 × 3 = 270s), nhưng khi đó gần như không còn chỗ cho restore/warm-up.
   Interval hợp lý nên ≤ 10–15s.

2. **Hạ interval xuống 1s thì RTO giảm mấy giây, trả giá gì?**
   Floor giảm từ 15s xuống 3s, RTO giảm khoảng **12s** (từ ~28s còn ~16s, chưa tính
   timeout). Cái giá phải trả: (a) 3 lần fail trong 3s rất dễ đến từ một đợt GC pause
   hoặc network blip, dẫn tới false positive và **flapping** failover 2 chiều (§4
   Anti-Patterns); (b) probe tải gấp 5 lần; (c) mỗi lần failover giả đều tốn GPU
   warm-up và có thể mất dữ liệu do RPO. Nếu muốn interval ngắn thì cần tăng threshold
   hoặc thêm circuit breaker / bước confirm của người.

3. **Outage 6 giờ, region chính mất dữ liệu vĩnh viễn, `docs_lost` nghĩa là gì?**
   `docs_lost = 4` nghĩa là có 4 document khách hàng đã upload thành công (đã nhận
   200 OK) nhưng **vĩnh viễn không còn** trong hệ thống. Tìm kiếm/RAG sẽ không trả về
   chúng, và khách không được báo trước. Với outage 6 giờ, con số này không tăng thêm
   (B nhận ingest mới sau cutover), nhưng cũng không bao giờ lấy lại được. Cần thông
   báo cho đúng các khách bị ảnh hưởng (liệt kê bằng `ingested_at > restored_latest_doc_ts`)
   để họ upload lại, và đây là lý do RPO phải được đo bằng **số doc**, không chỉ bằng giây.
