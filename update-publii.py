#!/usr/bin/env python3
"""
publii version bump: feed -> tag & commit -> generated-sources.json -> manifest & metainfo.
also diffs libsecret against the electron base app, warn only.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

APP_ID = 'net.tidycustoms.Publii'
MANIFEST = Path(APP_ID + '.yml')
METAINFO = Path(APP_ID + '.metainfo.xml')
SOURCES = Path('generated-sources.json')
FEED = 'https://notifications.getpublii.com/updates-linux.json'
UPSTREAM = 'GetPublii/Publii'
BASEAPP_YML = ('https://raw.githubusercontent.com/flathub/'
               'org.electronjs.Electron2.BaseApp/master/'
               'org.electronjs.Electron2.BaseApp.yml')

USER_AGENT = 'publii-flatpak-updater'


def fetch(url, retries=3):
    """
    http get as text, retried - both hosts hiccup too often
    """
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8')
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise SystemExit('could not fetch %s: %s' % (url, last))


def run(cmd, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise SystemExit('%s exited with %d' % (cmd[0], result.returncode))
    return result


def require_tools(*tools):
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise SystemExit('missing: ' + ', '.join(missing))


def iter_modules(manifest):
    """
    every module dict, nested ones included. string entries reference external
    files -> skipped
    """
    stack = list(manifest.get('modules') or [])
    while stack:
        mod = stack.pop(0)
        if not isinstance(mod, dict):
            continue
        yield mod
        stack.extend(mod.get('modules') or [])


def find_libsecret(manifest):
    for mod in iter_modules(manifest):
        if mod.get('name') == 'libsecret':
            return mod
    return None


def libsecret_archive(mod):
    for src in mod.get('sources') or []:
        if isinstance(src, dict) and 'libsecret' in str(src.get('url', '')):
            return src
    return {}


def libsecret_version(src):
    m = re.search(r'libsecret-(\d+(?:\.\d+)*)\.tar\.', str(src.get('url', '')))
    return m.group(1) if m else None


def parse_manifest(text, label):
    """
    safe_load also reads json, flatpak manifests come in both
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, '%s is unreadable: %s' % (label, str(exc).split('\n')[0])
    if not isinstance(data, dict):
        return None, '%s does not contain a manifest object' % label
    return data, None


def check_libsecret_drift():
    """
    diff libsecret against the base app.
    returns a note for the workflow, '' when equal
    """
    baseapp_text = fetch(BASEAPP_YML, retries=2)

    baseapp_doc, err = parse_manifest(baseapp_text, 'BaseApp manifest')
    if err:
        note = 'libsecret check: ' + err
        print(note)
        return note
    manifest_doc, err = parse_manifest(MANIFEST.read_text(encoding='utf-8'),
                                       'the manifest')
    if err:
        note = 'libsecret check: ' + err
        print(note)
        return note

    manifest_mod = find_libsecret(manifest_doc)
    baseapp_mod = find_libsecret(baseapp_doc)
    if manifest_mod is None or baseapp_mod is None:
        where = ' and '.join(
            w for w, m in (('in the manifest', manifest_mod),
                           ('in the BaseApp', baseapp_mod)) if m is None)
        note = ('libsecret check: module not found %s (restructured?) '
                '- please check manually.' % where)
        print(note)
        return note

    manifest_src = libsecret_archive(manifest_mod)
    baseapp_src = libsecret_archive(baseapp_mod)
    shown = lambda v: v if v else '(missing)'  # noqa: E731

    diffs = []
    manifest_ver = libsecret_version(manifest_src)
    baseapp_ver = libsecret_version(baseapp_src)
    if manifest_ver != baseapp_ver:
        diffs.append('  version:  manifest %s, BaseApp %s'
                     % (shown(manifest_ver), shown(baseapp_ver)))
    if manifest_src.get('sha256') != baseapp_src.get('sha256'):
        diffs.append('  sha256:   manifest %s\n            BaseApp  %s'
                     % (shown(manifest_src.get('sha256')),
                        shown(baseapp_src.get('sha256'))))
    manifest_opts = sorted(manifest_mod.get('config-opts') or [])
    baseapp_opts = sorted(baseapp_mod.get('config-opts') or [])
    only_baseapp = [o for o in baseapp_opts if o not in manifest_opts]
    only_manifest = [o for o in manifest_opts if o not in baseapp_opts]
    if only_baseapp:
        diffs.append('  config-opts only in BaseApp:  ' + ' '.join(only_baseapp))
    if only_manifest:
        diffs.append('  config-opts only in manifest: ' + ' '.join(only_manifest))

    if not diffs:
        print('libsecret check: %s, version, sha256 and config-opts match '
              'the BaseApp.' % manifest_ver)
        return ''

    note = ('libsecret differs from the Electron BaseApp:\n'
            + '\n'.join(diffs) + '\n'
            'The libsecret module here overrides the BaseApp library, so this\n'
            'is the version that ships. Differing config-opts therefore change\n'
            'the library Electron ends up loading .\n'
            'Source: ' + BASEAPP_YML)
    print(note)
    if os.environ.get('GITHUB_ACTIONS'):
        print('::warning title=libsecret differs from the BaseApp::'
              + diffs[0].strip())
    return note


def current_version():
    m = re.search(r'(?m)^\s*tag:\s*(\S+)\s*$', MANIFEST.read_text(encoding='utf-8'))
    if not m:
        raise SystemExit('no tag: line found in the manifest')
    tag = m.group(1)
    return tag, tag.removeprefix('v.').split('-build-')[0]


def latest_upstream():
    feed = json.loads(fetch(FEED))
    publii = feed.get('publii', feed)
    version = publii.get('version')
    if not version:
        raise SystemExit('feed returned no version')
    return (version, publii.get('build'),
            publii.get('description') or '',
            (publii.get('links') or {}).get('releaseNotes') or '')


def resolve_tag(version, build):
    """
    tag scheme is inconsistent (yay) usually v.<version>-build-<build>, sometimes
    just v.<version>. most specific first
    """
    tags = json.loads(fetch(
        'https://api.github.com/repos/%s/tags?per_page=100' % UPSTREAM))
    by_name = {t['name']: t['commit']['sha'] for t in tags}
    for want in ('v.%s-build-%s' % (version, build), 'v.%s' % version):
        if want in by_name:
            return want, by_name[want]
    prefix = 'v.%s-build-' % version
    for name, sha in by_name.items():
        if name.startswith(prefix):
            return name, sha
    raise SystemExit('no tag found for %s' % version)


def regenerate_sources(tag):
    with tempfile.TemporaryDirectory() as work:
        src = Path(work) / 'src'
        run(['git', 'clone', '--quiet', '--depth', '1', '--branch', tag,
             'https://github.com/%s.git' % UPSTREAM, str(src)])
        print('generating %s ...' % SOURCES)
        # --recursive: both package-lock.json (root & app/)
        # --electron-node-headers: headers for node-gyp
        run(['flatpak-node-generator', '--recursive', '--electron-node-headers',
             '--output', str(SOURCES.resolve()),
             'npm', str(src / 'package-lock.json')])


def write_manifest(tag, commit):
    """
    replace those two lines only, so the comments survive
    """
    text = MANIFEST.read_text(encoding='utf-8')
    text, n_tag = re.subn(r'(?m)^(\s*tag:\s*).*$', lambda m: m.group(1) + tag,
                          text, count=1)
    text, n_com = re.subn(r'(?m)^(\s*commit:\s*).*$', lambda m: m.group(1) + commit,
                          text, count=1)
    if n_tag != 1 or n_com != 1:
        raise SystemExit('manifest: tag/commit not uniquely identifiable')
    MANIFEST.write_text(text, encoding='utf-8')


def write_metainfo(version, notes, notes_url, today=None):
    """
    prepend a release entry. no-op if that version is already listed
    """
    tree = ET.parse(METAINFO)
    root = tree.getroot()
    releases = root.find('releases')
    if releases is None:
        releases = ET.SubElement(root, 'releases')
    if any(r.get('version') == version for r in releases.findall('release')):
        return False

    rel = ET.Element('release')
    rel.set('version', version)
    rel.set('date', (today or datetime.date.today()).isoformat())
    desc = ET.SubElement(rel, 'description')
    ET.SubElement(desc, 'p').text = notes.strip() or 'Upstream release %s.' % version
    if notes_url:
        url = ET.SubElement(rel, 'url')
        url.set('type', 'details')
        url.text = notes_url
    releases.insert(0, rel)
    ET.indent(tree, space='  ')
    tree.write(METAINFO, encoding='UTF-8', xml_declaration=True)
    with METAINFO.open('a', encoding='utf-8') as fh:
        fh.write('\n')
    return True


def emit_github_output(**values):
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as fh:
        for key, value in values.items():
            if '\n' in str(value):
                fh.write('%s<<PUBLII_EOF\n%s\nPUBLII_EOF\n' % (key, value))
            else:
                fh.write('%s=%s\n' % (key, value))


def main():
    os.chdir(Path(__file__).resolve().parent)
    require_tools('git', 'flatpak-node-generator')

    libsecret_note = check_libsecret_drift()

    cur_tag, cur_version = current_version()
    print('manifest is at:  %s (%s)' % (cur_version, cur_tag))

    version, build, notes, notes_url = latest_upstream()
    print('upstream says:   %s (build %s)' % (version, build))

    if version == cur_version:
        print('up to date, nothing changed.')
        return

    tag, commit = resolve_tag(version, build)
    print('tag:    %s' % tag)
    print('commit: %s' % commit)

    regenerate_sources(tag)
    write_manifest(tag, commit)
    write_metainfo(version, notes, notes_url)

    emit_github_output(
        version=version, tag=tag, changed='true',
        libsecret_note=libsecret_note or 'libsecret: no drift.')

    print()
    print('done: %s -> %s' % (cur_version, version))


if __name__ == '__main__':
    main()
