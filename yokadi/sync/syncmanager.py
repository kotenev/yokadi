"""
SyncManager
@author: Aurélien Gâteau <mail@agateau.com>
@license: GPL v3 or later
"""
import json
import os

from contextlib import contextmanager

from yokadi.core import db
from yokadi.sync import ALIASES_DIRNAME, PROJECTS_DIRNAME, TASKS_DIRNAME, VERSION
from yokadi.sync.dump import clearDump, dump, createVersionFile
from yokadi.sync.vcsimplerrors import NotFastForwardError, VcsImplError

from yokadi.sync.dbreplicator import DbReplicator
from yokadi.sync.pull import merge, importSince, importAll, findConflicts, getRemoteDumpVersion


BEFORE_MERGE_TAG = "before-merge"


class SyncManager(object):
    def __init__(self, *, session=None, vcsImpl=None):
        assert vcsImpl, "vcsImpl cannot be None"
        self.vcsImpl = vcsImpl
        self._dumpDir = vcsImpl.srcDir

        self._pathsToDelete = set()
        self._dictsToWrite = {}

        if session:
            self._dbReplicator = DbReplicator(self._dumpDir, session)
            self.session = session
        else:
            self.session = None

    @contextmanager
    def _mergeOperation(self):
        self.vcsImpl.createTag(BEFORE_MERGE_TAG)
        yield
        self.vcsImpl.deleteTag(BEFORE_MERGE_TAG)

    def initDumpRepository(self):
        assert not os.path.exists(self._dumpDir), "Dump dir {} should not already exist".format(self._dumpDir)
        os.makedirs(self._dumpDir)
        self.vcsImpl.init()
        createVersionFile(self._dumpDir)
        for dirname in ALIASES_DIRNAME, PROJECTS_DIRNAME, TASKS_DIRNAME:
            path = os.path.join(self._dumpDir, dirname)
            os.mkdir(path)
        self.vcsImpl.commitAll("Created")

    def isMergeInProgress(self):
        return self.vcsImpl.hasTag(BEFORE_MERGE_TAG)

    def abortMerge(self):
        self.vcsImpl.resetTo(BEFORE_MERGE_TAG)
        self.vcsImpl.deleteTag(BEFORE_MERGE_TAG)

    def sync(self, pullUi):
        if self.hasChangesToCommit():
            pullUi.reportProgress("Committing local changes")
            self.vcsImpl.commitAll("s_sync")

        while True:
            if not self.pull(pullUi=pullUi):
                return False
            if not self.hasChangesToPush():
                break
            pullUi.reportProgress("Pushing local changes")
            try:
                self.push()
                break
            except NotFastForwardError:
                pullUi.reportProgress("Remote has other changes, need to pull again")
            except VcsImplError as exc:
                pullUi.reportError("Failed to push: {}".format(exc))
                return False
        return True

    def clearDump(self):
        clearDump(self._dumpDir)

    def dump(self):
        assert self.session
        dump(session=self.session, vcsImpl=self.vcsImpl)

    def pull(self, pullUi):
        """
        Pull and import changes. Returns True in case of success.
        """
        assert self.session
        assert self.vcsImpl.isWorkTreeClean()
        pullUi.reportProgress("Pulling remote changes")
        self.vcsImpl.fetch()
        if not self._checkDumpVersion(pullUi):
            return False

        with self._mergeOperation():
            merge(self.vcsImpl, pullUi=pullUi)
            if self.hasChangesToImport():
                pullUi.reportProgress("Importing changes")
                importSince(self.session, self.vcsImpl, BEFORE_MERGE_TAG, pullUi=pullUi)
            else:
                pullUi.reportProgress("No remote changes")
        return True

    def importAll(self, pullUi):
        assert self.session
        with self._mergeOperation():
            importAll(self.session, self.vcsImpl, pullUi=pullUi)

    def push(self):
        self.vcsImpl.push()

    def checkDumpIntegrity(self):
        assert self.session
        self._checkItems(PROJECTS_DIRNAME, db.Project)
        self._checkItems(TASKS_DIRNAME, db.Task)
        self._checkItems(ALIASES_DIRNAME, db.Alias)
        self._checkUnicity(PROJECTS_DIRNAME)
        self._checkUnicity(ALIASES_DIRNAME)
        self._checkTaskProjects()

    def _checkItems(self, dirname, table):
        print("# Checking all {} are there".format(dirname))
        objectDir = os.path.join(self._dumpDir, dirname)
        dumpUuids = set()
        for name in os.listdir(objectDir):
            if not name.endswith(".json"):
                continue
            objectPath = os.path.join(objectDir, name)
            with open(objectPath) as fp:
                dct = json.load(fp)
            dumpUuids.add(dct["uuid"])

        query = self.session.query(table).all()
        dbUuids = set(x.uuid for x in query)

        if dbUuids != dumpUuids:
            missing = dumpUuids - dbUuids
            if missing:
                missing = '\n'.join(missing)
                print("## Missing DB items:\n{}\n".format(missing))
            missing = dbUuids - dumpUuids
            if missing:
                missing = '\n'.join(missing)
                print("## Missing dump items:\n{}\n".format(missing))

    def _checkUnicity(self, dirname):
        print("# Checking {} unicity".format(dirname))
        jsonDirPath = os.path.join(self._dumpDir, dirname)
        conflicts = findConflicts(jsonDirPath, "name")
        for name, conflictList in conflicts.items():
            print("## {} exists {} times".format(name, len(conflictList)))
            for conflict in conflictList:
                path = os.path.join(jsonDirPath, conflictList["uuid"] + ".json")
                print(path)

    def _checkTaskProjects(self):
        print("# Checking all tasks have an existing project")
        projectDir = os.path.join(self._dumpDir, PROJECTS_DIRNAME)
        taskDir = os.path.join(self._dumpDir, TASKS_DIRNAME)
        projectUuids = {os.path.splitext(x)[0] for x in os.listdir(projectDir)}

        first = True
        for taskName in os.listdir(taskDir):
            taskPath = os.path.join(taskDir, taskName)
            try:
                with open(taskPath) as fp:
                    dct = json.load(fp)
                if dct["projectUuid"] not in projectUuids:
                    if first:
                        print("These tasks point to a non existing project")
                        first = False
                    print(taskPath)
            except Exception as exc:
                raise Exception("Error in {}".format(taskPath)) from exc

    def hasChangesToCommit(self):
        return not self.vcsImpl.isWorkTreeClean()

    def hasChangesToImport(self):
        assert self.vcsImpl.hasTag(BEFORE_MERGE_TAG)
        changes = self.vcsImpl.getChangesSince(BEFORE_MERGE_TAG)
        return changes.hasChanges()

    def hasChangesToPush(self):
        changes = self.vcsImpl.getChangesSince("origin/master")
        return changes.hasChanges()

    def _checkDumpVersion(self, pullUi):
        remoteDumpVersion = getRemoteDumpVersion(self.vcsImpl)
        if remoteDumpVersion > VERSION:
            msg = "Remote dump version is {remote} but Yokadi expects version {local}.\n" \
                "You need to update your version of Yokadi to be able to synchronize your database." \
                .format(remote=remoteDumpVersion, local=VERSION)
            pullUi.reportError(msg)
            return False

        if remoteDumpVersion < VERSION and not self.vcsImpl.isUpToDate():
            msg = "Remote dump version is {remote} but Yokadi expects version {local}.\n" \
                "The remote dump has changes at version {remote} which have not been imported in your local Yokadi." \
                " Your local Yokadi cannot import changes from this remote dump version. You need to update the" \
                " version of Yokadi which made these changes and sync them again." \
                .format(remote=remoteDumpVersion, local=VERSION)
            pullUi.reportError(msg)
            return False
        # Note: if remoteDumpVersion is less than VERSION but remote has no changes, we allow the sync: this is how
        # sync repo version updates are pushed.
        return True
