"""人工复检工单：领取与结果凭据属于同一数据库事务。"""
import json
from orchard_grade.contracts.batches import ContractError
from orchard_grade.inspection.receipts import record_receipt

class InspectionWork:
    def __init__(self, repository):
        self.repository=repository
        with repository.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS inspection_work(work_id TEXT PRIMARY KEY,lot_id TEXT NOT NULL,evidence_hash TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,worker TEXT NOT NULL DEFAULT '',receipt_id TEXT)")

    def open(self,work_id,lot_id,reason):
        if not work_id or not reason.strip(): raise ContractError("工单及缘由不能为空")
        with self.repository.transaction() as c:
            row=c.execute("SELECT fingerprint FROM records WHERE record_id=?",(lot_id,)).fetchone()
            if row is None: raise ContractError("果批不存在")
            old=c.execute("SELECT * FROM inspection_work WHERE work_id=?",(work_id,)).fetchone()
            if old:
                if (old['lot_id'],old['evidence_hash'],old['reason'])!=(lot_id,row[0],reason): raise ContractError("工单不能替换果批或依据")
                return dict(old)
            c.execute("INSERT INTO inspection_work(work_id,lot_id,evidence_hash,reason,status) VALUES(?,?,?,?,'pending')",(work_id,lot_id,row[0],reason))
            return dict(c.execute("SELECT * FROM inspection_work WHERE work_id=?",(work_id,)).fetchone())

    def claim(self,work_id,worker):
        if not worker.strip(): raise ContractError("领取人不能为空")
        with self.repository.transaction() as c:
            row=c.execute("SELECT * FROM inspection_work WHERE work_id=?",(work_id,)).fetchone()
            if row is None or (row['status']!='pending' and (row['status'],row['worker'])!=('claimed',worker)):
                raise ContractError("工单不能领取")
            c.execute("UPDATE inspection_work SET status='claimed',worker=? WHERE work_id=?",(worker,work_id))

    def complete(self,work_id,worker,receipt):
        with self.repository.transaction() as c:
            row=c.execute("SELECT * FROM inspection_work WHERE work_id=?",(work_id,)).fetchone()
            if row is None or row['worker']!=worker or receipt.lot_id!=row['lot_id']: raise ContractError("工单领取人与果批不匹配")
            if row['status']=='completed':
                if row['receipt_id']!=receipt.receipt_id: raise ContractError("已完成结果不能替换")
                record_receipt(c,receipt);return
            current=c.execute("SELECT fingerprint FROM records WHERE record_id=?",(row['lot_id'],)).fetchone()
            if row['status']!='claimed' or current[0]!=row['evidence_hash']: raise ContractError("果批在复检期间已更正")
            payload=json.loads(c.execute("SELECT payload_json FROM records WHERE record_id=?",(row['lot_id'],)).fetchone()[0])
            for key in ('origin_code','line_id','camera_batch'):
                if str(payload.get(key,''))!=getattr(receipt,key):raise ContractError("复检凭据来源不匹配")
            record_receipt(c,receipt)
            c.execute("UPDATE inspection_work SET status='completed',receipt_id=? WHERE work_id=?",(receipt.receipt_id,work_id))

    def inspect(self):
        with self.repository.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM inspection_work ORDER BY work_id")]
