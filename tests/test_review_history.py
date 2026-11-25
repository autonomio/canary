import io
import json
import sqlite3
import zipfile

import pytest

from canary import compact_bundle, research_bundle, review_history
from test_compact_bundle import prepared


def test_review_editions_survive_sealing_correction_and_download(tmp_path):
    prepared(tmp_path)
    first = review_history.append(tmp_path, {'reason': 'Wrong population'})
    second = review_history.append(tmp_path, {'reason': 'Eligible outcome'})
    before = review_history.edition(tmp_path, first)
    assert before['base_sha256'] is None
    assert review_history.NAME not in {r['path'] for r in research_bundle.seal(tmp_path)['artifacts']}
    compact_bundle.retain(tmp_path)
    third = review_history.append(tmp_path, {'reason': 'Population confirmed'}, replaces=first)
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
        review_history.append(tmp_path, {}, replaces='missing')
    assert not (tmp_path / review_history.NAME).exists()
    compact_bundle.retain(tmp_path)
    assert review_history.edition(tmp_path)['entries'] == []
    assert not (tmp_path / review_history.NAME).exists()
    first = review_history.append(tmp_path, {'reason': 'Included'})
    assert review_history.edition(tmp_path, first)['last_entry'] == first


def test_compaction_refuses_a_changed_review_and_keeps_sources(tmp_path):
    prepared(tmp_path)
    review_history.append(tmp_path, {'reason': 'Included'})
    with sqlite3.connect(tmp_path / review_history.NAME) as db:
        db.execute("UPDATE entries SET body = replace(body, 'Included', 'Excluded')")
    with pytest.raises(ValueError, match='changed since'):
        compact_bundle.retain(tmp_path)
    assert (tmp_path / 'run.json').is_file()
    assert not compact_bundle.exists(tmp_path)


def test_cleanup_retry_keeps_the_original_decision_and_its_base(tmp_path, monkeypatch):
    prepared(tmp_path)
    entry = review_history.append(tmp_path, {'reason': 'Included'})
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
    first = review_history.append(tmp_path, {'reason': 'Included'})
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
