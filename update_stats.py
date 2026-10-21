import hashlib
import http.client
import json
import os
import random
import time
import urllib.error
import urllib.request
from lxml import etree

USER_NAME = os.environ.get('USER_NAME', 'AhmedMalekX')
ACCESS_TOKEN = os.environ['ACCESS_TOKEN']
API_URL = 'https://api.github.com/graphql'
HEADERS = {
    'Authorization': f'bearer {ACCESS_TOKEN}',
    'Content-Type': 'application/json',
}
CACHE_FILE = 'cache/' + hashlib.sha256(USER_NAME.encode()).hexdigest() + '.txt'
CACHE_COMMENT = [
    '# Cache of per-repo commit/LOC stats for the profile README.\n',
    '# format: repo_hash total_commit_count my_commits additions deletions\n',
]


REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 6
MAX_RATE_LIMIT_WAIT = 900
MIN_PAGE_SIZE = 5
RETRY_STATUSES = {500, 502, 503, 504}
PERMANENT_GRAPHQL_ERRORS = {'NOT_FOUND', 'FORBIDDEN'}


class TransientError(Exception):
    """A failure worth retrying (GitHub 5xx, timeout, rate limit). `wait` overrides the backoff."""

    def __init__(self, message, wait=None):
        super().__init__(message)
        self.wait = wait


def rate_limit_wait(headers):
    """Seconds GitHub asks us to wait, or None if the response isn't a rate limit."""
    if headers.get('Retry-After'):
        return int(headers['Retry-After'])
    if headers.get('X-RateLimit-Remaining') == '0' and headers.get('X-RateLimit-Reset'):
        return max(1, int(headers['X-RateLimit-Reset']) - int(time.time()))
    return None


def post(query, variables):
    """One GraphQL request. Raises TransientError for anything a retry might fix."""
    body = json.dumps({'query': query, 'variables': variables}).encode()
    request = urllib.request.Request(API_URL, data=body, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code in RETRY_STATUSES:
            raise TransientError(f'HTTP {e.code} {e.reason}') from e
        wait = rate_limit_wait(e.headers) if e.code in (403, 429) else None
        if wait is not None:
            raise TransientError(f'HTTP {e.code} rate limited', wait=wait) from e
        raise  # 401 bad/expired token, 404, etc. -- retrying won't help
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
            TimeoutError, json.JSONDecodeError) as e:
        raise TransientError(f'{type(e).__name__}: {e}') from e
    # GitHub reports query timeouts and rate limits as HTTP 200 with an `errors` list;
    # never treat that as an empty result, or zeros get cached as real stats.
    errors = payload.get('errors') or []
    types = {err.get('type') for err in errors}
    if errors and types <= PERMANENT_GRAPHQL_ERRORS and payload.get('data') is not None:
        # A repo deleted mid-run or an org the token can't see: retrying won't help, and
        # `data` still holds everything else (the affected fields are null).
        return payload['data']
    if errors or payload.get('data') is None:
        # Typed messages name the repo/org, and Actions logs are public, so log only the type.
        # Untyped errors (query timeouts) carry generic text that is safe and useful.
        details = '; '.join(err.get('type') or err.get('message', '?') for err in errors)
        raise TransientError(f'GraphQL error: {details or "no data"}')
    return payload['data']


def gql(query, variables, on_retry=None):
    """POST with exponential backoff. `on_retry(variables)` may return lighter variables."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return post(query, variables)
        except TransientError as e:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f'GitHub API still failing after {MAX_ATTEMPTS} attempts: {e}') from e
            if e.wait is not None:
                if e.wait > MAX_RATE_LIMIT_WAIT:
                    raise RuntimeError(f'Rate limited for {e.wait}s, giving up: {e}') from e
                delay = e.wait + 1
            else:
                delay = min(60, 2 ** attempt) + random.uniform(0, 1)
            if on_retry:
                variables = on_retry(variables)
            print(f'  {e} -- retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.0f}s', flush=True)
            time.sleep(delay)


def shrink_page(variables):
    """Big pages are what make GitHub time out (502/504), so halve the page on each retry."""
    return {**variables, 'first': max(MIN_PAGE_SIZE, variables['first'] // 2)}


def get_owner_id():
    query = '''
    query($login: String!) {
        user(login: $login) { id }
    }'''
    return gql(query, {'login': USER_NAME})['user']['id']


def get_repos():
    """All repos I own, collaborate on, or belong to via an org, excluding forks."""
    query = '''
    query($login: String!, $first: Int!, $cursor: String) {
        user(login: $login) {
            repositories(first: $first, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], isFork: false) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        defaultBranchRef { target { ... on Commit { history { totalCount } } } }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    edges = []
    cursor = None
    total_count = 0
    while True:
        variables = {'login': USER_NAME, 'first': 25, 'cursor': cursor}
        data = gql(query, variables, on_retry=shrink_page)['user']['repositories']
        total_count = data['totalCount']
        edges.extend(data['edges'])
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']
    return total_count, edges


def walk_repo_commits(owner_id, full_name):
    """Sums commits/additions/deletions authored by me on the default branch."""
    query = '''
    query($owner: String!, $name: String!, $first: Int!, $cursor: String) {
        repository(owner: $owner, name: $name) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: $first, after: $cursor) {
                            edges { node { additions deletions author { user { id } } } }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    owner, name = full_name.split('/', 1)
    commits = additions = deletions = 0
    cursor = None
    while True:
        variables = {'owner': owner, 'name': name, 'first': 50, 'cursor': cursor}
        repo = gql(query, variables, on_retry=shrink_page)['repository']
        if not repo:
            return None  # NOT_FOUND / FORBIDDEN: no usable stats, so the caller won't cache it
        if not repo.get('defaultBranchRef'):
            return commits, additions, deletions
        history = repo['defaultBranchRef']['target']['history']
        for edge in history['edges']:
            author = edge['node']['author']['user']
            if author and author['id'] == owner_id:
                commits += 1
                additions += edge['node']['additions']
                deletions += edge['node']['deletions']
        if not history['pageInfo']['hasNextPage']:
            return commits, additions, deletions
        cursor = history['pageInfo']['endCursor']


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE) as f:
        lines = f.readlines()[len(CACHE_COMMENT):]
    cache = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 5:
            repo_hash, total, commits, adds, dels = parts
            cache[repo_hash] = (int(total), int(commits), int(adds), int(dels))
    return cache


def save_cache(cache):
    os.makedirs('cache', exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        f.writelines(CACHE_COMMENT)
        for repo_hash, (total, commits, adds, dels) in cache.items():
            f.write(f'{repo_hash} {total} {commits} {adds} {dels}\n')


def justify_format(root, element_id, value, target_len=0):
    """Right-align a value by padding the sibling '<id>_dots' element with dots."""
    text = f'{value:,}' if isinstance(value, int) else str(value)
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = text
    just_len = max(0, target_len - len(text))
    dot_map = {0: '', 1: ' ', 2: '. '}
    dots = dot_map[just_len] if just_len <= 2 else ' ' + ('.' * just_len) + ' '
    dots_element = root.find(f".//*[@id='{element_id}_dots']")
    if dots_element is not None:
        dots_element.text = dots


def update_svg(filename, repos, commits, additions, deletions, net_loc):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'repo_data', repos, 10)
    justify_format(root, 'commit_data', commits, 8)
    justify_format(root, 'loc_data', net_loc, 11)
    justify_format(root, 'loc_add', additions)
    justify_format(root, 'loc_del', deletions, 11)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def main():
    owner_id = get_owner_id()
    repo_count, repo_edges = get_repos()
    cache = load_cache()

    total_commits = total_additions = total_deletions = 0
    try:
        for edge in repo_edges:
            node = edge['node']
            if not node:
                continue  # nulled out by a FORBIDDEN error (e.g. org SSO the token isn't authorized for)
            full_name = node['nameWithOwner']
            branch = node.get('defaultBranchRef')
            current_total = branch['target']['history']['totalCount'] if branch and branch.get('target') else 0
            repo_hash = hashlib.sha256(full_name.encode()).hexdigest()

            cached = cache.get(repo_hash)
            if cached and cached[0] == current_total:
                _, commits, additions, deletions = cached
            else:
                # Name stays out of the log: Actions logs are public and some repos are private.
                print(f'Walking repo {repo_hash[:8]} ({current_total} commits)', flush=True)
                stats = walk_repo_commits(owner_id, full_name)
                if stats is None:
                    print(f'  repo {repo_hash[:8]} not accessible, skipping', flush=True)
                    continue
                commits, additions, deletions = stats
                cache[repo_hash] = (current_total, commits, additions, deletions)

            total_commits += commits
            total_additions += additions
            total_deletions += deletions
    finally:
        # Keep the repos walked so far even if a later one fails, so the next run resumes.
        save_cache(cache)
    net_loc = total_additions - total_deletions

    update_svg('dark_mode.svg', repo_count, total_commits, total_additions, total_deletions, net_loc)
    update_svg('light_mode.svg', repo_count, total_commits, total_additions, total_deletions, net_loc)

    print(f'repos={repo_count} commits={total_commits} +{total_additions} -{total_deletions} net={net_loc}')


if __name__ == '__main__':
    main()
