import os
import stat
import shutil
import posixpath

from lektor.sourceobj import SourceObject


# TODO: add less support and stuff like that.


def get_asset(pad, filename, parent=None):
    env = pad.db.env

    if env.is_uninteresting_source_name(filename):
        return None

    try:
        st = os.stat(os.path.join(parent.source_filename, filename))
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return Directory(pad, filename, parent=parent)
    return File(pad, filename, parent=parent)


class Asset(SourceObject):
    # source specific overrides.  the source_filename to none removes
    # the inherited descriptor.
    source_classification = 'asset'
    source_filename = None

    is_directory = False

    def __init__(self, pad, name, path=None, parent=None):
        SourceObject.__init__(self, pad)
        if parent is not None:
            if path is None:
                path = name
            path = os.path.join(parent.source_filename, path)
        self.source_filename = path

        # If the name starts with an underscore it's corrected into a
        # dash.  This can only ever happen for files like _htaccess and
        # friends which are explicitly whitelisted in the environment as
        # all other files with leading underscores are ignored.
        if name[:1] == '_':
            name = '.' + name[1:]
        self.name = name
        self.parent = parent

    @property
    def url_path(self):
        if self.parent is None:
            return '/' + self.name
        return posixpath.join(self.parent.url_path, self.name)

    @property
    def artifact_name(self):
        if self.parent is not None:
            return self.parent.artifact_name.rstrip('/') + '/' + self.name
        return self.url_path

    def build_asset(self, f):
        pass

    @property
    def children(self):
        return iter(())

    def get_child(self, name):
        return None

    def resolve_url_path(self, url_path):
        if not url_path:
            return self
        child = self.get_child(url_path[0])
        if child:
            return child.resolve_url_path(url_path[1:])

    def __repr__(self):
        return '<%s %r>' % (
            self.__class__.__name__,
            self.artifact_name,
        )


class Directory(Asset):
    is_directory = True

    @property
    def children(self):
        try:
            files = os.listdir(self.source_filename)
        except OSError:
            return

        for filename in files:
            asset = self.get_child(filename)
            if asset is not None:
                yield asset

    def get_child(self, name):
        return get_asset(self.pad, name, parent=self)


class File(Asset):

    def build_asset(self, f):
        with open(self.source_filename, 'rb') as sf:
            shutil.copyfileobj(sf, f)
