import io
import json
import sqlite3
import zipfile

import pytest

from canary import compact_bundle, research_bundle, review_history
from test_compact_bundle import prepared


PERSON = {'kind': 'person', 'name': 'Reviewer A', 'role': 'screener', 'identity': 'self_declared'}
SOFTWARE = {'kind': 'software', 'name': 'Extractor', 'model_version': 'test-v1', 'prompt_reference': 'protocol-v1'}


def decision(reason='Included', **changes):
    return dict({'subject': 'Report A', 'judgement': 'include', 'status': 'accepted', 'reason': reason,
                 'evidence': ['Report A, methods, paragraph 2'], 'proposed_by': None,
                 'decided_by': SOFTWARE, 'checked_by': None}, **changes)


def test_review_editions_survive_sealing_correction_and_download(tmp_path):
    prepared(tmp_path)
    first = review_history.append(tmp_path, decision('Wrong population'))
    second = review_history.append(tmp_path, decision('Eligible outcome'))
    before = review_history.edition(tmp_path, first)
    assert before['base_sha256'] is None
    assert review_history.NAME not in {r['path'] for r in research_bundle.seal(tmp_path)['artifacts']}
    compact_bundle.retain(tmp_path)
    third = review_history.append(tmp_path, decision('Population confirmed'), replaces=first)
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
        review_history.append(tmp_path, decision(), replaces='missing')
    assert not (tmp_path / review_history.NAME).exists()
    compact_bundle.retain(tmp_path)
    assert review_history.edition(tmp_path)['entries'] == []
    assert not (tmp_path / review_history.NAME).exists()
    first = review_history.append(tmp_path, decision('Included'))
    assert review_history.edition(tmp_path, first)['last_entry'] == first


def test_compaction_refuses_a_changed_review_and_keeps_sources(tmp_path):
    prepared(tmp_path)
    review_history.append(tmp_path, decision('Included'))
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        db.execute("UPDATE entries SET body = replace(body, 'Included', 'Excluded')")
    with pytest.raises(ValueError, match='changed since'):
        compact_bundle.retain(tmp_path)
    assert (tmp_path / 'run.json').is_file()
    assert not compact_bundle.exists(tmp_path)


def test_cleanup_retry_keeps_the_original_decision_and_its_base(tmp_path, monkeypatch):
    prepared(tmp_path)
    entry = review_history.append(tmp_path, decision('Included'))
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
    first = review_history.append(tmp_path, decision('Included'))
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
    original = review_history.append(tmp_path, proposal)
    checked = decision('Methods confirm eligibility', status='modified', proposed_by=SOFTWARE,
                       decided_by=PERSON, checked_by=PERSON)
    review_history.append(tmp_path, checked, replaces=original)
    direct = decision(decided_by=PERSON)
    review_history.append(tmp_path, direct)
    automated = decision()
    review_history.append(tmp_path, automated)
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
        review_history.append(tmp_path, decision(**changes))
    assert not (tmp_path / review_history.NAME).exists()

def test_modified_decision_identifies_the_record_it_replaces(tmp_path):
    prepared(tmp_path)
    with pytest.raises(ValueError, match='must name the entry'):
        review_history.append(tmp_path, decision(status='modified'))


def test_software_validation_names_the_missing_provenance_field(tmp_path):
    prepared(tmp_path)
    actor = dict(SOFTWARE)
    del actor['prompt_reference']
    with pytest.raises(ValueError, match='prompt_reference'):
        review_history.append(tmp_path, decision(decided_by=actor))
