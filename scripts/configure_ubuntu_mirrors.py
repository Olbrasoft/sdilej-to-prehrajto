"""Normalize Ubuntu runner repositories, including mirror+file lists."""
from pathlib import Path


def configure(apt_root: Path = Path('/etc/apt')) -> int:
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
        if original != updated:
            path.write_text(updated)
            changed += 1
    return changed


if __name__ == '__main__':
    print(f'ubuntu_mirror_files_updated={configure()}')
