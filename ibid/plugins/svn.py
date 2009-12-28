"""
Configuration:

[plugins]
    [[svn]]
        [[[repositories]]]
            [[[[repo1]]]]
                url = https://...
                username = user1
                password = mypassword
            [[[[repo2]]]]
                url = https://...
                username = user2
                password = mypassword
            ...

"""

from datetime import datetime, timedelta
import logging
import os.path
import pysvn
import textwrap

import ibid
from ibid.plugins import Processor, match, RPC, handler, run_every
from ibid.config import DictOption
from ibid.utils import ago, format_date, human_join

help = {'svn': u'Retrieves commit logs from a Subversion repository.'}

HEAD_REVISION = pysvn.Revision(pysvn.opt_revision_kind.head)

class Branch(object):
    def __init__(self, repository_name = None, url = None, username = None, password = None):
        self.repository = repository_name
        self.url = url
        self.username = username
        self.password = password

    def _call_command(self, command, *args, **kw):
        return command(self, username=self.username, password=self.password)(*args, **kw)

    def log(self, *args, **kw):
        """
        Low-level SVN logging call - returns lists of pysvn.PysvnLog objects.
        """
        return self._call_command(SVNLog, *args, **kw)

    def _convert_to_revision(self, revision):
        """
        Convert numbers to pysvn.Revision instances
        """
        try:
            revision.kind
            return revision
        except:
            return pysvn.Revision(pysvn.opt_revision_kind.number, revision)

    def get_commits(self, start_revision = None, end_revision = None, limit = None, full = False):
        """
        Get formatted commit messages for each of the commits in range
        [start_revision:end_revision], defaulting to the latest revision.
        """
        if not full: # such as None
            full = False

        if not start_revision:
            start_revision = HEAD_REVISION

        start_revision = self._convert_to_revision(start_revision)

        # If no end-revision and no limit given, set limit to 1
        if not end_revision:
            end_revision = 0
            if not limit:
                limit = 1

        end_revision = self._convert_to_revision(end_revision)

        log_messages = self.log(start_revision, end_revision, paths=full)
        commits = [self.format_log_message(log_message, full) for log_message in log_messages]

        return commits

    def _generate_delta(self, changed_paths):
        class T(object):
            pass

        delta = T()
        delta.basepath = "/"
        delta.added = []
        delta.modified = []
        delta.removed = []
        delta.renamed = []

        action_mapper = {
            'M': delta.modified,
            'A': delta.added,
            'D': delta.removed,
        }

        all_paths = [changed_path.path for changed_path in changed_paths]

        commonprefix = os.path.commonprefix(all_paths)

        # os.path.commonprefix will return "/e" if you give it "/etc/passwd"
        # and "/exports/foo", which is not what we want.  Remove until the last
        # "/" character.
        while commonprefix and commonprefix[-1] != "/":
            commonprefix = commonprefix[:-1]

        pathinfo = commonprefix

        if commonprefix.startswith("/trunk/"):
            commonprefix = "/trunk/"
            pathinfo = " [trunk]"

        if commonprefix.startswith("/branches/"):
            commonprefix = "/branches/%s/" % (commonprefix.split('/')[2],)
            pathinfo = " [" + commonprefix.split('/')[2] + "]"

        if commonprefix.startswith("/tags/"):
            commonprefix = "/tags/%s/" % (commonprefix.split('/')[2],)
            pathinfo = " [" + commonprefix.split('/')[2] + "]"

        for changed_path in changed_paths:
            action_mapper[changed_path.action].append([changed_path.path[len(commonprefix):], None])

        return pathinfo, delta

    def format_log_message(self, log_message, full=False):
        """
        author - string - the name of the author who committed the revision
        date - float time - the date of the commit
        message - string - the text of the log message for the commit
        revision - pysvn.Revision - the revision of the commit

        changed_paths - list of dictionaries. Each dictionary contains:
            path - string - the path in the repository
            action - string
            copyfrom_path - string - if copied, the original path, else None
            copyfrom_revision - pysvn.Revision - if copied, the revision of the original, else None
        """
        revision_number = log_message['revision'].number
        author = log_message['author']
        commit_message = log_message['message']
        timestamp = log_message['date']

        if full:
            pathinfo, delta = self._generate_delta(log_message['changed_paths'])
            changes = []

            if delta.added:
                changes.append('Added:\n\t%s' % '\n\t'.join([file[0] for file in delta.added]))
            if delta.modified:
                changes.append('Modified:\n\t%s' % '\n\t'.join([file[0] for file in delta.modified]))
            if delta.removed:
                changes.append('Removed:\n\t%s' % '\n\t'.join([file[0] for file in delta.removed]))
            if delta.renamed:
                changes.append('Renamed:\n\t%s' % '\n\t, '.join(['%s => %s' % (file[0], file[1]) for file in delta.renamed]))

            timestamp_dt = datetime.utcfromtimestamp(timestamp)
            commit = 'Commit %s by %s to %s%s on %s at %s:\n\n\t%s \n\n%s\n' % (
                revision_number,
                author,
                self.repository,
                pathinfo,
                format_date(timestamp_dt, 'date'),
                format_date(timestamp_dt, 'time'),
                u'\n'.join(textwrap.wrap(commit_message, initial_indent="    ", subsequent_indent="    ")),
                '\n\n'.join(changes))
        else:
            commit = 'Commit %s by %s to %s %s ago: %s\n' % (
                revision_number,
                author,
                self.repository,
                ago(datetime.now() - datetime.fromtimestamp(timestamp), 2),
                commit_message.replace('\n', ' '))

        return commit

class SVNCommand(object):
    def __init__(self, branch, username=None, password=None):
        self._branch = branch
        self._username = username
        self._password = password
        self._client = self._initClient(branch)

    def _initClient(self, branch):
        client = pysvn.Client()
        client.callback_get_login = self.get_login
        client.callback_cancel = CancelAfterTimeout()
        return client

    def get_login(self, realm, username, may_save):
        if self._username and self._password:
            return True, self._username.encode('utf-8'), self._password.encode('utf-8'), False
        return False, None, None, False

    def _initCommand(self):
        self._client.callback_cancel.start()
        pass

    def _destroyCommand(self):
        self._client.callback_cancel.done()
        pass

    def __call__(self, *args, **kw):
        self._initCommand()
        return self._command(*args, **kw)
        self._destroyCommand()

class SVNLog(SVNCommand):
    def _command(self, start_revision=HEAD_REVISION, end_revision=None, paths=False, stop_on_copy=True, limit=1):
        log_messages = self._client.log(self._branch.url, revision_start=start_revision, revision_end=end_revision, discover_changed_paths=paths, strict_node_history=stop_on_copy, limit=limit)
        return log_messages

class CancelAfterTimeout(object):
    """
    Implement timeout for if a SVN command is taking its time
    """
    def __init__(self, timeout = 15):
        self.timeout = timeout

    def start(self):
        self.cancel_at = datetime.now() + timedelta(seconds=self.timeout)

    def __call__(self):
        return datetime.now() > self.cancel_at
    
    def done(self):
        pass

class Subversion(Processor, RPC):
    u"""(last commit|commit <revno>) [to <repo>] [full]
    (svnrepos|svnrepositories)
    """
    feature = 'svn'
    autoload = False

    repositories = DictOption('repositories', 'Dict of repositories names and URLs')

    def __init__(self, name):
        self.log = logging.getLogger('plugins.svn')
        Processor.__init__(self, name)
        RPC.__init__(self)

    def setup(self):
        self.branches = {}
        for name, repository in self.repositories.items():
            reponame = name.lower()
            self.branches[reponame] = Branch(reponame, repository['url'], username = repository['username'], password = repository['password'])

    @match(r'^svn ?(?:repos|repositories)$')
    def handle_repositories(self, event):
        repositories = self.branches.keys()
        if repositories:
            event.addresponse(u'I know about: %s', human_join(sorted(repositories)))
        else:
            event.addresponse(u"I don't know about any repositories")

    @match(r'^(?:last\s+)?commit(?:\s+(\d+))?(?:(?:\s+to)?\s+(\S+?))?(\s+full)?$')
    def commit(self, event, revno, repository, full):

        if repository == "full":
            repository = None
            full = True

        if full:
            full = True

        revno = revno and int(revno) or None
        commits = self.get_commits(repository, revno, full=full)

        if commits:
            for commit in commits:
                if commit:
                    event.addresponse(commit.strip())

    def get_commits(self, repository, start, end=None, full=None):
        branch = None
        if repository:
            repository = repository.lower()
            if repository not in self.branches:
                return None
            branch = self.branches[repository]

        if not branch:
            (repository, branch) = self.branches.items()[0]

        if not start:
            start = HEAD_REVISION

        if not end:
            end = None

        commits = branch.get_commits(start, end_revision=end, full=full)
        return commits

# vi: set et sta sw=4 ts=4:
