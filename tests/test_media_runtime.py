import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response():
    return SimpleNamespace(stdout=json.dumps({'streams': [{'codec_name': 'pcm_s16le', 'sample_rate': '16000'}]}))


def test_installed_tools_are_exercised_without_apt(monkeypatch):
    runtime = load_script('ensure_media_runtime')
    monkeypatch.setattr(runtime.shutil, 'which', lambda tool: '/usr/bin/' + tool)
    calls = []
    monkeypatch.setattr(runtime.subprocess, 'run', lambda cmd, **kw: calls.append((cmd, kw)) or response())
    runtime.ensure_runtime()
    assert [cmd[0] for cmd, kw in calls] == ['ffmpeg', 'ffprobe']
    assert all(kw['check'] and kw['timeout'] == 30 for cmd, kw in calls)


@pytest.mark.parametrize('failure', ['update', 'install', 'timeout'])
def test_failed_archive_falls_back_to_azure_with_hard_limits(monkeypatch, failure):
    runtime = load_script('ensure_media_runtime')
    # One binary alone is insufficient.
    monkeypatch.setattr(runtime.shutil, 'which', lambda tool: '/usr/bin/ffmpeg' if tool == 'ffmpeg' else None)
    calls = []
    mirror = None

    def run(cmd, **kwargs):
        nonlocal mirror
        calls.append((cmd, kwargs))
        if '--mirror' in cmd:
            mirror = cmd[-1]
        if 'apt-get' in cmd:
            assert cmd[:5] == ['sudo', '-n', 'timeout', '--kill-after=10s', '120s' if cmd[-1] == 'update' else '180s']
            assert 'APT::Update::Error-Mode=any' in cmd
            assert kwargs['timeout'] <= 200
            operation = 'update' if cmd[-1] == 'update' else 'install'
            if mirror == 'archive' and (failure == operation or failure == 'timeout'):
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
                raise subprocess.CalledProcessError(100, cmd)
        return response()

    monkeypatch.setattr(runtime.subprocess, 'run', run)
    runtime.ensure_runtime()
    assert [cmd[-1] for cmd, kw in calls if '--mirror' in cmd] == ['archive', 'azure']
    assert [cmd[0] for cmd, kw in calls][-2:] == ['ffmpeg', 'ffprobe']
    if failure in {'update', 'timeout'}:
        assert len([cmd for cmd, kw in calls if 'install' in cmd]) == 1


def test_both_failed_updates_do_not_install_from_stale_indexes(monkeypatch):
    runtime = load_script('ensure_media_runtime')
    monkeypatch.setattr(runtime.shutil, 'which', lambda _tool: None)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if 'apt-get' in cmd:
            raise subprocess.CalledProcessError(100, cmd)
        return response()

    monkeypatch.setattr(runtime.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='both official mirrors'):
        runtime.ensure_runtime()
    assert not any('install' in cmd for cmd in calls)


def test_runtime_probe_rejects_nonfunctional_installed_tools(monkeypatch):
    runtime = load_script('ensure_media_runtime')
    monkeypatch.setattr(runtime.shutil, 'which', lambda tool: tool)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(stdout='{"streams": []}'))
    with pytest.raises(RuntimeError, match='did not verify'):
        runtime.ensure_runtime()


def test_azure_fallback_preserves_apt_signature_configuration(tmp_path):
    mirrors = load_script('configure_ubuntu_mirrors')
    (tmp_path / 'sources.list.d').mkdir()
    path = tmp_path / 'sources.list.d/ubuntu.sources'
    path.write_text('URIs: https://archive.ubuntu.com/ubuntu/ https://security.ubuntu.com/ubuntu/\nSuites: resolute resolute-security\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
    vendor = tmp_path / 'sources.list.d/vendor.list'
    vendor.write_text('deb https://vendor.example/ubuntu stable main\n')
    assert mirrors.configure(tmp_path, mirror='azure') == 1
    assert mirrors.configure(tmp_path, mirror='azure') == 0
    assert path.read_text().count('https://azure.archive.ubuntu.com/ubuntu/') == 2
    assert 'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' in path.read_text()
    assert vendor.read_text() == 'deb https://vendor.example/ubuntu stable main\n'


@pytest.mark.parametrize('workflow', ['prepare-sources', 'backfill-subtitles', 'source-media-diagnostics', 'pilot-plan', 'test'])
def test_media_workflows_share_bounded_setup(workflow):
    text = (Path(__file__).parents[1] / '.github/workflows' / f'{workflow}.yml').read_text()
    assert 'timeout-minutes: 12\n        run: python3 scripts/ensure_media_runtime.py' in text
    assert 'sudo apt-get' not in text
