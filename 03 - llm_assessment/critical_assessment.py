#!/usr/bin/env python3
"""Prepare, launch and resume Phase 1 with separate case and rule-audit endpoints."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
from curia_assessment import pipeline as p
from curia_assessment.case_pipeline import prepare_case, run_case, analyse_case
from curia_assessment.formalisation_protocol import (SYSTEM_PROMPT, SEMANTICS,
    LOG_SCHEMA, ACTIVITY_DICTIONARY, ALLOWED_REFS)
from curia_assessment.formalisation_runner import AuditRunner, MODELS

DEFAULT_BASELINE = ROOT / 'config.phase1.activation50.requirement_declare.32000_20261124_002546.frozen.json'
PROTOCOL = 'critical_phase1_v1'


def source_files():
    paths = [ROOT / 'critical_assessment.py', ROOT / 'run_assessment.py']
    for directory in ('curia_assessment', 'curia_conformance'):
        paths.extend(sorted((ROOT / directory).glob('*.py')))
        paths.extend(sorted((ROOT / directory).glob('*.mjs')))
    return paths


def _table(path):
    import csv
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def _freeze_audit(output: Path, native: Path, config: dict):
    audit = output / '_audit'
    if output.exists():
        raise ValueError('The formalisation-audit destination already exists; use a fresh study.')
    rules = [dict(rule_id=r['base_rule_id'], base_rule_id=r['base_rule_id'],
                  requirement=r['requirement'], declare=r['declare'])
             for r in _table(native / '_audit/prepared/selected_rules.csv')]
    if len(rules) != 19 or len({r['rule_id'] for r in rules}) != 19:
        raise ValueError('This study requires the unchanged nineteen-rule catalogue.')
    labels = sorted({e['activity'] for pair in p.read_jsonl(native / '_audit/prepared/pairs.jsonl') for e in pair['events']})
    # The dictionary covers the complete cleaned input log, including labels not
    # observed in the fifty selected traces. Its observation limits stay explicit.
    import csv
    with Path(config['event_log_csv']).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        schema = reader.fieldnames
        labels = sorted({row['Activity'] for row in reader})
    dictionary = {**ACTIVITY_DICTIONARY, 'allowed_labels': labels}
    log_schema = {**LOG_SCHEMA, 'fields': schema}
    protocol = dict(protocol_id='formalisation_audit_v2', created_utc=p.utc_now(),
        production_code_sha256=p.code_digest(), providers=config['providers'],
        max_output_tokens=32000, timeout_seconds=900, request_workers=6,
        requests_per_provider=2, planned_calls=57, rules=19, activity_labels=labels,
        system_prompt=SYSTEM_PROMPT, semantics=SEMANTICS, log_schema=log_schema,
        activity_dictionary=dictionary,
        primary_endpoint='Full literal requirement versus formula within stipulated applicability; nineteen rule audits per model.',
        secondary_endpoint='Formula-alone scope adequacy without external guards; distinct from actual case outcomes.',
        retry_policy='Only operational/structural failures may be retried with the unchanged request. Logical defects, formula errors and absent proof directions are retained and never trigger regeneration.',
        parser_policy='Prospectively retain additional JSON fields in raw_json with flags. Known required fields and exact A/D/U labels are validated. Logical inconsistencies are diagnostics, never structural failure.',
        claim_selection='Report first-transmission outcomes and final structurally valid responses separately. First-attempt availability retains all57 planned records, including operational failures.',
        neutrality='All nineteen unchanged rules; no disagreement quota or outcome-based selection.',
        completeness='Complete-base-act premise applies exclusively to hypothetical audit worlds, never to actual CURIA cases.',
        human_legal_validation_pending=True)
    jobs, allowed = [], {}
    for rule in rules:
        ident = rule['rule_id']
        evidence = dict(rule_id=ident, requirement=rule['requirement'], declare=rule['declare'],
                        log_schema=log_schema, activity_dictionary=dictionary, semantics=SEMANTICS)
        user = ('Audit the two explicitly defined domains independently. Use only the following evidence.\n'
            + 'Allowed evidence references: ' + json.dumps(sorted(ALLOWED_REFS))
            + '\n\nEVIDENCE DATA BEGIN\n' + json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)
            + '\nEVIDENCE DATA END')
        allowed[ident] = sorted(ALLOWED_REFS)
        for provider in config['providers']:
            request = p.build_request(provider, SYSTEM_PROMPT, user, 32000)
            job = dict(protocol_id='formalisation_audit_v2', rule_id=ident, audit_pair_id=ident,
                       pilot_pair_id=ident, provider_id=provider['id'], provider=provider['provider'],
                       model=provider['model'], condition='A', run=1,
                       request_sha256=p.digest(request), prompt_sha256=p.digest([SYSTEM_PROMPT, user]))
            job['job_id'] = 'audit_' + p.digest(job)[:24]
            p.write_json(audit / 'prepared/requests' / (job['job_id'] + '.json'), request)
            jobs.append(job)
    p.write_json(audit / 'protocol.json', protocol)
    p.write_jsonl(audit / 'prepared/jobs.jsonl', jobs)
    p.write_jsonl(audit / 'prepared/rules.jsonl', rules)
    p.write_json(audit / 'prepared/allowed_refs.json', allowed)
    p.write_json(audit / 'prepared/input_identity.json', {
        'selected_rules_sha256': p.sha256_file(native / '_audit/prepared/selected_rules.csv'),
        'cleaned_log_sha256': p.sha256_file(Path(config['event_log_csv']))})
    for name in ('formalisation_runner.py', 'formalisation_protocol.py', 'formalisation_witnesses.py'):
        destination = audit / 'source_snapshot' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'curia_assessment' / name, destination)
    frozen = {str(f.relative_to(output)): p.sha256_file(f) for f in sorted(audit.rglob('*')) if f.is_file()}
    p.write_json(audit / 'manifest.json', dict(schema_version=2,
        production_code_sha256=p.code_digest(), protocol_id='formalisation_audit_v2', frozen_files=frozen))
    # Full offline guard before any credential is loaded or request transmitted.
    AuditRunner(output)


def prepare_study(baseline_config: Path = DEFAULT_BASELINE, output: Path | None = None):
    config, config_root = p.load_config(baseline_config)
    if config['assessment_mode'] != 'case' or config['conditions'] != ['A']:
        raise ValueError('A log-only, case-mode phase-1 baseline is required.')
    baseline = p.resolve(config_root, config['output_dir'])
    previous = p.read_json(baseline / '_audit/run_manifest.json')
    if previous['sampled_cases'] != 50 or previous['selected_pairs'] != 950:
        raise ValueError('The source cohort must contain fifty cases and nineteen rules.')
    if config['repetitions'] != 1 or config['max_output_tokens'] != 32000:
        raise ValueError('Retain one repetition and the uniform32,000-token ceiling.')
    if ({x['provider']: x['model'] for x in config['providers']} != MODELS
            or any(x.get('reasoning_effort') != 'medium' for x in config['providers'])):
        raise ValueError('The three pinned models and medium effort must be retained.')
    stamp = datetime.now(ZoneInfo('Europe/Rome')).strftime('%Y%m%d_%H%M%S')
    study = (output or ROOT / 'outputs' / ('phase1_2016_2026_50_cases_critical_' + stamp)).resolve()
    study.mkdir(parents=True, exist_ok=False)
    native = study / 'case_assessment'
    for field in ('rules_csv', 'rules_decl', 'event_log_csv', 'formal_results', 'evidence_jsonl',
                  'case_texts_dir', 'ocr_sidecars_dir'):
        if config.get(field) is not None:
            config[field] = str(p.resolve(config_root, config[field]))
    config.update(output_dir=str(native), cohort_source_dir=str(baseline), baseline_phase1_dir=None,
                  case_protocol='critical_v1', timeout_seconds=900, request_workers=6, requests_per_provider=2,
                  max_document_characters=None, max_case_characters=None)
    config_path = study / '_audit/config.phase1.frozen.json'
    p.write_json(config_path, config)
    manifest = prepare_case(config_path)
    if manifest['sampled_cases'] != 50 or manifest['selected_pairs'] != 950:
        raise ValueError('The prepared study did not retain the complete source cohort.')
    _freeze_audit(study / 'formalisation_audit', native, config)
    metadata = dict(schema_version=1, protocol_id=PROTOCOL, phase=1, created_utc=p.utc_now(),
        case_assessment_dir='case_assessment', formalisation_audit_dir='formalisation_audit',
        case_config='_audit/config.phase1.frozen.json', baseline_phase1_dir=str(baseline),
        planned_cases=50, rules=19, planned_case_requests=150, planned_audit_requests=57,
        case_protocol='critical_v1', max_output_tokens=32000, model_responses_reused=0,
        endpoints=dict(case_comparison='Requirement case judgement versus deterministic recorded-trace comparator.',
                       trace_fidelity='Model recorded-trace judgement versus deterministic recorded-trace comparator.',
                       formalisation='Whole requirement versus formula; nineteen unique rules per model.'),
        operational_retries=dict(max_invocations=3, unchanged_request=True, semantic_retries=False),
        limits='Case and formalisation outcomes have different units and are never pooled. Neither agreement rate establishes legal accuracy.')
    p.write_json(study / 'study.json', metadata)
    for source in source_files():
        target = study / '_audit/source_snapshot' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    protected = {}
    for source in sorted((ROOT / 'outputs').rglob('*')):
        if source.is_file() and not source.is_relative_to(study) and not source.is_symlink() and '__pycache__' not in source.parts and source.name != '.env' and not source.name.startswith('.env.'):
            protected[str(source.relative_to(ROOT))] = p.sha256_file(source)
    for source in sorted((ROOT / 'data').rglob('*')):
        if source.is_file() and not source.is_symlink() and source.name != '.env' and not source.name.startswith('.env.'):
            protected[str(source.relative_to(ROOT))] = p.sha256_file(source)
    for source in ROOT.glob('config*.json'):
        protected[str(source.relative_to(ROOT))] = p.sha256_file(source)
    p.write_json(study / '_audit/protected_historical_files.json', protected)
    frozen = {str(source.relative_to(study)): p.sha256_file(source)
              for source in sorted((study / '_audit/source_snapshot').rglob('*')) if source.is_file()}
    for relative in ('study.json', '_audit/config.phase1.frozen.json',
                     '_audit/protected_historical_files.json', 'case_assessment/_audit/run_manifest.json',
                     'formalisation_audit/_audit/manifest.json'):
        frozen[relative] = p.sha256_file(study / relative)
    identity = dict(schema_version=1, protocol_id=PROTOCOL, code_sha256=p.code_digest(),
                    entrypoint_sha256=p.sha256_file(Path(__file__)), frozen_files=frozen)
    p.write_json(study / '_audit/run_manifest.json', identity)
    verify(study)
    _status(study, 'prepared')
    print(json.dumps(dict(study=str(study), planned_api_requests=207, cases=50, rules=19,
                         model_responses_reused=0, status='prepared'), indent=2), flush=True)
    return study


def verify(study: Path):
    study = study.resolve()
    identity = p.read_json(study / '_audit/run_manifest.json')
    if identity['protocol_id'] != PROTOCOL or identity['code_sha256'] != p.code_digest() or identity['entrypoint_sha256'] != p.sha256_file(Path(__file__)):
        raise ValueError('The frozen study executable changed; preserve it and prepare a fresh study.')
    for relative, digest in identity['frozen_files'].items():
        path = study / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink()
                or not path.resolve().is_relative_to(study)
                or not path.is_file() or p.sha256_file(path) != digest):
            raise ValueError('A frozen study input changed: ' + relative)
    prepare_case(study / '_audit/config.phase1.frozen.json')
    AuditRunner(study / 'formalisation_audit')
    return p.read_json(study / 'study.json')


def _stage_status(study, subdirectory):
    file = study / subdirectory / '_audit/resume_status.json'
    return p.read_json(file) if file.is_file() else dict(planned=150 if subdirectory == 'case_assessment' else 57, complete=0, running=0)


def _status(study, stage, error=None):
    value = dict(protocol_id=PROTOCOL, updated_utc=p.utc_now(), stage=stage,
                 case_assessment=_stage_status(study, 'case_assessment'),
                 formalisation_audit=_stage_status(study, 'formalisation_audit'), error=error)
    p.write_json(study / '_audit/study_status.json', value)
    return value


def verify_historical(study: Path):
    expected = p.read_json(study / '_audit/protected_historical_files.json')
    changed, missing = [], []
    for relative, digest in expected.items():
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
        elif p.sha256_file(path) != digest:
            changed.append(relative)
    result = dict(checked_utc=p.utc_now(), files=len(expected), changed=changed,
                  missing=missing, unchanged=len(expected)-len(changed)-len(missing),
                  integrity_pass=not changed and not missing)
    p.write_json(study / '_audit/historical_integrity.json', result)
    return result


def run_study(study: Path, *, resume=False, provider=None, resolve_stop=False, resolution_note=None):
    study = study.resolve()
    with p.output_lock(study / '_audit'):
        verify(study)
        if resolve_stop:
            records_path = study / '_audit/resolution_requests.json'
            records = p.read_json(records_path) if records_path.is_file() else []
            records.append(dict(recorded_utc=p.utc_now(), provider=provider, note=resolution_note,
                                case_stop=_stage_status(study, 'case_assessment').get('providers', {}).get(provider, {}).get('stop')))
            p.write_json(records_path, records)
        _status(study, 'case_assessment_running')
        from run_assessment import _load_credentials
        _load_credentials()
        initial = _stage_status(study, 'case_assessment')
        known_stops = {key for key, row in initial.get('providers', {}).items() if row.get('stop')}
        if resume and known_stops:
            # A general technical resume does not implicitly resolve an account
            # stop. Re-enabling that provider requires the recorded resolution.
            eligible = [key for key in p.read_json(study / '_audit/config.phase1.frozen.json')['providers']
                        if (provider is None or key['id'] == provider)
                        and (key['id'] not in known_stops or resolve_stop and key['id'] == provider)]
            for item in eligible:
                run_case(study / '_audit/config.phase1.frozen.json', provider_id=item['id'], retry_failed=True)
        else:
            run_case(study / '_audit/config.phase1.frozen.json', provider_id=provider, retry_failed=resume)
        for index in range(2):
            state = _stage_status(study, 'case_assessment')
            eligible = [key for key, row in state.get('providers', {}).items()
                        if (provider is None or key == provider)
                        and row['remaining'] > 0 and not row.get('stop')]
            if not eligible:
                break
            # Retry only operational/structural failures. A different provider's
            # bad JSON must never automatically restart a budget-stopped model.
            for key in eligible:
                run_case(study / '_audit/config.phase1.frozen.json', provider_id=key,
                         retry_failed=True)
        _status(study, 'formalisation_audit_running')
        runner = AuditRunner(study / 'formalisation_audit')
        case_state = _stage_status(study, 'case_assessment')
        suspended = {key for key, row in case_state.get('providers', {}).items() if row.get('stop')}
        p.write_json(study / '_audit/cross_stage_provider_stops.json', {
            'recorded_utc': p.utc_now(), 'providers': {key: case_state['providers'][key]['stop'] for key in sorted(suspended)},
            'policy': 'A provider stopped during case assessment is not probed again by the rule audit. Explicit resume can re-enable it after the reported issue is resolved.'})
        audit_resolve = resolve_stop and provider in runner.inspect_provider_stops()
        for index in range(3):
            state = runner.execute('resume' if resume or index else 'run', provider,
                resolve_stop=audit_resolve and index == 0,
                resolution_note=resolution_note if audit_resolve and index == 0 else None,
                exclude_provider_ids=suspended)
            selected = [x for k,x in state['providers'].items() if (provider is None or k == provider) and k not in suspended]
            if all(x['remaining'] == 0 or x.get('stop') for x in selected):
                break
        verify(study)
        from curia_assessment.critical_reporting import write_critical_outputs
        outputs = write_critical_outputs(study)
        verify_historical(study)
        state = _status(study, 'complete' if is_complete(study) else 'incomplete')
        p.write_json(study / '_audit/published_outputs.json', outputs)
        print(json.dumps(dict(stage=state['stage'], study=str(study), outputs=outputs), indent=2), flush=True)
        return state


def is_complete(study):
    case = _stage_status(study, 'case_assessment')
    audit = _stage_status(study, 'formalisation_audit')
    return case.get('complete') == 150 and audit.get('complete') == 57


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'resume', 'status', 'analyse'])
    parser.add_argument('--baseline-config', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--study', type=Path)
    parser.add_argument('--provider', choices=['openai', 'anthropic', 'mistral'])
    parser.add_argument('--resolve-provider-stop', action='store_true')
    parser.add_argument('--resolution-note')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        prepare_study(args.baseline_config, args.study)
        return 0
    if args.study is None:
        parser.error('--study is required when continuing a prepared study.')
    study = args.study.resolve()
    try:
        if args.command in {'run', 'resume'}:
            if args.resolve_provider_stop and (args.command != 'resume' or args.provider is None or not args.resolution_note):
                parser.error('Resolving a provider stop requires resume, --provider and --resolution-note.')
            result = run_study(study, resume=args.command == 'resume', provider=args.provider,
                               resolve_stop=args.resolve_provider_stop, resolution_note=args.resolution_note)
            return 0 if result['stage'] == 'complete' else 3
        if args.command == 'status':
            print(json.dumps(dict(case_assessment=_stage_status(study, 'case_assessment'),
                                  formalisation_audit=_stage_status(study, 'formalisation_audit')), indent=2))
            return 0
        with p.output_lock(study / '_audit'):
            verify(study)
            analyse_case(study / '_audit/config.phase1.frozen.json')
            from curia_assessment.critical_reporting import write_critical_outputs
            print(json.dumps(write_critical_outputs(study), indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if (study / 'study.json').is_file() and p.read_json(study / 'study.json').get('protocol_id') == PROTOCOL:
            _status(study, 'stopped', error=f'{type(exc).__name__}: {exc}')
        print(f'Study stopped: {exc}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
