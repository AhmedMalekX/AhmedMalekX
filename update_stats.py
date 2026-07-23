import hashlib
import json
import os
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


def gql(query, variables):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    request = urllib.request.Request(API_URL, data=body, headers=HEADERS)
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def get_owner_id():
    query = '''
    query($login: String!) {
        user(login: $login) { id }
    }'''
    return gql(query, {'login': USER_NAME})['data']['user']['id']


def get_repos():
    """All repos I own, collaborate on, or belong to via an org, excluding forks."""
    query = '''
    query($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], isFork: false) {
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
        data = gql(query, {'login': USER_NAME, 'cursor': cursor})['data']['user']['repositories']
        total_count = data['totalCount']
        edges.extend(data['edges'])
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo']['endCursor']
    return total_count, edges


def walk_repo_commits(owner_id, full_name):
    """Sums commits/additions/deletions authored by me on the default branch."""
    query = '''
    query($owner: String!, $name: String!, $cursor: String) {
        repository(owner: $owner, name: $name) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
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
        data = gql(query, {'owner': owner, 'name': name, 'cursor': cursor})
        repo = data.get('data', {}).get('repository')
        if not repo or not repo.get('defaultBranchRef'):
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
    for edge in repo_edges:
        node = edge['node']
        full_name = node['nameWithOwner']
        branch = node.get('defaultBranchRef')
        current_total = branch['target']['history']['totalCount'] if branch else 0
        repo_hash = hashlib.sha256(full_name.encode()).hexdigest()

        cached = cache.get(repo_hash)
        if cached and cached[0] == current_total:
            _, commits, additions, deletions = cached
        else:
            commits, additions, deletions = walk_repo_commits(owner_id, full_name)
            cache[repo_hash] = (current_total, commits, additions, deletions)

        total_commits += commits
        total_additions += additions
        total_deletions += deletions

    save_cache(cache)
    net_loc = total_additions - total_deletions

    update_svg('dark_mode.svg', repo_count, total_commits, total_additions, total_deletions, net_loc)
    update_svg('light_mode.svg', repo_count, total_commits, total_additions, total_deletions, net_loc)

    print(f'repos={repo_count} commits={total_commits} +{total_additions} -{total_deletions} net={net_loc}')


if __name__ == '__main__':
    main()
