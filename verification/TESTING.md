# Kiểm thử bản hoàn thiện 2026-11-15

Đây là bằng chứng kiểm thử mã, không phải báo cáo môn học hoặc tuyên bố đã chạy
trên máy Ubuntu/WSL của người dùng.

## Môi trường

- ns-3.35 với hai thay đổi trong `patches/ns335-wifi-fixes.patch`.
- Linux, g++ 14.2.0, Python 3.12.14, matplotlib 3.10.8, scipy 1.17.0.
- Biên dịch `-O2`, **NS3_ASSERT_ENABLE được bật**. Không tắt assertion để tránh lỗi.
- `-include cstdint` xử lý tương thích compiler mới trong môi trường này;
  runner trên máy người dùng giữ nguyên cấu hình build đang có.
- Chi tiết: `environment.json`; hash ba file lõi và cấu hình: `final-matrix/experiment.json`.
- SHA256 C++ đã kiểm thử: `254fa75517ac73f1a46b59c439bbec67e25e59e26576c9a59ac305f7eddf49a2`.

## Ma trận đầy đủ

Đã chạy bằng chính `finish.sh`, không dựng số liệu minh họa:

```bash
bash finish.sh verification/final-matrix /duong/dan/ns-3.35
```

TCP/UDP × 5/10/20 trạm × run 1..5, seed 12345; nguồn bắt đầu giây 3,
gửi 30 giây, xả 10 giây. Kết quả **30/30 lượt thành công, 0 lỗi**.

- Mỗi trạm có gói IP và byte ứng dụng đến AP trong cả 30 lượt.
- Cả STA và AP đều ghi nhận liên kết trước lúc bắt đầu gửi; không có trạng thái
  liên kết lệch nhau ở thời điểm quan sát này.
- Trong 15 lượt TCP, toàn bộ byte socket đã chấp nhận được nhận ở ứng dụng trước
  cutoff. Điều này không có nghĩa TCP đạt đủ tốc độ yêu cầu 1 Mbps/trạm.
- Bộ kiểm tra đã đối chiếu bộ đếm từng trạm/tổng mạng, công thức PDR/mất gói/
  thông lượng/goodput và trung bình độ trễ có trọng số.
- TCP/UDP cùng N/run có cùng tọa độ đầu/cuối trong sai số < 1e-8 m;
  sai khác lớn nhất đo được khoảng 1e-10 m. Các dải RNG stream di chuyển tách riêng.
- `aggregate.csv`, `paired_differences.csv`, `comparison.png/.pdf` được tạo thành công.
  Đã xem ảnh để kiểm tra nhãn và bố cục.

Dữ liệu/log từng lượt ở `final-matrix/`. Bản cũ trước khi sửa liên kết AP không
được ghép vào ma trận này. Năm mẫu mỗi cấu hình chỉ hỗ trợ đánh giá thăm dò;
CI Student-t là xấp xỉ và không bảo đảm kết luận tổng quát.

## Các kiểm tra bổ sung đã đạt

| Kiểm tra | Kết quả / bằng chứng |
|---|---|
| Sửa lõi từ mã 3.35 gốc | Áp dụng đúng ba file; có backup và diff; `patch-checks.txt` |
| Máy đã có bản sửa va chạm | Chỉ thêm bản sửa liên kết AP còn thiếu |
| Chạy bộ sửa lần nữa | Không sửa chồng; trạng thái AP sửa dở bị từ chối trước khi ghi |
| Tăng xả 10 → 20 s, N=20/run=3, cả TCP và UDP | Tx/Rx IP, PDR, delay, byte ứng dụng và goodput cửa sổ gửi không đổi; `drain-check.json` và `drain20-n20-r3/` |
| Không nhận gói, UDP 1 trạm/công suất -100 dBm | PDR=0, mất=100%, delay trống; lượt hợp lệ có cảnh báo; phân tích/vẽ vẫn chạy; `zero-rx/` |
| Dừng thật bằng SIGINT giữa lượt thứ hai | Lượt đầu được lưu; tiếp tục giữ nguyên file lượt đầu và hoàn thành lượt thứ hai; `tool-checks.txt` |
| Tiếp tục nhưng đổi cleanup | Từ chối trộn cấu hình vào thư mục cũ |
| Giới hạn thời gian chạy hết hạn | Đánh dấu lỗi, không biến thành mẫu PDR=0 |
| Sửa dữ liệu PDR thành 101% | Analyzer từ chối dữ liệu hỏng |
| NetAnim tùy chọn | Xuất XML và parse thành công; `visual-xml/`; chưa kiểm thử GUI trên WSL |

Kiểm tra xả áp dụng cho hai lượt đã nêu, không chứng minh 10 giây luôn đủ với mọi
tham số tùy chỉnh. Throughput toàn cửa sổ sẽ đổi khi tăng mẫu số duration+cleanup,
dù số byte nhận không đổi; vì vậy phép kiểm tra so sánh bộ đếm và goodput trong
cùng cửa sổ gửi.

## Khi chạy trên máy của bạn

Dùng `bash finish.sh`, đọc bộ kết quả mới ở `results/final-01/`. Giữ `pilot-02`
làm lịch sử chẩn đoán; không nộp dữ liệu trong `verification/` với mô tả là bạn đã
tự chạy chúng. Nếu chương trình báo lỗi, log/cấu hình/hash được giữ để tái hiện.
