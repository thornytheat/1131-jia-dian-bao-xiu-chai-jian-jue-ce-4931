from collections import Counter
from .contracts import PartsRequest


def economic_choice(row: PartsRequest, *, distance_limit, threshold):
    expected_loss = row.cost_cents * row.risk
    avoidable = expected_loss-row.inspection_fee
    if row.distance > distance_limit:
        return "工况超出历史", avoidable
    if row.risk >= threshold and avoidable > 0:
        return "需要先拆检", avoidable
    return "可申请更换", avoidable


def version_verdict(entries, *, threshold, distance_limit):
    """Adjudicate one claim against every simultaneously effective catalogue version.

    entries: ordered sequence of (position, PartsRequest | None, unavailable_reason | None); the
    ordering is content-derived so catalogue row order never decides the outcome.
    Returns (verdict, representative request or None, per-version evidence).
    """
    notes = []
    requests = []
    states = []
    unusable = False
    for position, request, missing in entries:
        if request is None:
            unusable = True
            states.append(missing or "缺少可用估算")
            notes.append({"版本序号":position,"兼容件":"—","件数":0,"处置":missing or "缺少可用估算"})
            continue
        state, _ = economic_choice(request, distance_limit=distance_limit, threshold=threshold)
        states.append(state)
        notes.append({"版本序号":position,"兼容件":request.part,"件数":request.units,"处置":state})
        if state == "工况超出历史":
            unusable = True
        else:
            requests.append(request)
    if len(entries) == 1:
        return states[0], entries[0][1], notes
    if unusable or not requests:
        return "整单待查", None, notes
    representative = max(requests, key=lambda row: (row.risk, row.cost_cents, row.part, row.code, row.units))
    if all(state == "需要先拆检" for state in states):
        return "需要先拆检", representative, notes
    if all(state == "可申请更换" for state in states):
        if len({(row.part, row.units) for row in requests}) == 1:
            return "可申请更换", representative, notes
        return "整单挂起待核", None, notes
    return "整单挂起待核", None, notes


def make_versioned_plan(assessments, stock, allowance, *, threshold=.40, distance_limit=6.5):
    """Plan authorization when each claim may carry several simultaneously effective versions.

    assessments: sequence of (claim, appliance, entries) where entries is the ordered
    (position, PartsRequest | None, unavailable_reason | None) list for that claim's live versions.
    Returns (decisions, reserved, adjudication). Claims verdict 整单待查/整单挂起待核 stay out of
    the authorization decisions and surface in adjudication for the caller to hold.
    """
    if not 0 <= threshold <= 1 or allowance < 0 or any(n < 0 for n in stock.values()):
        raise ValueError("APPROVAL_POLICY_OUT_OF_RANGE")
    actionable = []
    shared_parts = {}
    adjudication = []
    for claim, appliance, entries in assessments:
        verdict, chosen, notes = version_verdict(entries, threshold=threshold, distance_limit=distance_limit)
        held = chosen is None
        adjudication.append(dict(claim=claim, appliance=appliance, verdict=verdict,
                                 versions=len(entries), held=held, notes=notes))
        if held:
            continue
        actionable.append(chosen)
        parts = {(row.part, row.units) for _, row, _ in entries if row is not None}
        shared_parts[claim] = parts.pop() if len(parts) == 1 else None
    decisions, reserved = make_plan(actionable, stock, allowance, threshold=threshold, distance_limit=distance_limit)
    for row in decisions:
        if row["reason"] == "需要先拆检" and shared_parts.get(row["claim"]) is None:
            row["code"] = "—"
            row["part"] = "—"
    return decisions, reserved, adjudication


def make_plan(requests, stock, allowance, *, threshold=.40, distance_limit=6.5):
    if not 0 <= threshold <= 1 or allowance < 0 or any(n < 0 for n in stock.values()):
        raise ValueError("APPROVAL_POLICY_OUT_OF_RANGE")
    duplicated=Counter(row.claim for row in requests)
    used=Counter()
    used_total=0
    decisions=[]
    conflicts=[]
    for row in requests:
        state, avoidable=economic_choice(row,distance_limit=distance_limit,threshold=threshold)
        conflicts.append((state,avoidable,row))
    for state, avoidable, row in sorted(conflicts,key=lambda item:(item[0]!="可申请更换",item[2].risk,item[2].claim)):
        reserved=0
        if duplicated[row.claim]>1:
            state="重复申请待查"
        elif state=="可申请更换":
            if row.units > stock.get(row.part,0)-used[row.part]:
                state="备件可用量不足"
            elif row.units > allowance-used_total:
                state="当班授权额度不足"
            else:
                state="批准换件"
                reserved=row.units
                used[row.part]+=reserved
                used_total+=reserved
        decisions.append(dict(claim=row.claim,appliance=row.appliance,code=row.code,part=row.part,parts_reserved=reserved,
                              risk=round(row.risk,4),cost_yuan=round(row.cost_cents/100,2),net_avoidable_yuan=round(avoidable/100,2),reason=state))
    return decisions, dict(used)
