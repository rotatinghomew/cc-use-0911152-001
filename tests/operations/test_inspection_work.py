from dataclasses import replace
from tests.operations.test_packhouse_queue import PackhouseQueueTests
from orchard_grade.application import Application
from orchard_grade.contracts.batches import ContractError, DomainRecord

class InspectionWorkTests(PackhouseQueueTests):
    def test_claim_conflict_completion_and_restart(self):
        work=self.application.inspection_work
        work.open('w','lot-1','人工争议');work.claim('w','甲');work.claim('w','甲')
        with self.assertRaises(ContractError):work.claim('w','乙')
        with self.assertRaises(ContractError):work.complete('w','甲',replace(self.receipt(),origin_code='other'))
        self.assertEqual(work.inspect()[0]['status'],'claimed')
        work.complete('w','甲',self.receipt());work.complete('w','甲',self.receipt())
        with self.assertRaises(ContractError):work.complete('w','甲',self.receipt('other'))
        self.assertEqual(Application(self.database).inspection_work.inspect()[0]['status'],'completed')
        self.assertEqual(len(self.application.repository.inspection_rows('lot-1')),1)

    def test_changed_lot_does_not_consume_receipt(self):
        work=self.application.inspection_work;work.open('w','lot-1','复检');work.claim('w','甲')
        changed=DomainRecord(self.record.record_id,dict(self.record.payload,sweetness_brix=17),self.record.state,2,self.record.occurred_at,self.record.source)
        self.application.repository.save_record(changed,expected_version=1)
        with self.assertRaises(ContractError):work.complete('w','甲',self.receipt())
        self.assertEqual(self.application.repository.inspection_rows('lot-1'),[])
        self.assertEqual(work.inspect()[0]['status'],'claimed')
