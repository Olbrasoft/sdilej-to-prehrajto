import importlib.util
from pathlib import Path

import pytest

from sdilej_to_prehrajto.cli import recover_failed_sources
from sdilej_to_prehrajto.ranking import SELECTION_POLICY
from sdilej_to_prehrajto.sources import SelectedSourceStore
from sdilej_to_prehrajto.state import StateStore


def test_runner_mirror_lists_and_deb822_are_normalized(tmp_path):
    spec = importlib.util.spec_from_file_location('mirrors', Path(__file__).parents[1] / 'scripts/configure_ubuntu_mirrors.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'sources.list.d').mkdir()
    (tmp_path / 'sources.list.d/ubuntu.sources').write_text('URIs: mirror+file:/etc/apt/apt-mirrors.txt\nSuites: noble noble-updates\n')
    (tmp_path / 'apt-mirrors.txt').write_text('http://azure.archive.ubuntu.com/ubuntu/\thttps=1\nhttp://archive.ubuntu.com/ubuntu/\n')
    (tmp_path / 'apt-mirrors-security.txt').write_text('http://security.ubuntu.com/ubuntu/\n')
    (tmp_path / 'sources.list').write_text('deb http://azure.archive.ubuntu.com/ubuntu noble main\n')
    (tmp_path / 'sources.list.d/vendor.list').write_text('deb https://vendor.example/repo stable main\n')
    assert module.configure(tmp_path) == 3
    assert module.configure(tmp_path) == 0
    assert 'azure.archive' not in (tmp_path / 'apt-mirrors.txt').read_text()
    assert 'https://archive.ubuntu.com/ubuntu/' in (tmp_path / 'apt-mirrors.txt').read_text()
    assert 'mirror+file:' in (tmp_path / 'sources.list.d/ubuntu.sources').read_text()
    assert (tmp_path / 'sources.list.d/vendor.list').read_text() == 'deb https://vendor.example/repo stable main\n'


@pytest.mark.parametrize('protected', [None, 'upload', 'prepared', 'previous_target_id', 'claim', 'historical_target', 'one_failure', 'other_error', 'new_source'])
def test_source_recovery_preserves_all_target_guards(tmp_path, protected):
    state = StateStore(tmp_path / 'sync.json')
    sources = SelectedSourceStore(tmp_path / 'sources.jsonl')
    sources.record({'cr_film_id': 1, 'source_id': '123', 'source_url': 'https://sdilej.cz/123/film.mkv', 'selection_policy': SELECTION_POLICY})
    attempts = [{'status': 'source_refresh_failed', 'source_id': '123', 'reason': 'Source detail has no authenticated download link; source_id=123'} for _ in range(3)]
    row = state.film(1)
    row['attempts'] = attempts
    if protected in {'upload', 'prepared', 'previous_target_id', 'claim'}:
        row[protected] = {'target_video_id': '777'}
    elif protected == 'historical_target':
        attempts[0]['target_video_id'] = '777'
    elif protected == 'one_failure':
        row['attempts'] = attempts[:1]
    elif protected == 'other_error':
        attempts[-1]['reason'] = 'Temporary network failure'
    elif protected == 'new_source':
        attempts[-1]['source_id'] = '456'
    original = state.snapshot(1)
    assert recover_failed_sources(state, sources) == (1 if protected is None else 0)
    assert state.snapshot(1) == original
    assert (sources.candidate(1) is None) == (protected is None)
    assert sources.get(1)['source_url'] == 'https://sdilej.cz/123/film.mkv'
