#!/usr/bin/env python3
"""Thin P0 runner. Every scenario owns a TemporaryDirectory database."""
import json
from app import SCENARIOS, run_scenario

def main():
    results=[]
    for sid in SCENARIOS:
        r=run_scenario(sid)
        assert r['due_task']['status']=='open'
        if sid=='P0-S2-start-abandon': assert r['abandon']['attempt_id'] is None
        if sid=='P0-S3-independent-submit': assert r['submit']['attempt_id']==r['repeat_submit']['attempt_id']
        if sid=='P0-S4-independent-assessment': assert len(r['assessment']['evidence_event_ids'])==1
        if sid=='P0-S5-assisted-or-unassessed': assert r['assessment']['result']=='unassessed' and not r['assessment']['evidence_event_ids']
        results.append({'scenario_id':sid,'status':'passed'})
    print(json.dumps({'runner':'run_p0_scenarios','results':results},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
