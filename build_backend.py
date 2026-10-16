"""Small dependency-free PEP 517 wheel builder for local pip install ."""
import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.2.0'
DIST = f'kinesift-{VERSION}.dist-info'
SOURCES = ('__init__.py', 'cli.py', 'api.py', 'service.py',
           'environment.py', 'oracle.py', 'run.py', 'contract.json', 'observe.py', 'sensor.py', 'harbor.py')


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = f'kinesift-{VERSION}-py3-none-any.whl'
    files = {f'kinesift/{source}': (ROOT / source).read_bytes() for source in SOURCES}
    files[f'{DIST}/METADATA'] = (
        'Metadata-Version: 2.4\nName: kinesift\n'
        f'Version: {VERSION}\n'
        'Summary: Host-side container observations and resettable agent task checks\n'
        'Requires-Python: >=3.10\n'
        'License-Expression: Apache-2.0\nLicense-File: LICENSE\nLicense-File: NOTICE\n'
        'Author-email: Kinesift Team <hello@kinesift.com>\n'
        'Description-Content-Type: text/markdown\n\n'
        + (ROOT / 'README.md').read_text(encoding='utf-8')).encode('utf-8')
    for legal in ('LICENSE', 'NOTICE'):
        files[f'{DIST}/licenses/{legal}'] = (ROOT / legal).read_bytes()
    files[f'{DIST}/WHEEL'] = (
        'Wheel-Version: 1.0\nGenerator: kinesift-local-build\n'
        'Root-Is-Purelib: true\nTag: py3-none-any\n').encode('ascii')
    files[f'{DIST}/entry_points.txt'] = b'[console_scripts]\nkinesift = kinesift.cli:main\n'
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode('ascii')
        writer.writerow([path, 'sha256=' + digest, len(data)])
    writer.writerow([f'{DIST}/RECORD', '', ''])
    files[f'{DIST}/RECORD'] = record.getvalue().encode('utf-8')
    with zipfile.ZipFile(Path(wheel_directory) / name, 'w', zipfile.ZIP_DEFLATED) as wheel:
        for path, data in files.items():
            wheel.writestr(path, data)
    return name
