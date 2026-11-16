果品智能分选质量闭环平台

复检与出库：可通过 Application.receive_inspection 记录人工复检凭据，
包括果批、产地、分选线、相机批次、机判级别、复检级别、抽样缘由及时间；
同一凭据重放不重复入库，冲突凭据会被拒绝。Application.dispatch 只给已放行
果批分配一次出库目的地，优先采用最新人工复检，否则沿用机器判级，
已有出库分配不会被后续证据静默改写。记录可通过仓储的 inspection_rows
和 queue_rows 读取，均使用运行命令的同一数据库。

运行验收：
python3 -m orchard_grade run --fixture fixtures/acceptance.json --database /tmp/orchard_grade.sqlite3 --output /tmp/orchard_grade-output

核验：
python3 -m orchard_grade verify --database /tmp/orchard_grade.sqlite3 --output /tmp/orchard_grade-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.grading.test_decision_table -v
python3 -m unittest tests.disputes.test_dispute_workflow -v

构建检查：
python3 -m compileall -q orchard_grade tests

inspection_work.open 手工登记复检工单，claim 领取，complete 在同一事务保存复检凭据并结束工单；果批资料更正后须重新确认，已完成工单不可重复派发。

争议分析与放行闭环（Application.disputes / orchard_grade.disputes）：

* 分析：disputes.analyse(lot_id) 为果批生成并保存争议分析。它引用复检当时的
  传感器快照与机器判断（inspection_evidence），比较有效人工结论，并把差异按
  产地、品类、设备条件（line/camera）分层，报告争议率、95% 区间、抽样方式、
  抽样偏差、代表性差距与综合不确定性。没有有效人工凭据的果批只会得到
  unverified 状态，绝不输出合格/不合格；结论是否可外推取决于抽样是否随机且
  样本量是否足够，而不是默认外推。
* 复核工作：提高争议分层或存在未决争议时，分析会自动生成可领取的
  inspection_work（dispute_sampling/evidence_withdrawn/reinspect/follow_up），
  claim 领取，complete 时同一事务写入复检凭据与传感器快照。已完成的证据版本
  （review_key）不会被重复派发；证据变化会重评受影响果批并关闭不再需要的工单。
* 放行约束：Application.release 与 dispatch 都会检查争议状态。存在未完成
  复核、撤回的人工证据或主管暂缓时拒绝；未抽检果批必须先由主管给出
  release_unverified/release_followup 处置，不能默认放行。
* 主管处置：disputes.record_disposition(lot_id, decision, actor, note) 支持
  accept_machine / accept_human / hold / reinspect / release_unverified /
  release_followup。处置绑定其所依据的证据摘要（input_hash），证据变化后处置
  自动失效，需要重新判断。
* 补报/更正/撤回：disputes.revise_inspection(InspectionRevision) 追加而不覆盖
  人工结论，并立即重评该果批及其同产地/品类/设备的果批；旧工单转为 superseded。
* 已放行事实：出库分配与放行记录只读保留。后续证据变化会产生
  post_release.armed，必须通过 disputes.record_followup 登记处置，不能静默
  改写目的地。
* 追溯：disputes.trace(lot_id) 汇总凭据、有效结论、证据快照、工单、历次分析、
  主管处置、出库与事件链；disputes.board() 给主管看板列出待复核与未闭环放行。

命令行：
python3 -m orchard_grade dispute analyse --database DB --lot LOT
python3 -m orchard_grade dispute board --database DB
python3 -m orchard_grade dispute trace --database DB --lot LOT
python3 -m orchard_grade dispute open-review --database DB --lot LOT [--force]
python3 -m orchard_grade dispute claim --database DB --work WORK --worker NAME
python3 -m orchard_grade dispute complete --database DB --work WORK --worker NAME --receipt receipt.json [--revision revision.json]
python3 -m orchard_grade dispute disposition --database DB --lot LOT --decision accept_human --actor NAME [--note TEXT]
python3 -m orchard_grade dispute revise --database DB --revision revision.json
python3 -m orchard_grade dispute followup --database DB --lot LOT --action notify_customer --actor NAME
python3 -m orchard_grade release --database DB --lot LOT
python3 -m orchard_grade dispatch --database DB --lot LOT
