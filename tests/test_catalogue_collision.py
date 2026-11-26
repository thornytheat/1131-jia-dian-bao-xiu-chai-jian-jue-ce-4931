from dataclasses import replace

import pytest

from warrantycost.model_aliases.analysis import effective_versions, version_key
from warrantycost.model_aliases.contracts import CatalogVersion
from warrantycost.parts_authorization.analysis import make_versioned_plan, version_verdict
from warrantycost.parts_authorization.contracts import PartsRequest
from warrantycost.repair_cohorts import service as history
from warrantycost.pipeline import PATH, receipt_file, run
import warrantycost.pipeline as pipeline


def request(part, *, units=2, risk=.21, code="主机", distance=1, fee=3900, cost=25000, claim="X"):
    return PartsRequest.parse(dict(claim=claim, appliance="A", code=code, part=part, units=units,
                                   risk=risk, distance=distance, inspection_fee=fee, cost_cents=cost))


def entries(*rows):
    return tuple((index, row, None) for index, row in enumerate(rows, start=1))


def verdict(*rows, threshold=.46, distance_limit=6.5):
    return version_verdict(entries(*rows), threshold=threshold, distance_limit=distance_limit)


def test_concurrent_versions_replace_with_same_part_participate_in_stock():
    first=request("阀组-V3", code="RZ-500", risk=.20)
    second=request("阀组-V3", code="RZ-700", risk=.23)
    assert verdict(first,second)[0]=="可申请更换"
    plan, used, adjudication=make_versioned_plan([("X","A",entries(first,second))],{"阀组-V3":6},5)
    assert used=={"阀组-V3":2}
    assert plan[0]["reason"]=="批准换件" and plan[0]["parts_reserved"]==2
    assert adjudication[0]["verdict"]=="可申请更换"


def test_concurrent_versions_inspect_first_even_with_different_parts():
    first=request("控制板-Z0", risk=.62, fee=1800, cost=40000, code="RZ-504")
    second=request("驱动件-F7", risk=.60, fee=1800, cost=40000, code="RZ-504")
    state, chosen, notes=verdict(first,second)
    assert state=="需要先拆检" and chosen is not None
    assert [row["处置"] for row in notes]==["需要先拆检","需要先拆检"]
    plan, used, _=make_versioned_plan([("X","A",entries(first,second))],{},5)
    assert used=={}
    assert plan[0]["reason"]=="需要先拆检" and plan[0]["parts_reserved"]==0
    assert plan[0]["part"]=="—" and plan[0]["code"]=="—"


def test_concurrent_inspection_keeps_part_when_all_versions_agree():
    first=request("阀组-V3", risk=.60, fee=1800, cost=40000, code="RZ-500")
    second=request("阀组-V3", risk=.58, fee=1800, cost=40000, code="RZ-700")
    state, _, _=verdict(first,second)
    assert state=="需要先拆检"
    plan, _, _=make_versioned_plan([("X","A",entries(first,second))],{},5)
    assert plan[0]["part"]=="阀组-V3"


def test_concurrent_replace_with_differing_parts_holds_whole_claim():
    first=request("阀组-V3", risk=.20, code="RZ-500")
    second=request("控制板-Z0", risk=.18, code="RZ-504")
    state, chosen, notes=verdict(first,second)
    assert state=="整单挂起待核" and chosen is None
    plan, used, adjudication=make_versioned_plan([("X","A",entries(first,second))],{"阀组-V3":6},5)
    assert used=={} and plan==[]
    assert adjudication[0]["verdict"]=="整单挂起待核"


def test_concurrent_replace_with_differing_units_holds_whole_claim():
    first=request("阀组-V3", units=1, risk=.20, code="RZ-500")
    second=request("阀组-V3", units=2, risk=.20, code="RZ-700")
    assert verdict(first,second)[0]=="整单挂起待核"
    _, used, _=make_versioned_plan([("X","A",entries(first,second))],{"阀组-V3":6},5)
    assert used=={}


def test_concurrent_versions_with_disagreeing_recommendations_hold_whole_claim():
    swap=request("阀组-V3", risk=.20)
    inspect=request("阀组-V3", risk=.60, fee=1800, cost=40000)
    assert verdict(swap,inspect)[0]=="整单挂起待核"


def test_any_version_without_estimate_sends_whole_claim_to_review():
    known=request("阀组-V3", risk=.20)
    state, chosen, notes=version_verdict(((1, None, "机型或维修样本待核对"),(2, known, None)),
                                         threshold=.46, distance_limit=6.5)
    assert state=="整单待查" and chosen is None
    assert notes[0]["处置"]=="机型或维修样本待核对"
    _, used, _=make_versioned_plan([("X","A",(((1, None, "验证档期无样本"),(2, known, None)) ))],{"阀组-V3":6},5)
    assert used=={}


def test_any_version_outside_training_regime_sends_whole_claim_to_review():
    confident=request("阀组-V3", risk=.01, distance=11)
    normal=request("阀组-V3", risk=.20)
    state, chosen, _=verdict(confident,normal)
    assert state=="整单待查" and chosen is None


def test_single_effective_version_keeps_legacy_reasons():
    outlier=request("阀组-V3", risk=.01, distance=11)
    state, _, _=verdict(outlier)
    assert state=="工况超出历史"
    tight=request("阀组-V3", risk=.20, code="RZ-500")
    plan, used, _=make_versioned_plan([("X","A",entries(tight))],{"阀组-V3":1},5)
    assert plan[0]["reason"]=="备件可用量不足" and used=={}


def test_effective_versions_are_content_ordered_and_row_order_independent():
    document={"alias":"批号/5","master":"RZ-504","family":"干衣机","part":"驱动件-F7",
              "begin":10,"end":100,"service_minutes":66,"part_units":1}
    old=CatalogVersion.parse(document)
    newer=replace(old,part="控制板-Z0",begin=85,service_minutes=66)
    forward=effective_versions([old,newer],87)
    backward=effective_versions([newer,old],87)
    assert forward==backward
    assert len(forward["批号/5"])==2
    assert forward["批号/5"]==tuple(sorted(forward["批号/5"],key=version_key))


def test_verdict_does_not_depend_on_entries_order():
    first=request("阀组-V3", units=1, risk=.20, code="RZ-500")
    second=request("阀组-V3", units=2, risk=.30, code="RZ-700")
    forward=version_verdict(((1, first, None),(2, second, None)),threshold=.46,distance_limit=6.5)
    reversed_entries=version_verdict(((1, second, None),(2, first, None)),threshold=.46,distance_limit=6.5)
    assert forward[0]==reversed_entries[0]=="整单挂起待核"


def test_catalogue_row_order_does_not_change_authorization_or_stock(monkeypatch):
    original=receipt_file
    def reversed_catalogue(name):
        lines, digest=original(name)
        if name=="parts_compatibility.ndjson":
            return list(reversed(lines)), digest
        return lines, digest
    for capacity in (3,9,15):
        baseline=run(capacity)
        monkeypatch.setattr(pipeline,"receipt_file",reversed_catalogue)
        shuffled=run(capacity)
        assert baseline["rows"]==shuffled["rows"]
        assert baseline["stock_review"]==shuffled["stock_review"]
        assert baseline["metrics"]==shuffled["metrics"]
        assert [row for row in baseline["stages"][4]["records"] if "verdict" in row]==[
            row for row in shuffled["stages"][4]["records"] if "verdict" in row]


def test_collided_alias_repairs_are_adjudicated_not_guessed():
    snapshot=run(15)
    adjudication={row["claim"]:row for row in snapshot["stages"][4]["records"] if "verdict" in row}
    for claim in ("CS-087-005","CS-087-011","CS-087-017"):
        assert adjudication[claim]["appliance"]=="批号/5"
        assert adjudication[claim]["versions"]==2
        assert adjudication[claim]["verdict"]=="需要先拆检"
        assert adjudication[claim]["held"] is False
        part_names={note["兼容件"] for note in adjudication[claim]["notes"]}
        assert part_names=={"控制板-Z0","驱动件-F7"}
    rows={row["claim"]:row for row in snapshot["rows"]}
    assert rows["CS-087-005"]["reason"]=="需要先拆检"
    assert rows["CS-087-005"]["parts_reserved"]==0
    assert rows["CS-087-005"]["part"]=="—"


def test_late_writeback_never_enters_current_allowance_estimate():
    samples=history.inspect(receipt_file("completed_repair_costs.ndjson")[0]).records
    split=history.sample_split(samples,87,holdout_days=12)
    late=[row for row in samples if row.authorized_published>=87]
    assert late
    assert all(row.work_order not in {item.work_order for item in split.training+split.holdout} for row in late)
    assert {work for work,reason in split.excluded if reason=="NOT_VISIBLE_AT_DECISION"}>={row.work_order for row in late}
