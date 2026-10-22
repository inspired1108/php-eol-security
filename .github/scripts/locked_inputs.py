#!/usr/bin/env python3
"""Lock, fetch and verify the external inputs of the Windows build.

inputs.lock.json pins every file the build downloads (SHA-256) and every
repository it takes code from (commit). Subcommands:

  fetch   on the build runner, before the build: download what one branch and
          architecture need, check each file against the lock and fill the store
          the builder overrides (.github/builder-overrides) serve from. A file
          that is missing from the lock or does not match stops the build.
  lock    propose a complete lock: download every input, record its SHA-256 and
          check it against the publisher's own digest or signature where one
          exists. Pins (commits, versioned URLs) are kept from the current lock.
  verify  download every locked file again, for instance from your own network,
          and compare: a second vantage point for inputs without a publisher
          digest.

Standard library only; needs git, and gpg for the nginx signature in "lock".
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEPS_BASE = 'https://downloads.php.net/~windows/php-sdk/deps'
ARCHES = ('x64', 'x86')
ORACLE_PAGES = {
    'x64': 'https://www.oracle.com/database/technologies/instant-client/winx64-64-downloads.html',
    'x86': 'https://www.oracle.com/database/technologies/instant-client/microsoft-windows-32-downloads.html',
}
NGINX_KEYS_PAGE = 'https://nginx.org/en/pgp_keys.html'
COMPOSER_RELEASE = 'https://github.com/composer/composer/releases/download/{}/composer.phar'
USER_AGENT = 'php-eol-security-locked-inputs'

# PGO files the SDK fetches only when absent from its work directory, and where
# the SDK at the locked commit names their URL. "fetch" pre-seeds all of them.
PGO_SEEDS = {
    'package_cache/nginx.zip': ('pgo/tpl/nginx/phpsdk_pgo.json', 'pkg_url'),
    'package_cache/mariadb.zip': ('pgo/tpl/mariadb/phpsdk_pgo.json', 'pkg_url'),
    'package_cache/php_pgo_training_scripts_01org.zip': ('pgo/cases/pgo01org/phpsdk_pgo.json', 'pgo01org_zip_url'),
    # Replaces the download and execution of https://getcomposer.org/installer
    'tools/composer.phar': None,
}
# Training cases the seeds cover; another active case would download unlocked code
PGO_CASES = {'pgo01org'}


def fail(message):
    print(f'::error::{message}', flush=True)
    sys.exit(1)


def oracle_url(arch):
    return 'https://download.oracle.com/otn_software/nt/instantclient/instantclient-sdk-{}.zip'.format(
        'nt' if arch == 'x86' else 'windows')


def series_url(branch, vs, arch):
    return f'{DEPS_BASE}/series/packages-{branch}-{vs}-{arch}-staging.txt'


def package_url(vs, arch, package):
    return f'{DEPS_BASE}/{vs}/{arch}/{package}'


def series_packages(text):
    # Same split as Get-PhpDepsPackages.ps1
    return [line for line in re.split(r'[\r\n]+', text) if line.strip()]


def open_url(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    return urllib.request.urlopen(request, timeout=120)


def download(url, dest, required=True):
    """Download url to dest, returning (sha256, size), or None if not required."""
    for attempt in range(3):
        try:
            digest, size = hashlib.sha256(), 0
            with open_url(url) as response, open(dest, 'wb') as out:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            return digest.hexdigest(), size
        except OSError as error:
            if attempt == 2:
                if required:
                    fail(f'download of {url} failed: {error}')
                print(f'download of {url} failed: {error}')
                return None
            time.sleep(5 * (attempt + 1))


def read_url(url, required=True):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'content'
        return path.read_bytes() if download(url, path, required) else None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args, capture=False):
    result = subprocess.run(['git', *args], text=True, stdout=subprocess.PIPE if capture else None)
    if result.returncode:
        fail(f'git {" ".join(args)} failed')
    return result.stdout.strip() if capture else None


def fetch_commit(repo, commit, dest):
    """Check out exactly commit; git verifies every object against its hash."""
    git('init', '-q', str(dest))
    git('-C', str(dest), 'fetch', '-q', '--depth', '1', '--no-tags', repo, commit)
    git('-C', str(dest), '-c', 'core.autocrlf=false', '-c', 'advice.detachedHead=false',
        'checkout', '-q', '--detach', commit)
    head = git('-C', str(dest), 'rev-parse', 'HEAD', capture=True)
    if head != commit:
        fail(f'{repo}: checked out {head}, locked {commit}')


def remote_ref(repo, ref):
    out = git('ls-remote', repo, ref, ref + '^{}', capture=True).splitlines()
    refs = dict(reversed(line.split('\t')) for line in out)
    # An annotated tag resolves to its commit through ^{}
    return refs.get(ref + '^{}') or refs.get(ref)


def remove_tree(path):
    def writable(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def load_lock(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def git_entry(lock, role):
    entries = [(url, e) for url, e in lock['git'].items() if e['role'] == role and 'seed' not in e]
    if len(entries) != 1:
        fail(f'inputs.lock.json needs exactly one git entry with role {role}')
    return entries[0]


def check_pgo(lock, sdk):
    """The seeds must cover what the SDK at the locked commit would download."""
    cases = {p.name for p in (sdk / 'pgo' / 'cases').iterdir() if not (p / 'inactive').exists()}
    if cases != PGO_CASES:
        fail(f'active PGO training cases are {sorted(cases)}, the lock covers {sorted(PGO_CASES)}')
    seeds = {e['seed']: url for url, e in list(lock['files'].items()) + list(lock['git'].items()) if 'seed' in e}
    if set(seeds) != set(PGO_SEEDS):
        fail(f'PGO seeds in inputs.lock.json are {sorted(seeds)}, expected {sorted(PGO_SEEDS)}')
    for seed, source in PGO_SEEDS.items():
        if source:
            with open(sdk / source[0], encoding='utf-8') as f:
                url = json.load(f)[source[1]]
            if seeds[seed] != url:
                fail(f'{source[0]} downloads {url}, inputs.lock.json seeds {seed} from {seeds[seed]}')
    return seeds


def cmd_fetch(args):
    lock = load_lock(args.lock)
    with open(args.vs_json, encoding='utf-8') as f:
        vs = json.load(f)['php'][args.branch]
    store = Path(args.store)
    if store.exists():
        remove_tree(store)
    (store / 'files').mkdir(parents=True)
    index = {'files': {}, 'seeds': [], 'sdk': None}

    def get(url):
        entry = lock['files'].get(url)
        if not entry:
            fail(f'{url} is not in inputs.lock.json: run the "Lock inputs" workflow and review its proposal')
        if not entry.get('sha256'):
            fail(f'{url} has no sha256 in inputs.lock.json: run the "Lock inputs" workflow and review its proposal')
        path = store / 'files' / entry['sha256']
        if not path.exists():
            source = entry.get('fetch', url)
            tmp = path.with_suffix('.part')
            sha256, size = download(source, tmp)
            if sha256 != entry['sha256']:
                fail(f'{source}: SHA-256 {sha256}, locked {entry["sha256"]}')
            tmp.rename(path)
            print(f'ok {sha256} {size:>10} {source}')
        index['files'][url] = {'path': f'files/{entry["sha256"]}', 'sha256': entry['sha256']}
        return path

    # PHP SDK, from git: the archive the builder downloads is not byte-stable
    url, entry = git_entry(lock, 'php-sdk')
    sdk = store / 'php-sdk'
    fetch_commit(entry['repo'], entry['commit'], sdk)
    remove_tree(sdk / '.git')
    index['sdk'] = {'path': 'php-sdk', 'commit': entry['commit']}
    print(f'ok {entry["commit"]} {entry["repo"]}')

    # Dependency libraries: the locked series listing fixes the package list
    listing = get(series_url(args.branch, vs, args.arch))
    for package in series_packages(listing.read_bytes().decode('utf-8-sig')):
        get(package_url(vs, args.arch, package))

    get(oracle_url(args.arch))

    # PGO training: servers, training scripts and tools the SDK would download
    seeds = check_pgo(lock, sdk)
    for seed, url in sorted(seeds.items()):
        if url in lock['files']:
            path = get(url)
        else:
            entry = lock['git'][url]
            with tempfile.TemporaryDirectory() as tmp:
                fetch_commit(entry['repo'], entry['commit'], Path(tmp) / 'repo')
                path = store / 'files' / Path(seed).name
                git('-C', str(Path(tmp) / 'repo'), 'archive', '--format=zip', '--prefix=' + entry['prefix'],
                    '-o', str(path.resolve()), entry['commit'])
                remove_tree(Path(tmp) / 'repo')
            print(f'ok {entry["commit"]} {entry["repo"]}')
        index['seeds'].append({'path': path.relative_to(store).as_posix(), 'sha256': sha256_file(path), 'target': seed})

    with open(store / 'index.json', 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2)
    print(f'{len(index["files"])} locked files, {len(index["seeds"])} PGO seeds and the PHP SDK in {store}')


def oracle_published_sha256(arch, filename):
    page = read_url(ORACLE_PAGES[arch], required=False)
    if page is None:
        return None
    text = re.sub(r'<[^>]+>', ' ', html.unescape(page.decode('utf-8', 'replace')))
    match = re.search(re.escape(filename) + r'[^<]{0,300}?SHA256\s*([0-9a-f]{64})', text)
    return match.group(1) if match else None


def nginx_signer(url, path):
    """Fingerprint of the key that signed url, or None when it cannot be checked."""
    page = read_url(NGINX_KEYS_PAGE, required=False)
    asc = read_url(url + '.asc', required=False)
    if not shutil.which('gpg') or page is None or asc is None:
        return None
    with tempfile.TemporaryDirectory() as home:
        for key in sorted(set(re.findall(r'href="([^"]+\.key)"', page.decode('utf-8', 'replace')))):
            subprocess.run(['gpg', '--homedir', home, '--batch', '--quiet', '--import'],
                           input=read_url(urllib.parse.urljoin(NGINX_KEYS_PAGE, key), required=False) or b'',
                           capture_output=True)
        signature = Path(home) / 'signature.asc'
        signature.write_bytes(asc)
        result = subprocess.run(['gpg', '--homedir', home, '--batch', '--status-fd', '1', '--verify',
                                 str(signature), str(path)], capture_output=True, text=True)
        match = re.search(r'^\[GNUPG:\] VALIDSIG ([0-9A-F]{40})', result.stdout, re.M)
        if not match:
            fail(f'{url}: OpenPGP signature does not verify\n{result.stdout}{result.stderr}')
        return match.group(1)


def cmd_lock(args):
    lock = load_lock(args.lock)
    with open(args.versions, encoding='utf-8') as f:
        branches = list(json.load(f))
    with open(args.vs_json, encoding='utf-8') as f:
        vs_by_branch = json.load(f)['php']
    proposal = {k: v for k, v in lock.items() if k not in ('git', 'files')}
    proposal['git'] = json.loads(json.dumps(lock['git']))
    proposal['files'] = {}
    report = ['| Input | SHA-256 / commit | Independent check |', '|---|---|---|']
    alerts = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        def record(url, role, verified_by, check=None, **extra):
            old = lock['files'].get(url, {})
            fetch = extra.get('fetch') or old.get('fetch')
            path = tmp / 'download'
            sha256, size = download(fetch or url, path)
            if check:
                verified_by = check(path, sha256) or verified_by
            if old.get('sha256') and old['sha256'] != sha256 and old.get('fetch') == fetch:
                alerts.append(f'`{fetch or url}` changed: locked {old["sha256"]}, now {sha256}')
            entry = {'role': role}
            for key in ('fetch', 'seed'):
                if extra.get(key) or old.get(key):
                    entry[key] = extra.get(key) or old.get(key)
            entry.update(sha256=sha256, size=size, verified_by=verified_by)
            proposal['files'][url] = entry
            report.append(f'| {fetch or url} | `{sha256}` | {verified_by} |')
            return path

        # Dependency libraries of every branch built here
        no_digest = 'none published: compare with `verify` from a second network'
        for branch in branches:
            for arch in ARCHES:
                vs = vs_by_branch[branch]
                listing = record(series_url(branch, vs, arch), 'deps-series', no_digest)
                for package in series_packages(listing.read_bytes().decode('utf-8-sig')):
                    url = package_url(vs, arch, package)
                    if url not in proposal['files']:
                        record(url, 'deps', no_digest)

        # Oracle Instant Client SDK: versioned file, checked against Oracle's page
        for arch in ARCHES:
            url = oracle_url(arch)
            fetch = lock['files'].get(url, {}).get('fetch')
            if not fetch:
                fail(f'{url} needs a versioned "fetch" URL in inputs.lock.json')

            def oracle_check(path, sha256, arch=arch, fetch=fetch):
                published = oracle_published_sha256(arch, fetch.rsplit('/', 1)[1])
                if published != sha256:
                    alerts.append(f'`{fetch}`: SHA-256 {sha256}, Oracle publishes {published}')
                    return f'**MISMATCH with Oracle ({published})**'
                return f'matches the SHA-256 published on {ORACLE_PAGES[arch]}'
            record(url, 'oracle-sdk', 'not checked', oracle_check, fetch=fetch)

        # Git pins: the commit is the lock; report where the upstream ref now points
        for url, entry in proposal['git'].items():
            current = remote_ref(entry['repo'], entry['ref'])
            note = 'git commit' if current == entry['commit'] else f'**{entry["ref"]} now at {current}**'
            if entry['role'] == 'php-sdk' and current != entry['commit']:
                alerts.append(f'{entry["repo"]} {entry["ref"]} moved from {entry["commit"]} to {current}')
            report.append(f'| {entry["repo"]} | `{entry["commit"]}` | {note} |')

        # PGO training inputs, as named by the SDK at the locked commit
        url, entry = git_entry(lock, 'php-sdk')
        sdk = tmp / 'php-sdk'
        fetch_commit(entry['repo'], entry['commit'], sdk)
        seeds = check_pgo(lock, sdk)
        for seed, url in sorted(seeds.items()):
            if url in proposal['git']:
                continue
            if seed == 'package_cache/nginx.zip':
                def check(path, sha256, url=url):
                    signer = nginx_signer(url, path)
                    if not signer:
                        alerts.append(f'`{url}`: OpenPGP signature not checked')
                        return '**signature not checked**'
                    return f'OpenPGP signature by `{signer}`: compare with {NGINX_KEYS_PAGE}'
            elif seed == 'package_cache/mariadb.zip':
                def check(path, sha256, url=url):
                    sums = (read_url(url.rsplit('/', 1)[0] + '/sha256sums.txt', required=False) or b'').decode()
                    published = re.search(r'^([0-9a-f]{64})\s+\*?' + re.escape(url.rsplit('/', 1)[1]) + r'\s*$', sums, re.M)
                    if not published or published.group(1) != sha256:
                        alerts.append(f'`{url}` does not match sha256sums.txt')
                        return '**MISMATCH with sha256sums.txt**'
                    return 'matches sha256sums.txt (same server)'
            else:
                def check(path, sha256, url=url):
                    version = url.split('/')[-2]
                    published = (read_url(url + '.sha256sum', required=False) or b'-').decode().split()[0]
                    release = hashlib.sha256(read_url(COMPOSER_RELEASE.format(version), required=False) or b'').hexdigest()
                    if published != sha256 or release != sha256:
                        alerts.append(f'`{url}`: {sha256}, .sha256sum {published}, GitHub release {release}')
                        return '**MISMATCH**'
                    return 'matches getcomposer.org .sha256sum and the GitHub release asset'
            record(url, 'pgo', 'not checked', check, seed=seed)

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(proposal, f, indent=2)
        f.write('\n')
    summary = ['## Proposed inputs.lock.json', '']
    if alerts:
        summary += ['### :warning: Review before committing', ''] + [f'- {a}' for a in alerts] + ['']
    summary += report
    with open(args.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(summary) + '\n')
    print('\n'.join(summary))
    if alerts:
        sys.exit(2)


def cmd_verify(args):
    lock = load_lock(args.lock)
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for url, entry in lock['files'].items():
            source = entry.get('fetch', url)
            if not entry.get('sha256'):
                print(f'MISSING          {source}')
                bad += 1
                continue
            sha256, _ = download(source, Path(tmp) / 'download')
            ok = sha256 == entry['sha256']
            bad += not ok
            print(f'{"ok      " if ok else "MISMATCH"} {sha256} {source}')
        for url, entry in lock['git'].items():
            fetch_commit(entry['repo'], entry['commit'], Path(tmp) / url.split('/')[4])
            print(f'ok       {entry["commit"]} {entry["repo"]}')
    print(f'{bad} problem(s)')
    sys.exit(1 if bad else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--lock', default='inputs.lock.json')
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch')
    fetch.add_argument('--branch', required=True)
    fetch.add_argument('--arch', required=True, choices=ARCHES)
    fetch.add_argument('--vs-json', required=True, help="the builder's config/vs.json")
    fetch.add_argument('--store', required=True)
    lock = sub.add_parser('lock')
    lock.add_argument('--versions', default='versions.json')
    lock.add_argument('--vs-json', required=True, help="the builder's config/vs.json")
    lock.add_argument('--out', required=True)
    lock.add_argument('--report', required=True)
    sub.add_parser('verify')
    args = parser.parse_args()
    {'fetch': cmd_fetch, 'lock': cmd_lock, 'verify': cmd_verify}[args.command](args)


if __name__ == '__main__':
    main()
