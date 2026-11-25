import io
import json
import sqlite3
import zipfile
import uuid

import pytest

from canary import compact_bundle, research_bundle, review_history
from test_compact_bundle import prepared


PERSON = {'kind': 'person', 'name': 'Reviewer A', 'role': 'screener', 'identity': 'self_declared'}
SOFTWARE = {'kind': 'software', 'name': 'Extractor', 'model_version': 'test-v1', 'prompt_reference': 'protocol-v1'}


def decision(reason='Included', **changes):
    return dict({'subject': 'Report A', 'judgement': 'include', 'status': 'accepted', 'reason': reason,
                 'evidence': ['Report A, methods, paragraph 2'], 'proposed_by': None,
                 'decided_by': SOFTWARE, 'checked_by': None}, **changes)


def record(workspace, value, **options):
    head = review_history.edition(workspace)['last_entry']
    return review_history.append(workspace, value, request_id=uuid.uuid4().hex, expected_last_entry=head, **options)


def test_review_editions_survive_sealing_correction_and_download(tmp_path):
    prepared(tmp_path)
    first = record(tmp_path, decision('Wrong population'))
    second = record(tmp_path, decision('Eligible outcome'))
    before = review_history.edition(tmp_path, first)
    assert before['base_sha256'] is None
    assert review_history.NAME not in {r['path'] for r in research_bundle.seal(tmp_path)['artifacts']}
    compact_bundle.retain(tmp_path)
    third = record(tmp_path, decision('Population confirmed'), replaces=first)
    old = review_history.edition(tmp_path, first)
    assert old['entries'] == before['entries']
    assert old['bundle_id'] == before['bundle_id']
    assert old['base_sha256'] == research_bundle.file_hash(tmp_path / compact_bundle.NAME)
    current = review_history.edition(tmp_path)
    assert [r['id'] for r in current['entries']] == [first, second, third]
    assert current['entries'][-1]['replaces'] == first

    archive = io.BytesIO()
    research_bundle.archive(tmp_path, archive)
    with zipfile.ZipFile(archive) as output:
        assert json.loads(output.read('review-decisions.json')) == current
        assert output.read(review_history.NAME) == (tmp_path / review_history.NAME).read_bytes()


def test_failed_first_correction_and_old_bundles(tmp_path):
    prepared(tmp_path)
    with pytest.raises(ValueError, match='Unknown review decision'):
        record(tmp_path, decision(), replaces='missing')
    assert not (tmp_path / review_history.NAME).exists()
    compact_bundle.retain(tmp_path)
    assert review_history.edition(tmp_path)['entries'] == []
    assert not (tmp_path / review_history.NAME).exists()
    first = record(tmp_path, decision('Included'))
    assert review_history.edition(tmp_path, first)['last_entry'] == first


def test_compaction_refuses_a_changed_review_and_keeps_sources(tmp_path):
    prepared(tmp_path)
    record(tmp_path, decision('Included'))
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        db.execute("UPDATE entries SET body = replace(body, 'Included', 'Excluded')")
    with pytest.raises(ValueError, match='changed since'):
        compact_bundle.retain(tmp_path)
    assert (tmp_path / 'run.json').is_file()
    assert not compact_bundle.exists(tmp_path)


def test_cleanup_retry_keeps_the_original_decision_and_its_base(tmp_path, monkeypatch):
    prepared(tmp_path)
    entry = record(tmp_path, decision('Included'))
    release = compact_bundle._release

    def interrupt(workspace):
        (workspace / 'papers/W1.txt').unlink()
        raise OSError('Interrupted cleanup')

    monkeypatch.setattr(compact_bundle, '_release', interrupt)
    with pytest.raises(OSError, match='Interrupted cleanup'):
        compact_bundle.retain(tmp_path)
    before = review_history.edition(tmp_path, entry)
    monkeypatch.setattr(compact_bundle, '_release', release)
    compact_bundle.retain(tmp_path)
    assert not (tmp_path / '.run-reservation').exists()
    assert review_history.edition(tmp_path, entry) == before


def test_retry_binds_decisions_if_publication_was_interrupted(tmp_path, monkeypatch):
    prepared(tmp_path)
    first = record(tmp_path, decision('Included'))
    bind = review_history.bind

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(review_history, 'bind', interrupt)
    with pytest.raises(KeyboardInterrupt):
        compact_bundle.retain(tmp_path)
    assert compact_bundle.exists(tmp_path)
    assert (tmp_path / '.run-reservation').is_file()
    monkeypatch.setattr(review_history, 'bind', bind)
    manifest = compact_bundle.retain(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {compact_bundle.NAME, compact_bundle.CHECKSUM, review_history.NAME}
    assert review_history.edition(tmp_path, first)['base_sha256'] == research_bundle.file_hash(tmp_path / compact_bundle.NAME)
    assert compact_bundle.retain(tmp_path) == manifest

def test_recorded_participants_and_evidence_survive_compaction(tmp_path):
    prepared(tmp_path)
    proposal = decision('Review eligibility', status='proposed', proposed_by=SOFTWARE, decided_by=None)
    original = record(tmp_path, proposal)
    checked = decision('Methods confirm eligibility', status='modified', proposed_by=SOFTWARE,
                       decided_by=PERSON, checked_by=PERSON)
    record(tmp_path, checked, replaces=original)
    direct = decision(decided_by=PERSON)
    record(tmp_path, direct)
    automated = decision()
    record(tmp_path, automated)
    compact_bundle.retain(tmp_path)
    assert [entry['decision'] for entry in review_history.edition(tmp_path)['entries']] == [
        proposal, checked, direct, automated]


@pytest.mark.parametrize('changes', [
    {'reason': ' '}, {'evidence': []}, {'status': 'accepted', 'decided_by': None},
    {'status': 'proposed', 'proposed_by': None, 'decided_by': None},
    {'decided_by': dict(SOFTWARE, model_version='')}, {'checked_by': SOFTWARE},
    {'decided_by': dict(PERSON, identity='verified')},
])
def test_invalid_decisions_leave_no_review_file(tmp_path, changes):
    prepared(tmp_path)
    with pytest.raises(ValueError, match='Invalid review decision'):
        record(tmp_path, decision(**changes))
    assert not (tmp_path / review_history.NAME).exists()

def test_modified_decision_identifies_the_record_it_replaces(tmp_path):
    prepared(tmp_path)
    with pytest.raises(ValueError, match='must name the entry'):
        record(tmp_path, decision(status='modified'))


def test_software_validation_names_the_missing_provenance_field(tmp_path):
    prepared(tmp_path)
    actor = dict(SOFTWARE)
    del actor['prompt_reference']
    with pytest.raises(ValueError, match='prompt_reference'):
        record(tmp_path, decision(decided_by=actor))


def test_concurrent_resubmissions_and_competing_edits(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    prepared(tmp_path)
    data = decision()
    def submit(request_id):
        try:
            return review_history.append(tmp_path, data, request_id=request_id, expected_last_entry='initial')
        except review_history.ReviewConflict:
            return None

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(submit, ['first', 'first', 'second', 'second']))
    assert results.count(None) == 2
    first, = set(results) - {None}
    assert len(review_history.edition(tmp_path)['entries']) == 1
    correction = dict(request_id='correction', expected_last_entry=first, replaces=first)
    updated = decision('Methods revised')
    second = review_history.append(tmp_path, updated, **correction)
    record(tmp_path, decision('Another outcome', subject='Outcome B'))
    assert review_history.append(tmp_path, updated, **correction) == second
    with pytest.raises(review_history.ReviewConflict):
        review_history.append(tmp_path, data, **correction)
    with pytest.raises(review_history.ReviewConflict):
        record(tmp_path, updated, replaces=first)
    assert len(review_history.edition(tmp_path)['entries']) == 3


def test_large_review_file_and_amendments_are_outside_the_contribution(tmp_path, monkeypatch):
    import shutil
    from canary import bundle_connectors, bundle_encoding
    workspace = tmp_path / 'research'
    workspace.mkdir()
    prepared(workspace)
    snapshot = tmp_path / 'snapshot'
    shutil.copytree(workspace, snapshot)
    compact_bundle.retain(workspace)
    def exported():
        return bundle_encoding.dumps(bundle_connectors.export(workspace, 'resolvent')).encode()
    expected = exported()
    shutil.rmtree(workspace)
    shutil.copytree(snapshot, workspace)
    subject = decision('Published passage', evidence=['p. 2: ' + 'x' * bundle_connectors.CONTRIBUTION_LIMIT_BYTES])
    first = record(workspace, subject)
    compact_bundle.retain(workspace)
    assert (workspace / review_history.NAME).stat().st_size > bundle_connectors.CONTRIBUTION_LIMIT_BYTES
    assert len(expected) + 1 < bundle_connectors.CONTRIBUTION_LIMIT_BYTES
    assert exported() == expected
    record(workspace, decision('Passage corrected'), replaces=first)
    assert exported() == expected
    assert review_history.edition(workspace, first)['entries'][0]['decision'] == subject
    assert len(review_history.edition(workspace)['entries']) == 2
    monkeypatch.setattr(bundle_connectors, 'CONTRIBUTION_LIMIT_BYTES', len(expected) - 1)
    with pytest.raises(bundle_connectors.ContributionTooLarge):
        bundle_connectors.export(workspace, 'resolvent')
    archive = io.BytesIO()
    research_bundle.archive(workspace, archive)
    with zipfile.ZipFile(archive) as output:
        downloaded = json.loads(output.read('review-decisions.json'))
    assert downloaded == review_history.edition(workspace)


def test_corrupt_earlier_entry_blocks_appends_corrections_and_retries(tmp_path):
    prepared(tmp_path)
    first = record(tmp_path, decision('First read'))
    second = review_history.append(tmp_path, decision('Second read'), request_id='second', expected_last_entry=first)
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        db.execute("UPDATE entries SET body = replace(body, 'First read', 'Changed') WHERE id = ?", (first,))
    for options in (dict(request_id='third', expected_last_entry=second),
                    dict(request_id='correct', expected_last_entry=second, replaces=first),
                    dict(request_id='second', expected_last_entry=first)):
        with pytest.raises(ValueError, match='changed since'):
            review_history.append(tmp_path, decision('Second read'), **options)
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2


def test_review_journals_stay_out_of_the_inventory_and_archived_database_is_complete(tmp_path):
    from contextlib import closing
    workspace = tmp_path / 'research'
    workspace.mkdir()
    prepared(workspace)
    first = record(workspace, decision('First'))
    with closing(sqlite3.connect(workspace / review_history.NAME)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        db.execute('BEGIN')
        db.execute('SELECT id FROM entries').fetchall()
        second = record(workspace, decision('Second'))
        assert (workspace / (review_history.NAME + '-wal')).stat().st_size > 0
        manifest = research_bundle.seal(workspace)
        assert review_history.DATABASE_FILES.isdisjoint(e['path'] for e in manifest['artifacts'])
        compact_bundle.retain(workspace)
        archive = io.BytesIO()
        research_bundle.archive(workspace, archive)
    with zipfile.ZipFile(archive) as output:
        snapshot = output.extract(review_history.NAME, tmp_path / 'download')
        assert json.loads(output.read('review-decisions.json'))['last_entry'] == second
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute('SELECT id FROM entries ORDER BY rowid').fetchall() == [(first,), (second,)]
        assert db.execute('SELECT base_sha256 FROM history').fetchone()[0] == research_bundle.file_hash(workspace / compact_bundle.NAME)


def test_retry_refuses_an_entry_past_the_saved_head(tmp_path):
    prepared(tmp_path)
    first = record(tmp_path, decision('First'))
    retry = dict(request_id='second', expected_last_entry=first)
    review_history.append(tmp_path, decision('Second'), **retry)
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        db.execute('UPDATE history SET last_entry = ?', (first,))
    with pytest.raises(ValueError, match='inconsistent last entry'):
        review_history.append(tmp_path, decision('Second'), **retry)
