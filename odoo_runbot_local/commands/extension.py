"""Build and sign the browser extension.

chrome/ and firefox/ are assembled from one shared source; keeping a single copy
of the logic is what stops the two builds drifting apart, which is how an MV2
popup once ended up inside an MV3 package.
"""
import json
import os
import shutil
import subprocess

from .. import config as cfg
from .. import ui
from ..platform import proc

SHARED_FILES = ('content.js', 'background.js', 'popup.html', 'popup.js')
BROWSERS = ('chrome', 'firefox')


def source_dir():
    return os.path.join(cfg.REPO_ROOT, 'extension')


def build(destination=None):
    """Assemble per-browser packages. Returns the destination directory."""
    destination = destination or os.path.join(cfg.REPO_ROOT, 'build', 'extension')
    shared = os.path.join(source_dir(), 'shared')

    for name in SHARED_FILES:
        path = os.path.join(shared, name)
        if not os.path.exists(path):
            raise FileNotFoundError(f'missing {path}')

    for browser in BROWSERS:
        manifest = os.path.join(source_dir(), browser, 'manifest.json')
        if not os.path.exists(manifest):
            raise FileNotFoundError(f'missing {manifest}')

        target = os.path.join(destination, browser)
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target)
        shutil.copy2(manifest, os.path.join(target, 'manifest.json'))
        for name in SHARED_FILES:
            shutil.copy2(os.path.join(shared, name), os.path.join(target, name))

    return destination


def _manifest_info():
    path = os.path.join(source_dir(), 'firefox', 'manifest.json')
    with open(path) as fh:
        manifest = json.load(fh)
    gecko = manifest.get('browser_specific_settings', {}).get('gecko', {})
    if not gecko.get('id'):
        raise ValueError('manifest has no browser_specific_settings.gecko.id — '
                         'AMO cannot sign it')
    return manifest['version'], gecko['id']


def sign(channel='unlisted'):
    """Build, lint and sign on AMO. Returns the path to the signed .xpi."""
    if not proc.which('web-ext'):
        raise RuntimeError('web-ext is not installed. Install it with: '
                           'npm install --global web-ext')

    version, addon_id = _manifest_info()
    ui.info(f'Add-on ID: {addon_id}')
    ui.info(f'Version:   {version}')
    ui.info(f'Channel:   {channel}')

    releases = os.path.join(cfg.REPO_ROOT, 'releases')
    target = os.path.join(releases, f'{cfg.APP_NAME}-{version}.xpi')
    # AMO permanently rejects a version it has already signed, so catch the
    # mistake here rather than after a failed round trip.
    if os.path.exists(target):
        raise RuntimeError(
            f'releases/{cfg.APP_NAME}-{version}.xpi already exists. AMO will not '
            f'accept a version it has already signed — bump "version" in both '
            f'extension/chrome/manifest.json and extension/firefox/manifest.json.')

    build_dir = os.path.join(cfg.REPO_ROOT, 'build', 'sign')
    shutil.rmtree(build_dir, ignore_errors=True)
    build(build_dir)
    source = os.path.join(build_dir, 'firefox')

    ui.info('Linting...')
    if not proc.stream(['web-ext', 'lint', '--source-dir', source,
                        '--warnings-as-errors=false'], timeout=600).ok:
        raise RuntimeError('lint failed — fix the errors above before signing')

    ui.info('Uploading to AMO for signing (this can take a minute)...')
    artifacts = os.path.join(build_dir, 'artifacts')
    result = proc.stream([
        'web-ext', 'sign',
        '--source-dir', source,
        '--artifacts-dir', artifacts,
        '--channel', channel,
    ], timeout=1800)
    if not result.ok:
        raise RuntimeError(
            'signing failed. Common causes:\n'
            '  * no credentials — create ~/.web-ext-config.mjs\n'
            "  * wrong channel — an add-on registered as 'listed' cannot be "
            "signed 'unlisted'\n"
            f'  * version {version} was already uploaded — bump both manifests')

    signed = next((os.path.join(artifacts, name) for name in os.listdir(artifacts)
                   if name.endswith('.xpi')), None)
    if not signed:
        raise RuntimeError(f'web-ext reported success but produced no .xpi in {artifacts}')

    # A signed package carries Mozilla's signature block; an unsigned one does not.
    listing = subprocess.run(['unzip', '-l', signed], capture_output=True, text=True)
    if 'META-INF/mozilla.rsa' not in listing.stdout:
        raise RuntimeError(f'{signed} is not signed — refusing to publish it')

    os.makedirs(releases, exist_ok=True)
    shutil.copy2(signed, target)
    return target


def run(args):
    if args.action == 'build':
        destination = build(args.output)
        for browser in BROWSERS:
            ui.ok(f'built {os.path.join(destination, browser)}')
        return 0

    try:
        path = sign(args.channel)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        ui.err(str(exc))
        return 1
    ui.ok(f'Signed extension written to {path}')
    print()
    ui.info('Install it in Firefox by opening the file, or from about:addons →')
    ui.info("gear icon → 'Install Add-on From File...'")
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('extension', help='build or sign the browser extension')
    actions = parser.add_subparsers(dest='action', metavar='ACTION', required=True)

    build_parser = actions.add_parser('build', help='assemble chrome/ and firefox/')
    build_parser.add_argument('-o', '--output', help='destination directory')

    sign_parser = actions.add_parser('sign', help='build and sign on AMO')
    sign_parser.add_argument('--channel', default='unlisted',
                             choices=['unlisted', 'listed'],
                             help='AMO channel (default: unlisted)')

    parser.set_defaults(func=run)
    return parser
