

Áp dụng các quy tắc dưới đây khi viết hoặc sửa test, thực hiện bug fix, review thay đổi và kết luận hoàn thành công việc.



\*\*Mục tiêu:\*\* Mỗi test phải bảo vệ một hành vi có ý nghĩa và phát hiện được một sai lệch cụ thể. Mọi kết luận hoàn thành phải dựa trên bằng chứng thực thi của đúng phiên bản bàn giao.



\## 1. Xác định giá trị trước khi viết test



Trước khi thêm một test, phải trả lời được:



> “Khi xảy ra \[tình huống], nếu sản phẩm mắc \[lỗi cụ thể], test này sẽ fail tại \[assertion], vì hợp đồng yêu cầu \[kết quả đúng].”



Xác định rõ:



\- \*\*Hợp đồng:\*\* Hành vi được yêu cầu bởi specification, acceptance criteria, quy tắc nghiệp vụ hoặc invariant.

\- \*\*Tình huống:\*\* Input, trạng thái ban đầu và điều kiện cần để kích hoạt hành vi.

\- \*\*Kết quả quan sát được:\*\* Giá trị trả về, trạng thái, dữ liệu được ghi, sự kiện phát ra hoặc tương tác bắt buộc.

\- \*\*Lỗi cần phát hiện:\*\* Sai lệch cụ thể mà test phải bắt được.



Nếu chưa nêu được test bắt lỗi gì, hãy làm rõ hợp đồng trước. Không thêm test chỉ để tăng số lượng, coverage hoặc tạo cảm giác đã kiểm chứng.



Ưu tiên bổ sung assertion hoặc scenario vào test phù hợp đã có, nếu cách đó bảo vệ được hành vi mà không tạo trùng lặp.



\## 2. Loại bỏ test tự xác nhận và assertion không đủ mạnh



Không viết hoặc giữ nguyên các test có những đặc điểm sau:



| Dạng test cần tránh | Yêu cầu thay thế |

|---|---|

| So sánh một giá trị với chính nó; assertion luôn đúng | So sánh kết quả thực tế với expected được xác định độc lập |

| Cấu hình mock trả về X rồi chỉ kiểm tra mock trả về X | Gọi logic sản phẩm và kiểm tra hành vi mà logic đó phải thực hiện |

| Mock chính phần logic cần kiểm thử | Chạy implementation thật của phần đang được đánh giá; chỉ thay thế dependency phù hợp |

| Tính expected bằng chính hàm đang được test hoặc helper chứa cùng logic có thể sai | Lấy expected từ hợp đồng, dữ liệu chuẩn đã kiểm chứng hoặc phương pháp đối chiếu độc lập |

| Chép lại thuật toán sản phẩm trong test để tính expected | Dùng ví dụ đầu vào/đầu ra có căn cứ hoặc invariant độc lập |

| Chỉ kiểm tra `not null`, `true` hoặc “không ném exception”, trong khi hợp đồng quy định kết quả cụ thể | Assert đầy đủ các giá trị, trạng thái, số lượng hoặc thứ tự mà hợp đồng yêu cầu |

| Khóa private method, cấu trúc nội bộ hoặc chuỗi lời gọi không thuộc hợp đồng | Kiểm tra qua interface phù hợp và hành vi quan sát được |

| Nuốt exception, bỏ qua assertion hoặc chuyển failure thành success | Giữ failure hiển thị rõ và điều tra nguyên nhân |



\*\*Ngoại lệ phải có lý do:\*\* `not null`, “không throw”, kiểm tra số lần gọi hoặc kiểm tra thứ tự có thể hợp lệ khi chính nội dung đó thuộc hợp đồng. Không dùng chúng làm bằng chứng thay thế cho các yêu cầu khác chưa được kiểm tra.



\## 3. Dùng mock để kiểm soát dependency và xác minh hợp đồng



Mock, stub hoặc fake được phép dùng để kiểm soát dependency, tạo tình huống khó tái hiện hoặc quan sát tác động tại ranh giới hệ thống.



Khi assert lên mock:



\- Phải chỉ ra tương tác đó bảo vệ yêu cầu nào.

\- Phải bảo đảm tương tác được tạo ra bởi logic sản phẩm đang chạy.

\- Chỉ kiểm tra số lần gọi, tham số hoặc thứ tự nếu chúng có ý nghĩa đối với hợp đồng.



Ví dụ hợp lệ: “Khi validation thất bại, không được gọi thao tác lưu”; “một yêu cầu hợp lệ chỉ phát ra một sự kiện”.



Không coi việc mock hoạt động đúng cấu hình là bằng chứng rằng sản phẩm hoạt động đúng. Test dùng mock cũng không chứng minh dependency thật hoặc tích hợp thật đã được kiểm tra.



\## 4. Chứng minh test nhạy với lỗi cần phát hiện



Với hành vi đang bảo vệ, tự hỏi:



> “Nếu phần logic này bị vô hiệu hóa hoặc bị sửa sai theo một cách vi phạm hợp đồng, test nào sẽ fail, và fail ở đâu?”



Nếu thay đổi làm sai hành vi mà test vẫn GREEN, bộ kiểm tra chưa đủ để bảo vệ hành vi đó.



Khi cần xác minh thêm, dùng một thay đổi sai nhỏ, có chủ đích, chẳng hạn bỏ một bước ghi bắt buộc, đảo điều kiện lọc hoặc trả về một kết quả sai. Thay đổi này phải:



\- Vẫn cho phép code chạy đến hành vi cần kiểm tra.

\- Làm sai hợp đồng trong scenario đang xét.

\- Khiến test fail do sai hành vi, không chỉ do lỗi compile hoặc setup.

\- Được thực hiện trong phạm vi cô lập và hoàn nguyên sau khi kiểm tra.



Không yêu cầu mọi test phải bắt mọi lỗi. Không coi thay đổi tương đương về hành vi là một lỗi mà test buộc phải phát hiện.



Một regression test đã fail đúng nguyên nhân trên phiên bản còn bug là bằng chứng phù hợp về độ nhạy đối với bug đó; không cần thêm mutation chỉ để lặp lại cùng bằng chứng.



\## 5. Bug fix phải có bằng chứng RED → GREEN



Đối với task sửa bug, thực hiện theo trình tự:



1\. \*\*Xác định hành vi sai và kết quả đúng.\*\*

2\. \*\*Viết hoặc chọn regression test tái hiện tình huống gây lỗi.\*\*

3\. \*\*Chạy test trên phiên bản còn lỗi và quan sát RED đúng nguyên nhân.\*\*

4\. \*\*Sửa logic sản phẩm.\*\*

5\. \*\*Chạy lại cùng test để xác nhận GREEN, giữ nguyên hợp đồng và assertion đã chứng minh lỗi.\*\*

6\. \*\*Chạy các kiểm tra liên quan để phát hiện regression do bản sửa gây ra.\*\*



RED hợp lệ phải thể hiện lỗi đang xử lý. Lỗi import, thiếu dependency, fixture hỏng hoặc lỗi môi trường không liên quan không được tính là đã tái hiện bug.



Nếu test GREEN ngay từ đầu:



\- Kiểm tra lại fixture, assertion, đường thực thi và phiên bản sản phẩm đang chạy.

\- Xác minh test có thực sự đi qua logic chứa bug hay không.

\- Nếu bản sửa đã được áp dụng, chạy test trên phiên bản trước sửa trong checkout hoặc môi trường cô lập.

\- Tiếp tục điều tra khả thi trước khi kết luận thiếu bằng chứng.



\*\*Không dựng RED giả\*\* bằng cách sửa expected thành giá trị sai, phá setup, thêm assertion luôn fail hoặc cố tình tạo lỗi compile.



Nếu vẫn không thể quan sát RED đúng nguyên nhân, báo:



> \*\*BLOCKED — chưa chứng minh được regression trên phiên bản còn lỗi.\*\*



Nêu rõ điều đã thử, kết quả thực tế và bằng chứng còn thiếu. \*\*Không đánh dấu task là “đã sửa và được xác minh”.\*\* Dừng việc xác nhận hoàn tất; không tự biến thiếu bằng chứng thành thành công.



Phân biệt rõ:



\- \*\*Trạng thái test:\*\* Chỉ báo RED khi đã thực sự quan sát test thất bại.

\- \*\*Trạng thái gate:\*\* Chưa có bằng chứng bắt buộc thì gate chưa đạt, dù các test đã chạy đều GREEN.



Mutation chứng minh khả năng bắt một lỗi giả định không được trình bày thành bằng chứng đã tái hiện bug gốc.



Yêu cầu RED → GREEN này áp dụng cho sửa bug; không ép task refactor giữ nguyên hành vi hoặc test bổ sung cho hành vi đã đúng phải tạo một RED hình thức.



\## 6. Diễn giải RED và GREEN đúng phạm vi



\- \*\*RED là tín hiệu phải điều tra.\*\* Chỉ xem đó là bằng chứng của bug mục tiêu sau khi xác nhận nguyên nhân.

\- \*\*GREEN chứng minh những kiểm tra thực sự đã chạy đều đạt trong điều kiện đã ghi nhận.\*\* Không suy rộng thành “không còn lỗi”.

\- \*\*Một lượt GREEN không tự chứng minh test đủ mạnh.\*\* Chạy lại cùng một test yếu nhiều lần không khắc phục được assertion yếu.

\- Không retry cho đến khi có một lần pass rồi bỏ qua các lần fail trước.

\- Nếu kết quả thay đổi trên cùng code và điều kiện tương đương, phải điều tra tính bất định và báo giới hạn còn lại.

\- Coverage và số lượng test là thông tin hỗ trợ, không thay thế bằng chứng rằng test phát hiện được sai lệch có ý nghĩa.



\## 7. Nếu dùng review làm gate, phải có ít nhất hai lượt và hợp nhất kết quả



Đây là yêu cầu quy trình khi review được dùng làm điều kiện hoàn thành.



\- Thực hiện \*\*ít nhất hai lượt review\*\* với phạm vi rõ ràng trên cùng revision hoặc snapshot của ứng viên bàn giao.

\- Ưu tiên reviewer hoặc ngữ cảnh độc lập; không mô tả việc đọc lại trong cùng ngữ cảnh là review độc lập.

\- Mỗi finding phải nêu vị trí, hành vi bị ảnh hưởng, điều kiện gây lỗi và bằng chứng hoặc cách kiểm chứng.

\- Hợp nhất finding, loại trùng và xác minh từng finding trước khi kết luận.

\- Finding xuất hiện ở một lượt vẫn phải được xử lý, dù lượt khác không phát hiện.

\- Finding bị bác bỏ phải có lý do và bằng chứng; không quyết định bằng cách đếm số reviewer báo GREEN.

\- Nếu sửa code sau review, cập nhật snapshot và review lại phần bị ảnh hưởng. Không dùng kết luận của phiên bản cũ để xác nhận phần code mới.



Hai lần chạy test không được tính thành hai lượt review. Hai lượt review cũng không thay thế việc chạy verifier.



\## 8. Gate hoàn thành phải dựa trên verifier thực thi được



`build-verifier` là script, job hoặc chuỗi lệnh thực thi các tiêu chí đã xác định. Nếu có một Agent mang vai trò này, kết luận của Agent phải dựa trên kết quả thực thi, không chỉ dựa trên lời nhận xét.



Verifier phải:



\- Chạy trên \*\*đúng phiên bản bàn giao\*\*.

\- Kiểm tra các acceptance criteria có thể kiểm bằng máy, bao gồm hành vi liên quan; build thành công đơn thuần chưa đủ.

\- Sử dụng lệnh, cấu hình, dữ liệu và điều kiện chạy được ghi nhận để có thể tái lập.

\- Xác nhận các test bắt buộc thực sự được phát hiện và chạy.

\- Ghi rõ kết quả, exit code, failure, skip và những kiểm tra chưa chạy được.

\- Giữ failure hiển thị và không che kết quả bằng cơ chế tiếp tục khi lỗi.



Ưu tiên verifier hiện có của repository. Chỉ bổ sung kiểm tra cần thiết cho yêu cầu hoặc rủi ro cụ thể còn thiếu.



Không tự làm gate dễ hơn để đạt GREEN bằng cách xóa test, giảm assertion, đổi expected theo kết quả sai, nới threshold, thêm skip, che exit code hoặc bỏ kiểm tra bắt buộc.



Các nguồn bất định như thời gian, random, mạng và trạng thái dùng chung phải được kiểm soát khi phù hợp. Nếu chưa kiểm soát được, phải ghi rõ giới hạn; không cam kết kết quả tất định khi chưa có căn cứ.



\## 9. Báo cáo hoàn thành bằng bằng chứng



Báo cáo ngắn gọn theo cấu trúc:



| Nội dung | Bằng chứng cần có |

|---|---|

| Hành vi được bảo vệ | Hợp đồng và lỗi cụ thể mà test phát hiện |

| RED trước sửa | Test, phiên bản còn lỗi và nguyên nhân fail; hoặc ghi rõ chưa quan sát được / không áp dụng |

| GREEN sau sửa | Cùng test trên phiên bản đã sửa và kết quả thực tế |

| Kiểm tra liên quan | Lệnh đã chạy, phạm vi, kết quả và các kiểm tra chưa thực hiện |

| Review gate | Revision/snapshot, các lượt review, finding đã hợp nhất và trạng thái xử lý |

| Verifier gate | Lệnh hoặc job, phiên bản được kiểm tra, exit code và kết quả |

| Kết luận | `VERIFIED`, `FAILED` hoặc `BLOCKED`, kèm lý do |



Chỉ dùng \*\*VERIFIED\*\* khi mọi bằng chứng bắt buộc của task đều đầy đủ, verifier đạt và không còn finding chặn hoàn thành.



Dùng \*\*FAILED\*\* khi đã thực thi kiểm tra và xác nhận yêu cầu chưa đạt. Dùng \*\*BLOCKED\*\* khi thiếu điều kiện hoặc bằng chứng bắt buộc để xác minh.



\*\*Không biến “chưa phát hiện lỗi” thành “đã chứng minh đúng”; không biến “chưa kiểm tra được” thành “đã hoàn thành”.\*\*

