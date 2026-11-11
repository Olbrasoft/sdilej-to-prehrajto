"""Normalize Ubuntu runner repositories, including mirror+file lists."""
from pathlib import Path
import argparse
import re


def configure(apt_root: Path = Path('/etc/apt'), mirror: str = 'archive') -> int:
    hosts = {'archive': 'archive.ubuntu.com', 'azure': 'azure.archive.ubuntu.com'}
    if mirror not in hosts:
        raise ValueError('Unsupported Ubuntu mirror')
    paths = [apt_root / 'sources.list', apt_root / 'apt-mirrors.txt',
             apt_root / 'apt-mirrors-security.txt']
    directory = apt_root / 'sources.list.d'
    paths.extend(sorted(directory.glob('*.sources')))
    paths.extend(sorted(directory.glob('*.list')))
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        original = path.read_text()
        updated = original.replace('http://azure.archive.ubuntu.com/ubuntu',
                                   'https://archive.ubuntu.com/ubuntu')
        updated = updated.replace('https://azure.archive.ubuntu.com/ubuntu',
                                  'https://archive.ubuntu.com/ubuntu')
        updated = updated.replace('http://archive.ubuntu.com/ubuntu',
                                  'https://archive.ubuntu.com/ubuntu')
        updated = updated.replace('http://security.ubuntu.com/ubuntu',
                                  'https://security.ubuntu.com/ubuntu')
        if mirror == 'azure':
            # Azure's official full archive includes the security suites too.
            # Retain suites, components and Signed-By; never disable apt trust.
            updated = re.sub(r'https?://(?:azure\.archive|archive|security)\.ubuntu\.com/ubuntu(?=/|\s|$)',
                             'https://' + hosts[mirror] + '/ubuntu', updated)
        if original != updated:
            path.write_text(updated)
            changed += 1
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mirror', choices=('archive', 'azure'), default='archive')
    args = parser.parse_args()
    print(f'ubuntu_mirror_files_updated={configure(mirror=args.mirror)}')
