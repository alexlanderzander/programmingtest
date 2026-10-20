#!/usr/bin/env python3
"""Run four synthetic Xcode metadata variants and print exact files to job logs."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / '.oracle-output'
VARIANTS = {
    'plain': [],
    'indexed': ['ORACLE_INDEXED'],
    'long-running': ['ORACLE_LONG_RUNNING'],
    'combined': ['ORACLE_INDEXED', 'ORACLE_LONG_RUNNING'],
}


def output(command):
    return subprocess.check_output(command, text=True).strip()


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This oracle requires genuine Xcode on macOS.')
    version = output(['xcodebuild', '-version'])
    match = re.search(r'^Xcode (\d+)(?:\.|$)', version, re.M)
    if not match or int(match[1]) < 27:
        raise SystemExit('Xcode 27 or later is required for LongRunningIntent.')
    sdk_version = output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'])
    if version != 'Xcode 27.1\nBuild version 27A9275' or sdk_version != '27.1':
        raise SystemExit('This proposal pins genuine Xcode 27.1 build 27A9275 / iPhoneOS 27.1.')
    if OUTPUT.exists():
        raise SystemExit('Refusing to overwrite an existing oracle run.')
    OUTPUT.mkdir()
    print('ORACLE_ENV_JSON ' + json.dumps({
        'xcode': version,
        'developer_directory': output(['xcode-select', '-p']),
        'swift': output(['xcrun', 'swiftc', '--version']),
        'iphoneos_sdk': sdk_version,
    }), flush=True)
    for variant, flags in VARIANTS.items():
        directory = OUTPUT / variant
        products = directory / 'products'
        intermediates = directory / 'intermediates'
        command = [
            'xcodebuild', '-project', str(HERE / 'OracleFixture.xcodeproj'),
            '-target', 'OracleFixture', '-configuration', 'Release',
            '-sdk', 'iphoneos', 'build', 'CODE_SIGNING_ALLOWED=NO',
            'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=',
            'SYMROOT=' + str(products), 'OBJROOT=' + str(intermediates),
            'SWIFT_ACTIVE_COMPILATION_CONDITIONS=' + ' '.join(flags),
        ]
        print('ORACLE_BUILD_JSON ' + json.dumps({'variant': variant, 'flags': flags}), flush=True)
        subprocess.run(command, check=True, cwd=HERE)
        apps = list(products.rglob('OracleFixture.app'))
        if len(apps) != 1:
            raise RuntimeError('Expected exactly one built synthetic app.')
        app = apps[0]
        metadata = app / 'Metadata.appintents'
        actions = metadata / 'extract.actionsdata'
        if not actions.is_file():
            raise RuntimeError('Genuine Xcode build did not produce App Intents metadata.')
        json.loads(actions.read_bytes())
        const_values = sorted(intermediates.rglob('*.swiftconstvalues'))
        if not const_values:
            raise RuntimeError('Xcode did not retain compiler const-values evidence.')
        files = [app / 'Info.plist', *sorted(p for p in metadata.rglob('*') if p.is_file()),
                 *const_values]
        # Logs/job summaries do not consume billed Actions artifact storage.
        # No upload-artifact action, dependency cache or credentials are used.
        for number, path in enumerate(files):
            data = path.read_bytes()
            if len(data) > 5 * 1024 * 1024:
                raise RuntimeError('Unexpectedly large synthetic oracle file.')
            relative = ('metadata/' + str(path.relative_to(metadata)) if path.is_relative_to(metadata)
                        else ('compiler/' + str(number) + '-' + path.name if path in const_values
                              else 'Info.plist'))
            print('ORACLE_FILE_JSON ' + json.dumps({
                'variant': variant, 'path': relative, 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'base64': base64.b64encode(data).decode(),
            }, separators=(',', ':')), flush=True)
        print('ORACLE_VARIANT_COMPLETE ' + variant, flush=True)


if __name__ == '__main__':
    main()
