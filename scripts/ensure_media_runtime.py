"""Verify media tools or install them with bounded, signed apt mirror fallback."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def verify_runtime():
    with tempfile.TemporaryDirectory(prefix='media-runtime-') as directory:
        sample = str(Path(directory) / 'sample.wav')
        subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono',
                        '-t', '0.1', '-c:a', 'pcm_s16le', sample],
                       check=True, timeout=30)
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                 '-show_entries', 'stream=codec_name,sample_rate',
                                 '-of', 'json', sample],
                                check=True, timeout=30, capture_output=True, text=True)
        streams = json.loads(result.stdout).get('streams', [])
        if not streams or streams[0].get('codec_name') != 'pcm_s16le' or streams[0].get('sample_rate') != '16000':
            raise RuntimeError('Media runtime did not verify the generated audio')


def ensure_runtime():
    if all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
        verify_runtime()
        print('media_runtime=verified source=preinstalled', flush=True)
        return
    options = ['-o', 'Acquire::Retries=0', '-o', 'Acquire::http::Timeout=15',
               '-o', 'Acquire::https::Timeout=15', '-o', 'APT::Update::Error-Mode=any']
    for mirror in ('archive', 'azure'):
        try:
            subprocess.run(['sudo', '-n', 'python3', str(Path(__file__).with_name('configure_ubuntu_mirrors.py')),
                            '--mirror', mirror], check=True, timeout=30)
            # Per-request apt timeouts alone can still multiply into an hour
            # across dozens of dependencies. Bound each entire apt command.
            # sudo runs timeout so the root apt process is terminated as well.
            prefix = ['sudo', '-n', 'timeout', '--kill-after=10s']
            subprocess.run(prefix + ['120s', 'apt-get', *options, 'update'],
                           check=True, timeout=140)
            subprocess.run(prefix + ['180s', 'apt-get', *options, 'install', '-y',
                                     '--no-install-recommends', 'ffmpeg'], check=True, timeout=200)
            verify_runtime()
            print(f'media_runtime=verified source={mirror}', flush=True)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, ValueError) as error:
            print(f'media_runtime_install_failed mirror={mirror} error={type(error).__name__}', flush=True)
    raise RuntimeError('Media runtime installation failed on both official mirrors')


if __name__ == '__main__':
    ensure_runtime()
