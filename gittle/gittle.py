# From the future
from __future__ import absolute_import

# Python imports
import os
import copy
import logging
from hashlib import sha1
from shutil import rmtree
from functools import partial, wraps

# Dulwich imports
from dulwich.repo import Repo as DulwichRepo
from dulwich.client import get_transport_and_path
from dulwich.index import build_index_from_tree, changes_from_tree
from dulwich.objects import Tree

# Funky imports
import funky

# Local imports
from gittle.auth import GittleAuth
from gittle.exceptions import InvalidRemoteUrl
from gittle import utils


# Exports
__all__ = ('Gittle',)


# Useful decorators
# A better way to do this in the future would maybe to use Mixins
def working_only(method):
    @wraps(method)
    def f(self, *args, **kwargs):
        assert self.is_working, "%s can not be called on a bare repository" % method.func_name
        return method(self, *args, **kwargs)
    return f


def bare_only(method):
    @wraps(method)
    def f(self, *args, **kwargs):
        assert self.is_bare, "%s can not be called on a working repository" % method.func_name
        return method(self, *args, **kwargs)
    return f


class Gittle(object):
    """All paths used in Gittle external methods must be paths relative to the git repository
    """
    DEFAULT_COMMIT = 'HEAD'
    DEFAULT_BRANCH = 'master'
    DEFAULT_REMOTE = 'origin'
    DEFAULT_MESSAGE = '**No Message**'
    DEFAULT_USER_INFO = {
        'name': None,
        'email': None,
    }

    DIFF_FUNCTIONS = {
        'classic': utils.git.classic_tree_diff,
        'dict': utils.git.dict_tree_diff,
        'changes': utils.git.dict_tree_diff
    }
    DEFAULT_DIFF_TYPE = 'dict'

    HIDDEN_REGEXES = [
        # Hide git directory
        r'.*\/\.git\/.*',
    ]

    # References
    REFS_BRANCHES = 'refs/heads/'
    REFS_REMOTES = 'refs/remotes/'
    REFS_TAGS = 'refs/tags/'

    # Name pattern truths
    # Used for detecting if files are :
    # - deleted
    # - added
    # - changed
    PATTERN_ADDED = (False, True)
    PATTERN_REMOVED = (True, False)
    PATTERN_MODIFIED = (True, True)

    # Permissions
    MODE_DIRECTORY = 040000  # Used to tell if a tree entry is a directory

    # Tree depth
    MAX_TREE_DEPTH = 1000

    # Acceptable Root paths
    ROOT_PATHS = (os.path.curdir, os.path.sep)

    def __init__(self, repo_or_path, origin_uri=None, auth=None, *args, **kwargs):
        if isinstance(repo_or_path, DulwichRepo):
            self.repo = repo_or_path
        elif isinstance(repo_or_path, Gittle):
            self.repo = DulwichRepo(repo_or_path.path)
        elif isinstance(repo_or_path, basestring):
            path = os.path.abspath(repo_or_path)
            self.repo = DulwichRepo(path)
        else:
            logging.warning('Repo is of type %s' % type(repo_or_path))
            raise Exception('Gittle must be initialized with either a dulwich repository or a string to the path')

        # Set path
        self.path = self.repo.path

        # The remote url
        self.origin_uri = origin_uri

        # Build ignore filter
        self.hidden_regexes = copy.copy(self.HIDDEN_REGEXES)
        self.hidden_regexes.extend(self._get_ignore_regexes())
        self.ignore_filter = utils.paths.path_filter_regex(self.hidden_regexes)
        self.filters = [
            self.ignore_filter,
        ]

        # Get authenticator
        if auth:
            self.authenticator = auth
        else:
            self.auth(*args, **kwargs)

    def _format_author(self, name, email):
        return "%s <%s>" % (name, email)

    def _format_userinfo(self, userinfo):
        name = userinfo.get('name')
        email = userinfo.get('email')
        if name and email:
            return self._format_author(name, email)
        return None

    def _format_ref(self, base, extra):
        return ''.join([base, extra])

    def _format_ref_branch(self, branch_name):
        return self._format_ref(self.REFS_BRANCHES, branch_name)

    def _format_ref_remote(self, remote_name):
        return self._format_ref(self.REFS_REMOTES, remote_name)

    def _format_ref_tag(self, tag_name):
        return self._format_ref(self.REFS_TAGS, tag_name)

    @property
    def head(self):
        """Return SHA of the current HEAD
        """
        return self.repo.head()

    @property
    def is_bare(self):
        """Bare repositories have no working directories or indexes
        """
        return self.repo.bare

    @property
    def is_working(self):
        return not(self.is_bare)

    def has_index(self):
        """Opposite of is_bare
        """
        return self.repo.has_index()

    @property
    def has_commits(self):
        """
        If the repository has no HEAD we consider that is has no commits
        """
        try:
            self.repo.head()
        except KeyError:
            return False
        return True

    @property
    def walker(self):
        """
        Very simple, basic walker
        """
        return self.repo.revision_history(self.repo.head())

    def commit_info(self, start=0, end=None):
        """Return a generator of commits with all their attached information
        """
        if self.has_commits:
            commits = [utils.git.commit_info(entry) for entry in self.walker]
            if not end:
                return commits
            return commits[start:end]
        return []

    @property
    def commit_count(self):
        return len(self.walker)

    def commits(self):
        """Return a list of SHAs for all the concerned commits
        """
        return [commit['sha'] for commit in self.commit_info()]

    @property
    def git_dir(self):
        return self.repo.controldir()

    def auth(self, *args, **kwargs):
        self.authenticator = GittleAuth(*args, **kwargs)
        return self.authenticator

    # Generate a branch selector (used for pushing)
    def _wants_branch(self, branch_name=None):
        branch_name = branch_name or self.DEFAULT_BRANCH

        def wants_func(old):
            refs_key = "refs/heads/%s" % branch_name
            return {
                refs_key: self.repo.refs["HEAD"]
            }
        return wants_func

    def _get_ignore_regexes(self):
        gitignore_filename = os.path.join(self.path, '.gitignore')
        if not os.path.exists(gitignore_filename):
            return []
        lines = open(gitignore_filename).readlines()
        globers = map(lambda line: line.rstrip(), lines)
        return utils.paths.globers_to_regex(globers)

    # Get the absolute path for a file in the git repo
    def abspath(self, repo_file):
        return os.path.abspath(
            os.path.join(self.path, repo_file)
        )

    # Get the relative path from the absolute path
    def relpath(self, abspath):
        return os.path.relpath(abspath, self.path)

    @property
    def last_commit(self):
        return self[self.repo.head()]

    @property
    def index(self):
        return self.repo.open_index()

    @classmethod
    def init(cls, path):
        """Initialize a repository"""
        repo = DulwichRepo.init(path)
        return cls(repo)

    @classmethod
    def init_bare(cls, path):
        repo = DulwichRepo.init_bare(path)
        return cls(repo)

    def get_client(self, origin_uri=None, **kwargs):
        # Get the remote URL
        origin_uri = origin_uri or self.origin_uri

        # Fail if inexistant
        if not origin_uri:
            raise InvalidRemoteUrl()

        client_kwargs = {}
        auth_kwargs = self.authenticator.kwargs()

        client_kwargs.update(auth_kwargs)
        client_kwargs.update(kwargs)

        client, remote_path = get_transport_and_path(origin_uri, **client_kwargs)
        return client, remote_path

    def push_to(self, origin_uri, branch_name=None):
        selector = self._wants_branch(branch_name=branch_name)
        client, remote_path = self.get_client(origin_uri)
        return client.send_pack(remote_path, selector, self.repo.object_store.generate_pack_contents)

    # Like: git push
    def push(self, origin_uri=None, branch_name=None):
        return self.push_to(origin_uri, branch_name)

    # Not recommended at ALL ... !!!
    def dirty_pull_from(self, origin_uri, branch_name=None):
        # Remove all previously existing data
        rmtree(self.path)
        os.makedirs(self.path)
        self.repo = DulwichRepo.init(self.path)

        # Fetch brand new copy from remote
        return self.pull_from(origin_uri, branch_name)

    def pull_from(self, origin_uri, branch_name=None):
        return self.fetch(origin_uri)

    # Like: git pull
    def pull(self, origin_uri=None, branch_name=None):
        return self.pull_from(origin_uri, branch_name)

    def fetch_remote(self, origin_uri=None):
        # Get client
        client, remote_path = self.get_client(origin_uri=origin_uri)

        # Fetch data from remote repository
        remote_refs = client.fetch(remote_path, self.repo)

        return remote_refs

    def fetch(self, origin_uri=None, bare=None):
        bare = bare or False

        # Remote refs
        remote_refs = self.fetch_remote(origin_uri)

        # Update head
        # Hit repo because head doesn't yet exist so
        self.repo['HEAD'] = remote_refs['HEAD']

        # Checkout working directories
        if not bare:
            self.checkout_all()

    @classmethod
    def clone(cls, origin_uri, local_path, auth=None, mkdir=True, bare=False, **kwargs):
        """Clone a remote repository"""
        if mkdir and not(os.path.exists(local_path)):
            os.makedirs(local_path)

        # Initialize the local repository
        if bare:
            local_repo = cls.init_bare(local_path)
        else:
            local_repo = cls.init(local_path)

        repo = cls(local_repo, origin_uri=origin_uri, auth=auth)

        repo.fetch(bare=bare)

        # Add origin
        # TODO

        return repo

    @classmethod
    def clone_bare(cls, *args, **kwargs):
        """Same as .clone except clones to a bare repository by default
        """
        kwargs.setdefault('bare', True)
        return cls.clone(*args, **kwargs)

    def _commit(self, committer=None, author=None, message=None, files=None, *args, **kwargs):
        modified_files = files or self.modified_files
        logging.warning("STAGING : %s" % modified_files)
        self.add(modified_files)
        message = message or self.DEFAULT_MESSAGE
        author_msg = self._format_userinfo(author)
        committer_msg = self._format_userinfo(committer)
        return self.repo.do_commit(
            message=message,
            author=author_msg,
            committer=committer_msg,
            encoding='UTF-8')

    # Like: git commmit -a
    def commit(self, name=None, email=None, message=None, files=None):
        user_info = {
            'name': name,
            'email': email,
        }
        return self._commit(
            committer=user_info,
            author=user_info,
            message=message,
            files=files)

    # Push all local commits
    # and pull all remote commits
    def sync(self, origin_uri=None):
        self.push(origin_uri)
        return self.pull(origin_uri)

    def lookup_entry(self, relpath, trackable_files=set()):
        if not relpath in trackable_files:
            raise KeyError

        abspath = self.abspath(relpath)

        with open(abspath, 'rb') as git_file:
            data = git_file.read()
            s = sha1()
            s.update("blob %u\0" % len(data))
            s.update(data)
        return (s.hexdigest(), os.stat(abspath).st_mode)

    @property
    @funky.transform(set)
    def tracked_files(self):
        return list(self.index)

    @property
    @funky.transform(set)
    def raw_files(self):
        return utils.paths.subpaths(self.path)

    @property
    @funky.transform(set)
    def ignored_files(self):
        return utils.paths.subpaths(self.path, filters=self.filters)

    @property
    @funky.transform(set)
    def trackable_files(self):
        return self.raw_files - self.ignored_files

    @property
    @funky.transform(set)
    def untracked_files(self):
        return self.trackable_files - self.tracked_files

    """
    @property
    @funky.transform(set)
    def modified_staged_files(self):
        "Checks if the file has changed since last commit"
        timestamp = self.last_commit.commit_time
        index = self.index
        return [
            f
            for f in self.tracked_files
            if index[f][1][0] > timestamp
        ]
    """

    # Return a list of tuples
    # representing the changed elements in the git tree
    def _changed_entries(self):
        if not self.has_commits:
            return []
        obj_sto = self.repo.object_store
        tree_id = self['HEAD'].tree
        names = self.trackable_files

        lookup_func = partial(self.lookup_entry, trackable_files=names)

        # Format = [((old_name, new_name), (old_mode, new_mode), (old_sha, new_sha)), ...]
        tree_diff = changes_from_tree(names, lookup_func, obj_sto, tree_id, want_unchanged=False)
        return list(tree_diff)

    @funky.transform(set)
    def _changed_entries_by_pattern(self, pattern):
        changed_entries = self._changed_entries()
        filtered_paths = [
            funky.first_true(names)
            for names, modes, sha in changed_entries
            if tuple(map(bool, names)) == pattern and funky.first_true(names)
        ]

        return filtered_paths

    @property
    @funky.transform(set)
    def removed_files(self):
        return self._changed_entries_by_pattern(self.PATTERN_REMOVED) - self.ignored_files

    @property
    @funky.transform(set)
    def added_files(self):
        return self._changed_entries_by_pattern(self.PATTERN_ADDED) - self.ignored_files

    @property
    @funky.transform(set)
    def modified_files(self):
        modified_files = self._changed_entries_by_pattern(self.PATTERN_MODIFIED) - self.ignored_files
        return modified_files

    @property
    @funky.transform(set)
    def modified_unstaged_files(self):
        timestamp = self.last_commit.commit_time
        return [
            f
            for f in self.tracked_files
            if os.stat(self.abspath(f)).st_mtime > timestamp
        ]

    @property
    def pending_files(self):
        """
        Returns a list of all files that could be possibly staged
        """
        # Union of both
        return self.modified_files | self.untracked_files

    @property
    def pending_files_by_state(self):
        files = {
            'modified': self.modified_files,
            'untracked': self.untracked_files
        }

        # "Flip" the dictionary
        return {
            path: state
            for state, paths in files.items()
            for path in paths
        }

    """
    @property
    @funky.transform(set)
    def modified_files(self):
        return self.modified_staged_files | self.modified_unstaged_files
    """

    # Like: git add
    @funky.arglist_method
    def stage(self, files):
        return self.repo.stage(files)

    def add(self, *args, **kwargs):
        return self.stage(*args, **kwargs)

    # Like: git rm
    @funky.arglist_method
    def rm(self, files, force=False):
        index = self.index
        index_files = filter(lambda f: f in index, files)
        for f in index_files:
            del self.index[f]
        return index.write()

    def mv_fs(self, file_pair):
        old_name, new_name = file_pair
        os.rename(old_name, new_name)

    # Like: git mv
    @funky.arglist_method
    def mv(self, files_pair):
        index = self.index
        files_in_index = filter(lambda f: f[0] in index, files_pair)
        map(self.mv_fs, files_in_index)
        old_files = map(funky.first, files_in_index)
        new_files = map(funky.last, files_in_index)
        self.add(new_files)
        self.rm(old_files)
        self.add(old_files)
        return

    @working_only
    def _checkout_tree(self, tree):
        return build_index_from_tree(
            self.repo.path,
            self.repo.index_path(),
            self.repo.object_store,
            tree
        )

    def checkout_all(self, commit_sha=None):
        commit_sha = commit_sha or self.head
        commit_tree = self._commit_tree(commit_sha)
        # Rebuild index from the current tree
        return self._checkout_tree(commit_tree)

    def checkout(self, commit_sha=None, files=None):
        """Checkout only a select amount of files
        """
        commit_sha = commit_sha or self.head
        files = files or []

        return self

    @funky.arglist_method
    def reset(self, files, commit='HEAD'):
        pass

    def rm_all(self):
        self.index.clear()
        return self.index.write()

    def _to_commit(self, commit_obj):
        """Allows methods to accept both SHA's or dulwich Commit objects as arguments
        """
        if isinstance(commit_obj, basestring):
            return self.repo[commit_obj]
        return commit_obj

    def _commit_sha(self, commit_obj):
        """Extracts a Dulwich commits SHA
        """
        if utils.git.is_sha(commit_obj):
            return commit_obj
        elif isinstance(commit_obj, basestring):
            # Can't use self[commit_obj] to avoid infinite recursion
            commit_obj = self.repo[commit_obj]
        return commit_obj.id

    def _blob_data(self, sha):
        """Return a blobs content for a given SHA
        """
        return self[sha].data

    # Get the nth parent back for a given commit
    def get_parent_commit(self, commit, n=None):
        """ Recursively gets the nth parent for a given commit
            Warning: Remember that parents aren't the previous commits
        """
        if n is None:
            n = 1
        commit = self._to_commit(commit)
        parents = commit.parents

        if n <= 0 or not parents:
            # Return a SHA
            return self._commit_sha(commit)

        parent_sha = parents[0]
        parent = self[parent_sha]

        # Recur
        return self.get_parent_commit(parent, n - 1)

    def get_previous_commit(self, commit_ref, n=None):
        commit_sha = self._parse_reference(commit_ref)
        n = n or 1
        commits = self.commits()
        return funky.next(commits, commit_sha, n=n, default=commit_sha)

    def _parse_reference(self, ref_string):
        # COMMIT_REF~x
        if '~' in ref_string:
            ref, count = ref_string.split('~')
            count = int(count)
            commit_sha = self._commit_sha(ref)
            return self.get_previous_commit(commit_sha, count)
        return self._commit_sha(ref_string)

    def _commit_tree(self, commit_sha):
        """Return the tree object for a given commit
        """
        return self[commit_sha].tree

    def diff(self, commit_sha, compare_to=None, diff_type=None):
        diff_type = diff_type or self.DEFAULT_DIFF_TYPE
        diff_func = self.DIFF_FUNCTIONS[diff_type]

        if not compare_to:
            compare_to = self.get_previous_commit(commit_sha)

        return self._diff_between(compare_to, commit_sha, diff_function=diff_func)

    def get_commit_files(self, commit_sha, parent_path=None, is_tree=None, paths=None):
        """Returns a dict of the following Format :
            {
                "directory/filename.txt": {
                    'name': 'filename.txt',
                    'path': "directory/filename.txt",
                    "sha": "xxxxxxxxxxxxxxxxxxxx",
                    "data": "blablabla",
                    "mode": 0xxxxx",
                },
                ...
            }
        """
        # Default values
        context = {}
        is_tree = is_tree or False
        parent_path = parent_path or ''

        if is_tree:
            tree = self[commit_sha]
        else:
            tree = self[self._commit_tree(commit_sha)]

        for mode, path, sha in tree.entries():
            # Check if entry is a directory
            if mode == self.MODE_DIRECTORY:
                context.update(
                    self.get_commit_files(sha, parent_path=os.path.join(parent_path, path), is_tree=True, paths=paths)
                )
                continue

            subpath = os.path.join(parent_path, path)

            # Only add the files we want
            if not(paths is None or subpath in paths):
                continue

            # Add file entry
            context[subpath] = {
                'name': path,
                'path': subpath,
                'mode': mode,
                'sha': sha,
                'data': self._blob_data(sha),
            }
        return context

    def file_versions(self, path):
        """Returns all commits where given file was modified
        """
        versions = []
        commits_info = self.commit_info()
        seen_shas = set()

        for commit in commits_info:
            try:
                files = self.get_commit_files(commit['sha'], paths=[path])
                file_path, file_data = files.items()[0]
            except IndexError:
                continue

            file_sha = file_data['sha']

            if file_sha in seen_shas:
                continue
            else:
                seen_shas.add(file_sha)

            # Add file info
            commit['file'] = file_data
            versions.append(file_data)
        return versions

    def _diff_between(self, old_commit_sha, new_commit_sha, diff_function=None):
        """Internal method for getting a diff between two commits
            Please use .diff method unless you have very speciic needs
        """

        # If commit is first commit (new_commit_sha == old_commit_sha)
        # then compare to an empty tree
        if new_commit_sha == old_commit_sha:
            old_tree = Tree()
        else:
            old_tree = self._commit_tree(old_commit_sha)

        new_tree = self._commit_tree(new_commit_sha)

        return diff_function(self.repo.object_store, old_tree, new_tree)

    def changes(self, *args, **kwargs):
        """ List of changes between two SHAs
            Returns a list of lists of tuples :
            [
                [
                    (oldpath, newpath), (oldmode, newmode), (oldsha, newsha)
                ],
                ...
            ]
        """
        kwargs['diff_type'] = 'changes'
        return self.diff(*args, **kwargs)

    def changes_count(self, *args, **kwargs):
        return len(self.changes(*args, **kwargs))

    def _refs_by_pattern(self, pattern):
        refs = self.refs

        def item_filter(key_value):
            """Filter only concered refs"""
            key, value = key_value
            return key.startswith(pattern)

        def item_map(key_value):
            """Rewrite keys"""
            key, value = key_value
            new_key = key[len(pattern):]
            return (new_key, value)

        return dict(
            map(item_map,
                filter(item_filter,
                    refs.items()
                )
            )
        )

    @property
    def refs(self):
        return self.repo.get_refs()

    @property
    def branches(self):
        return self._refs_by_pattern(self.REFS_BRANCHES)

    @property
    def remote_branches(self):
        return self._refs_by_pattern(self.REFS_REMOTES)

    @property
    def tags(self):
        return self._refs_by_pattern(self.REFS_TAGS)

    @property
    def remotes(self):
        """ Dict of remotes
        {
            'origin': 'http://friendco.de/some_user/repo.git',
            ...
        }
        """
        config = self.repo.get_config()
        return {
            keys[1]: values['url']
            for keys, values in config.items()
            if keys[0] == 'remote'
        }

    def create_branch(self, base_branch, new_branch, tracking=None):
        """Try creating a new branch which tracks the given remote
            if such a branch does not exist then branch off a local branch
        """

        # The remote to track
        tracking = self.DEFAULT_REMOTE

        # Already exists
        if new_branch in self.branches:
            raise Exception("branch %s already exists" % new_branch)

        # Get information about remote_branch
        remote_branch = os.path.sep.join([tracking, base_branch])

        # Fork Local
        if base_branch in self.branches:
            base_ref = self._format_ref_branch(base_branch)
        # Fork remote
        elif remote_branch in self.remote_branches:
            base_ref = self._format_ref_remote(remote_branch)
            # TODO : track
        else:
            raise Exception("Can not find the branch named '%s' to fork either locally or in '%s'" % (base_branch, tracking))

        # Reference of new branch
        new_ref = self._format_ref_branch(new_branch)

        # Copy reference to create branch
        self.repo.refs[new_ref] = self.repo.refs[base_ref]

        return new_ref

    def switch_branch(self, branch_name, tracking=None, create=None):
        """Changes the current branch
        """
        if create is None:
            create = True

        # Check if branch exists
        if not branch_name in self.branches:
            self.create_branch(branch_name, branch_name, tracking=tracking)

        # Get branch reference
        branch_ref = self._format_ref_branch(branch_name)

        # Change main branch
        self.repo.refs.set_symbolic_ref('HEAD', branch_ref)

        if self.is_working:
            # Remove all files
            self.clean_working()

            # Add files for the current branch
            self.checkout_all()

    def clean(self, force=None, directories=None):
        untracked_files = self.untracked_files
        map(os.remove, untracked_files)
        return untracked_files

    def clean_working(self):
        """Purges all the working (removes everything except .git)
            used by checkout_all to get clean branch switching
        """
        return self.clean()

    def _get_fs_structure(self, tree_sha, depth=None, parent_sha=None):
        tree = self[tree_sha]
        structure = {}
        if depth is None:
            depth = self.MAX_TREE_DEPTH
        elif depth == 0:
            return structure
        for mode, path, sha in tree.entries():
            # tree
            if mode == self.MODE_DIRECTORY:
                # Recur
                structure[path] = self._get_fs_structure(sha, depth=depth - 1, parent_sha=tree_sha)
            # commit
            else:
                structure[path] = sha
        structure['.'] = tree_sha
        structure['..'] = parent_sha or tree_sha
        return structure

    def _get_fs_structure_by_path(self, tree_sha, path):
        parts = path.split(os.path.sep)
        depth = len(parts) + 1
        structure = self._get_fs_structure(tree_sha, depth=depth)

        return funky.subkey(structure, parts)

    def commit_ls(self, ref, subpath=None):
        """List a "directory" for a given commit
            using the tree of thqt commit
        """
        tree_sha = self._commit_tree(ref)

        # Root path
        if subpath in self.ROOT_PATHS or not subpath:
            return self._get_fs_structure(tree_sha, depth=1)
        # Any other path
        return self._get_fs_structure_by_path(tree_sha, subpath)

    def commit_file(self, ref, path):
        """Return info on a given file for a given commit
        """
        name, info = self.get_commit_files(ref, paths=[path]).items()[0]
        return info

    def _is_fast_forward(self):
        pass

    def _merge_fast_forward(self):
        pass

    def __hash__(self):
        """This is required otherwise the memoize function will just mess it up
        """
        return hash(self.path)

    def __getitem__(self, key):
        sha = self._parse_reference(key)
        return self.repo[sha]

    def __setitem__(self, key, value):
        self.repo[sha] = value

    # Alias to clone_bare
    fork = clone_bare
    log = commit_info
    diff_count = changes_count
