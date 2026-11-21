# Đánh giá chất lượng PhosphoChat (thử nghiệm, tháng 11/2026)

## Tóm tắt

Chúng tôi kiểm thử PhosphoChat trên 5 bài báo khoa học thật và 21 câu hỏi chat, sau đó cải tiến
hệ thống qua 11 bước có đo đạc. Ở lần chạy cuối, mọi điểm số đều do một mô hình chấm điểm độc lập
(Claude Sonnet 4.6) đưa ra:

- **Các trang wiki viết từ bài báo trung thành với nguồn.** Chỉ 2 trên 412 câu được kiểm tra
  (0,5%) nói quá những gì bài báo viết, giảm từ 2,0% ở lần chạy đầu. Mọi đoạn văn đều có trích dẫn.
- **Wiki giữ được phần lớn nội dung quan trọng của mỗi bài.** 86% kết quả chính của các bài báo
  có trong wiki (bao phủ một phần tính là một nửa), tăng từ 66%.
- **Câu trả lời chat đúng và có căn cứ.** Trên thang 0–2: độ đúng 1,95, độ bám nguồn 2,0, trích
  dẫn 1,75, độ đầy đủ 1,65. Không câu trả lời nào trích dẫn một bản ghi cơ sở dữ liệu không tồn
  tại, và câu hỏi ngoài phạm vi bị từ chối đúng.
- **Điểm yếu chính là độ đầy đủ.** Câu trả lời đúng nhưng đôi khi bỏ sót các ý phụ. Câu hỏi dùng
  từ khác với wiki ("xử lý mẫu" so với trang về "thiếu máu lạnh" – cold ischemia) phụ thuộc vào
  việc mô hình có tìm lại hay không.

## 1. Đối tượng kiểm thử

PhosphoChat là trợ lý nghiên cứu về sinh học kinase–cơ chất, gồm hai phần:

- **Capture (thu nhận):** người dùng thêm một bài báo, ứng dụng viết các trang wiki từ bài đó kèm
  trích dẫn. Các phát biểu kinase → cơ chất được ghi lại thành claim và đối chiếu với cơ sở dữ liệu.
- **Chat:** trả lời câu hỏi từ wiki của người dùng và từ một đồ thị Neo4j hợp nhất PhosphoSIGNOR
  (cùng PhosphoSitePlus, EPSD, iPTMNet) và KiNet. Mỗi câu trả lời có tối đa ba phần: wiki nói gì,
  cơ sở dữ liệu nói gì, và hai bên khớp nhau thế nào.

**Bài báo.** Năm bài từ bộ dữ liệu của môn học, chọn để bao quát nhiều loại nội dung:

| Tệp | Chủ đề |
|---|---|
| `1495.pdf` | Thiếu máu lạnh (cold ischemia) và tín hiệu phosphotyrosine trong mẫu u phẫu thuật |
| `nature18003.pdf` | Proteogenomics ung thư vú (CPTAC) |
| `s41467-025-59779-y.pdf` | benchmarKIN: đánh giá các phương pháp suy luận hoạt tính kinase |
| `mcdermott-2020-…ovarian-hgsc….pdf` | Đặc điểm proteogenomic của ung thư buồng trứng thanh dịch độ cao (HGSC) |
| `1-s2.0-S2666979X25001077-main.pdf` | PROTsi, chỉ số "tính gốc" (stemness) dựa trên protein trên nhiều loại ung thư |

**Câu hỏi.** 21 câu chia thành sáu nhóm (danh sách đầy đủ ở `backend/eval/questions.yaml`):

| Nhóm | Số câu | Kiểm tra điều gì | Ví dụ |
|---|---|---|---|
| Cơ sở dữ liệu | 6 | Danh sách và dữ kiện từ cơ sở dữ liệu kinase | "Which kinases phosphorylate RB1?" |
| Bài báo | 8 | Kết quả cụ thể của một bài | "What is PROTsi, and what did my sources use it for?" |
| Liên bài báo | 2 | Tổng hợp từ nhiều bài | "How is kinase activity inferred from phosphoproteomics data, and what limits it?" |
| Claim | 2 | Claim đã ghi so với cơ sở dữ liệu | "What do my sources and the databases each say about CDK1 phosphorylating RB1?" |
| Phủ định | 2 | Chủ đề wiki không có; câu trả lời đúng phải nói rõ là không có | "What do my sources say about LRRK2 phosphorylation of Rab GTPases?" |
| Ngoài phạm vi | 1 | Phải bị từ chối | "Plan a three-day sightseeing trip to Da Nang for me." |

**Mô hình.** Tất cả được gọi qua CLIProxyAPI, dịch vụ cung cấp các mô hình Google Antigravity
miễn phí nhưng có hạn mức theo tuần.

| Vai trò | Mô hình |
|---|---|
| Đọc bài báo và viết trang | `gemini-3.8-flash-high` |
| Chat và kiểm tra chủ đề | `gemini-3-flash` |
| Giám khảo cuối | `claude-sonnet-4-6`, khác họ mô hình với mô hình viết |
| Giám khảo thường kỳ, trong vòng cải tiến | `gemini-3.8-flash-high` |

## 2. Cách đo

**Kiểm tra bằng code** không tốn chi phí và chạy ở mọi bước:

- **Capture:** tỉ lệ văn bản thực sự được đọc, số đoạn không có trích dẫn, tính hợp lệ của
  frontmatter.
- **Chat:** mọi SIGNOR ID, PMID, trang wiki và nguồn được trích có tồn tại không; định dạng liên
  kết hỏng; độ chính xác (precision: các kinase được nêu có thật trong cơ sở dữ liệu không) và độ
  phủ top-5 (top-5 recall: các kinase có bằng chứng mạnh nhất có được nêu không); câu ngoài phạm vi
  có bị từ chối không.

**Mô hình giám khảo** so sánh đầu ra với nguồn.

- **Capture:** mọi câu trên các trang được viết đều được đối chiếu với toàn văn bài báo, và gắn
  nhãn có căn cứ hoặc không có căn cứ (ví dụ "nói quá"). Năm kết quả chính được rút ra từ mỗi bài và
  đánh dấu là có trong wiki, có một phần, hoặc thiếu.
- **Chat:** mỗi câu trả lời được chấm 0–2 về độ đúng, độ đầy đủ, độ bám nguồn và trích dẫn, so với
  các ý chính soạn sẵn cho từng câu. Giám khảo được xem đúng ngữ cảnh truy xuất mà mô hình chat đã
  thấy.

**Chọn giám khảo.** Trước khi dùng giám khảo rẻ hơn để so sánh thường kỳ, nó được đối chiếu với
Claude: trên các câu chat, nó chấm lệch Claude không quá 1 điểm trong 94% trường hợp, và cho cùng
điểm trong 73%. Nó chỉ được dùng để so sánh các phiên bản của ứng dụng. Mọi con số cuối cùng đều
từ Claude.

**Nhiễu.** Cùng một câu trả lời có thể được 2 điểm ở lần này và 1 điểm ở lần sau. Thay đổi 1 điểm
trên một câu hỏi được coi là nhiễu và được chạy lại trước khi kết luận.

## 3. Kết quả

### 3.1 Capture

| Bài báo | Văn bản đã đọc | Số trang | Số câu kiểm tra | Không có căn cứ | Kết quả chính (có / một phần / không) | Số request của mô hình viết |
|---|---|---|---|---|---|---|
| 1495 (thiếu máu lạnh) | 72% | 3 | 59 | 0 | 5 / 0 / 0 | 5 |
| nature18003 (ung thư vú) | 100% | 5 | 75 | 0 | 3 / 2 / 0 | 11 |
| s41467 (benchmarKIN) | 58% | 4 | 68 | 0 | 3 / 1 / 1 | 14 |
| McDermott (HGSC buồng trứng) | 46% | 5 | 48 | 0 | 4 / 1 / 0 | 16 |
| PROTsi | 45% | 6 | 162 | 2 | 4 / 1 / 0 | 8 |
| **Tổng** | | **23** | **412** | **2 (0,5%)** | **19 / 5 / 1 (86%)** | 10,8 mỗi bài |

- **Đọc dưới 100% là có chủ đích.** Các phần tài liệu tham khảo, phương pháp, lời cảm ơn, tài trợ
  và phụ lục được bỏ qua để mô hình đọc tập trung vào phần kết quả.
- **Cả hai câu không có căn cứ đều là nói quá**, không phải bịa. Một trang viết hoạt tính Haspin tăng
  ở nhiều loại u, trong khi bài báo nói "đặc biệt" ở một số loại. Câu còn lại khái quát hóa một kết
  quả về SNRPB trong GBM.
- **Đoạn không có trích dẫn: 0** trên cả 23 trang.
- **So với lần chạy đầu** (cùng bài báo, cùng giám khảo): tỉ lệ câu không có căn cứ giảm từ 2,0%
  xuống 0,5%, tỉ lệ bao phủ kết quả chính tăng từ 0,66 lên 0,86, số trang mỗi bài tăng từ 4,0 lên 4,6.

### 3.2 Chat

Điểm giám khảo theo nhóm câu hỏi (0–2):

| Nhóm | Số câu | Độ đúng | Độ đầy đủ | Độ bám nguồn | Trích dẫn |
|---|---|---|---|---|---|
| Cơ sở dữ liệu | 6 | 2,0 | 1,83 | 2,0 | 2,0 |
| Bài báo | 8 | 2,0 | 1,62 | 2,0 | 2,0 |
| Liên bài báo | 2 | 2,0 | 1,0 | 2,0 | 1,5 |
| Claim | 2 | 2,0 | 2,0 | 2,0 | 1,0 |
| Phủ định | 2 | 1,5 | 1,5 | 2,0 | 1,0 |
| **Cả 20 câu được trả lời** | 20 | **1,95** | **1,65** | **2,0** | **1,75** |

Câu ngoài phạm vi: bị từ chối đúng, không cần gọi mô hình chat.

Kiểm tra bằng code:

| Tiêu chí | Lần chạy đầu | Lần cuối |
|---|---|---|
| Câu trả lời trích bản ghi hoặc trang không tồn tại | 1 / 17 | **0 / 21** |
| Liên kết sai định dạng mỗi câu trả lời | chưa đo | **0** |
| Precision cơ sở dữ liệu (kinase được nêu có thật trong cơ sở dữ liệu) | 0,95 | 0,94 |
| Top-5 recall cơ sở dữ liệu (kinase có bằng chứng mạnh nhất được nêu) | 0,32 | **0,52** |
| Token đầu vào mỗi câu trả lời | khoảng 7.300 | **khoảng 3.600** |
| Thời gian mỗi câu trả lời | 0,5–5 giây | 1,4–4,9 giây |

**Những chỗ bị trừ điểm:**

- **Độ đầy đủ 1, ở 6 câu.** Câu trả lời đúng nhưng bỏ sót ý chính phụ. Ví dụ, P8 nêu đủ 62
  phosphosite và hai kinase nhưng không nêu chỉ số đường tín hiệu 58%.
- **Danh sách cơ sở dữ liệu.** Câu trả lời hiển thị 12 kinase có bằng chứng mạnh nhất trên tối đa
  77, nên một số tên được kỳ vọng (ví dụ CDK5 với TP53) không có mặt. Câu trả lời có nêu tổng số và
  đề nghị xem danh sách đầy đủ.
- **N1 (LRRK2), độ đúng 1.** Wiki không có gì về chủ đề này và câu trả lời có nói vậy, nhưng lại mở
  đầu bằng dữ kiện từ cơ sở dữ liệu. Giám khảo muốn thông báo "nguồn của bạn không có" được đặt
  trước.
- **Trích dẫn 0 ở C1 và N2.** Cả hai đều là câu trả lời "không tìm thấy" đúng. Giám khảo trừ điểm
  vì thiếu trích dẫn, dù chính ghi chú của nó nói không cần trích dẫn. Chúng tôi xem đây là giám
  khảo quá khắt khe, không phải lỗi của ứng dụng.

## 4. Vòng cải tiến đã thay đổi gì

Mỗi bước chỉ thay đổi một điểm, được đo so với lần chạy tốt gần nhất, và chỉ được giữ nếu chỉ số
mục tiêu cải thiện và không chỉ số nào xấu đi quá mức nhiễu. Cả 11 bước đều được giữ.

| # | Thay đổi | Hiệu quả đo được |
|---|---|---|
| 1 | Ngữ cảnh chat: liên kết trực tiếp lên trước; danh sách cơ sở dữ liệu 6 → 12 mục kèm tổng số | Độ đúng 1,80 → 1,95; top-5 recall 0,32 → 0,48 |
| 2 | Kiểm tra bằng code mọi trích dẫn sau mỗi câu trả lời, kèm ghi chú cho trích dẫn lạ | Bắt được 3/3 trích dẫn sai, không báo nhầm |
| 3 | Quy tắc định dạng trích dẫn và tự sửa các lỗi liên kết thường gặp | Câu trả lời có liên kết hỏng 3 → 1 |
| 4 | Mô hình đọc lấy mọi kết quả riêng biệt, giữ nguyên số liệu và tên | Ý chính giữ được từ nature18003: 4–6/8 → 8/8 |
| 5 | Ghi chú chuyển cho mô hình viết: 7.000 → 14.000 ký tự | Câu không có căn cứ 2,1% → 0; bao phủ kết quả chính 0,85 → 1,0 (2 bài) |
| 6 | Trích đoạn wiki xếp theo từ của câu hỏi thay vì lấy các dòng đầu trang | Ý chính có trong ngữ cảnh 3 → 6/9 |
| 7 | Trang xếp theo mức bao phủ câu hỏi; wiki tối đa nửa ngữ cảnh | Phần cơ sở dữ liệu có mặt 9/9; token đầu vào −33% |
| 8 | "Tìm lại trong wiki bằng từ khác trước khi nói không có gì" | Câu diễn đạt khác X2: 0,33 → 1,0; câu phủ định không đổi |
| 9 | Bộ kiểm tra chủ đề chấp nhận thuật ngữ nó không biết (ví dụ PROTsi) | Đúng 11/11 câu thử, hai lần |
| 10 | Thay trích dẫn ví dụ trong prompt chat bằng chỗ trống (mô hình đã chép lại nó) | Số câu chép ID ví dụ 2 → 0 |
| 11 | Lịch sử của mô hình viết: không còn mất nội dung trang khi tin nhắn cũ bị rút gọn | Số lần chạy chạm giới hạn 40 request: 3/4 → 0/4 |

**Chi phí prompt.** Một lần thiết kế lại trước vòng cải tiến đã giảm phần prompt cố định gửi kèm
mỗi request (đếm theo token của Groq):

| Agent | Trước | Sau |
|---|---|---|
| Capture | 5.026 | 1.101 |
| Lint | 4.688 | 1.227 |
| Digest | 4.597 | 1.092 |
| Chat | 4.061 | 869 |

## 5. Các lỗi về độ bền đã phát hiện và sửa

Kiểm thử trên bài báo thật tìm ra những lỗi mà unit test không bắt được:

- **Vòng lặp của mô hình viết (bước 11).** Khi tin nhắn cũ bị rút gọn cho vừa giới hạn lịch sử,
  phần bị cắt mất vĩnh viễn, vì PydanticAI lưu lịch sử đã rút gọn làm lịch sử thật của lượt chạy.
  Một trang đã bị cắt thì không bao giờ được thấy đầy đủ lại, nên mô hình đọc lại cùng các trang tới
  15 lần cho đến khi chạm giới hạn request. Vì lỗi này, hai trong năm bài cần chạy lại lần hai ở
  lần chạy cuối; lỗi được sửa sau đó và đã kiểm thử lại.
- **Cập nhật index song song** dùng chung một tệp tạm và có thể làm mất dòng index. Nay đã có khóa
  và tệp tạm riêng.
- **Lời gọi tool sai định dạng của Gemini** nay được thử lại thay vì làm hỏng job. Mỗi trang chỉ
  được viết tối đa 3 lần mỗi lượt chạy, để chặn vòng lặp viết lại.
- **YAML không hợp lệ trong frontmatter**, do trích dẫn đặt sau phần summary, nay được tự sửa.

## 6. Hạn chế

- **Quy mô thử nghiệm nhỏ.** 5 bài báo và 21 câu hỏi đủ để tìm lỗi và so sánh phiên bản, nhưng
  chưa đủ để cho tỉ lệ chính xác. Một câu hỏi chiếm khoảng 5% điểm chat.
- **Câu hỏi diễn đạt khác** phụ thuộc vào việc mô hình chat có chọn tìm lại hay không. Tìm kiếm wiki
  khớp theo từ, không theo nghĩa; tìm kiếm ngữ nghĩa (semantic search) sẽ khắc phục điều này.
- **Kiểm tra trích dẫn.** Nó xác nhận PMID được trích có tồn tại, chưa xác nhận PMID đó thuộc đúng
  liên kết được nói tới. Một câu trả lời đã gắn PMID của các liên kết lân cận.
- **Lint, digest và sync** không nằm trong lần chạy cuối. Ở các lần trước, lint và digest đạt mọi
  kiểm tra bằng code, còn sync đạt sau khi sửa lỗi lời gọi tool.
- **Nhiễu của giám khảo.** Chênh lệch 1 điểm trên một câu hỏi nằm trong mức nhiễu.
- **Phụ thuộc mô hình.** Kết quả trên ứng với các mô hình đã nêu. Mô hình nhỏ hơn cho kết quả kém
  hơn: khi làm mô hình đọc, `gemini-3-flash` giữ 8 kết quả trong khi `gemini-3.8-flash-high` giữ
  21, và nó không phù hợp để viết trang.

## 7. Tái lập

Bộ công cụ đánh giá nằm trong `backend/eval/`. Nhật ký đầy đủ của mọi lần chạy và quyết định ở
`backend/eval/PROGRESS.md`, kết quả thô ở `backend/eval/results/`.

```bash
cd backend
.venv/Scripts/python -m eval.run_capture --job-model compat:cliproxy:gemini-3.8-flash-high --vault eval-final --papers <files>
.venv/Scripts/python -m eval.score_capture --vault eval-final --judge-model compat:cliproxy:claude-sonnet-4-6
.venv/Scripts/python -m eval.run_chat --chat-model compat:cliproxy:gemini-3-flash --vault eval-final --tag final2
.venv/Scripts/python -m eval.score_chat --file chat-eval-final-cliproxy-gemini-3-flash-final2.json --judge-model compat:cliproxy:claude-sonnet-4-6
.venv/Scripts/python -m eval.compare --base <score file> --new <score file>
```

Tham số `--tier code` ở cả hai bộ chấm chỉ chạy các kiểm tra bằng code miễn phí. Backend có 210
test chạy offline (`pytest -q`).
