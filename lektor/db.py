import os
import errno
import hashlib
import operator
import posixpath

from itertools import islice

from jinja2 import Undefined, is_undefined
from jinja2.utils import LRUCache
from jinja2.exceptions import UndefinedError

from lektor import metaformat
from lektor.utils import sort_normalize_string, cleanup_path, to_os_path, \
     fs_enc
from lektor.sourceobj import SourceObject
from lektor.context import get_ctx
from lektor.datamodel import load_datamodels, load_flowblocks
from lektor.imagetools import make_thumbnail, read_exif, get_image_info
from lektor.assets import Directory
from lektor.editor import make_editor_session, PRIMARY_ALT


def _require_ctx(record):
    ctx = get_ctx()
    if ctx is None:
        raise RuntimeError('This operation requires a context but none was '
                           'on the stack.')
    if ctx.pad is not record.pad:
        raise RuntimeError('The context on the stack does not match the '
                           'pad of the record.')
    return ctx


def _is_content_file(filename, alt=PRIMARY_ALT):
    if filename == 'contents.lr':
        return True
    if alt != PRIMARY_ALT and filename == 'contents+%s.lr' % alt:
        return True
    return False


class _CmpHelper(object):

    def __init__(self, value, reverse):
        self.value = value
        self.reverse = reverse

    @staticmethod
    def coerce(a, b):
        if isinstance(a, basestring) and isinstance(b, basestring):
            return sort_normalize_string(a), sort_normalize_string(b)
        if type(a) is type(b):
            return a, b
        if isinstance(a, Undefined) or isinstance(b, Undefined):
            if isinstance(a, Undefined):
                a = None
            if isinstance(b, Undefined):
                b = None
            return a, b
        if isinstance(a, (int, long, float)):
            try:
                return a, type(a)(b)
            except (ValueError, TypeError, OverflowError):
                pass
        if isinstance(b, (int, long, float)):
            try:
                return type(b)(a), b
            except (ValueError, TypeError, OverflowError):
                pass
        return a, b

    def __eq__(self, other):
        a, b = self.coerce(self.value, other.value)
        return a == b

    def __lt__(self, other):
        a, b = self.coerce(self.value, other.value)
        try:
            if self.reverse:
                return b < a
            return a < b
        except TypeError:
            return NotImplemented

    def __gt__(self, other):
        return not (self.__lt__(other) or self.__eq__(other))

    def __le__(self, other):
        return self.__lt__(other) or self.__eq__(other)

    def __ge__(self, other):
        return not self.__lt__(other)


def _auto_wrap_expr(value):
    if isinstance(value, _Expr):
        return value
    return _Literal(value)


def save_eval(filter, record):
    try:
        return filter.__eval__(record)
    except UndefinedError as e:
        return Undefined(e.message)


class _Expr(object):

    def __eval__(self, record):
        return record

    def __eq__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.eq)

    def __ne__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.ne)

    def __and__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.and_)

    def __or__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.or_)

    def __gt__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.gt)

    def __ge__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.ge)

    def __lt__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.lt)

    def __le__(self, other):
        return _BinExpr(self, _auto_wrap_expr(other), operator.le)

    def contains(self, item):
        return _ContainmentExpr(self, _auto_wrap_expr(item))

    def startswith(self, other):
        return _BinExpr(self, _auto_wrap_expr(other),
            lambda a, b: unicode(a).lower().startswith(unicode(b).lower()))

    def endswith(self, other):
        return _BinExpr(self, _auto_wrap_expr(other),
            lambda a, b: unicode(a).lower().endswith(unicode(b).lower()))

    def startswith_cs(self, other):
        return _BinExpr(self, _auto_wrap_expr(other),
                        lambda a, b: unicode(a).startswith(unicode(b)))

    def endswith_cs(self, other):
        return _BinExpr(self, _auto_wrap_expr(other),
                        lambda a, b: unicode(a).endswith(unicode(b)))


class _Literal(_Expr):

    def __init__(self, value):
        self.__value = value

    def __eval__(self, record):
        return self.__value


class _BinExpr(_Expr):

    def __init__(self, left, right, op):
        self.__left = left
        self.__right = right
        self.__op = op

    def __eval__(self, record):
        return self.__op(
            self.__left.__eval__(record),
            self.__right.__eval__(record)
        )


class _ContainmentExpr(_Expr):

    def __init__(self, seq, item):
        self.__seq = seq
        self.__item = item

    def __eval__(self, record):
        seq = self.__seq.__eval__(record)
        item = self.__item.__eval__(record)
        if isinstance(item, Record):
            item = item['_id']
        return item in seq


class _RecordQueryField(_Expr):

    def __init__(self, field):
        self.__field = field

    def __eval__(self, record):
        try:
            return record[self.__field]
        except KeyError:
            return Undefined(obj=record, name=self.__field)


class _RecordQueryProxy(object):

    def __getattr__(self, name):
        if name[:2] != '__':
            return _RecordQueryField(name)
        raise AttributeError(name)

    def __getitem__(self, name):
        try:
            return self.__getattr__(name)
        except AttributeError:
            raise KeyError(name)


F = _RecordQueryProxy()


class Record(SourceObject):
    source_classification = 'record'

    def __init__(self, pad, data):
        SourceObject.__init__(self, pad)
        self._data = data

    @property
    def datamodel(self):
        """Returns the data model for this record."""
        try:
            return self.pad.db.datamodels[self._data['_model']]
        except LookupError:
            # If we cannot find the model we fall back to the default one.
            return self.pad.db.default_model

    @property
    def is_hidden(self):
        """If a record is hidden it will not be processed.  This is related
        to the expose flag that can be set on the datamodel.
        """
        if not is_undefined(self._data['_hidden']):
            return self._data['_hidden']

        node = self
        while node is not None:
            if not node.datamodel.expose:
                return True
            node = node.parent

        return False

    @property
    def is_visible(self):
        """The negated version of :attr:`is_hidden`."""
        return not self.is_hidden

    @property
    def record_label(self):
        """The generic record label."""
        rv = self.datamodel.format_record_label(self)
        if rv:
            return rv
        if not self['_id']:
            return '(Index)'
        return self['_id'].replace('-', ' ').replace('_', ' ').title()

    @property
    def url_path(self):
        """The target path where the record should end up."""
        bits = []
        node = self
        while node is not None:
            bits.append(node['_slug'])
            node = node.parent
        bits.reverse()
        return '/' + '/'.join(bits).strip('/')

    def get_sort_key(self, fields):
        """Returns a sort key for the given field specifications specific
        for the data in the record.
        """
        rv = [None] * len(fields)
        for idx, field in enumerate(fields):
            if field[:1] == '-':
                field = field[1:]
                reverse = True
            else:
                field = field.lstrip('+')
                reverse = False
            rv[idx] = _CmpHelper(self._data.get(field), reverse)
        return rv

    def to_dict(self):
        """Returns a clone of the internal data dictionary."""
        return dict(self._data)

    def iter_fields(self):
        """Iterates over all fields and values."""
        return self._data.iteritems()

    def iter_record_path(self):
        """Iterates over all records that lead up to the current record."""
        rv = []
        node = self
        while node is not None:
            rv.append(node)
            node = node.parent
        return reversed(rv)

    def __contains__(self, name):
        return name in self._data and not is_undefined(self._data[name])

    def __getitem__(self, name):
        return self._data[name]

    def __setitem__(self, name, value):
        self.pad.cache.persist_if_cached(self)
        self._data[name] = value

    def __delitem__(self, name):
        self.pad.cache.persist_if_cached(self)
        del self._data[name]

    def __eq__(self, other):
        if self is other:
            return True
        if self.__class__ != other.__class__:
            return False
        return self['_path'] == other['_path']

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<%s model=%r path=%r%s>' % (
            self.__class__.__name__,
            self['_model'],
            self['_path'],
            self['_alt'] != PRIMARY_ALT and ' alt=%r' % self['_alt'] or '',
        )


class Page(Record):
    """This represents a loaded record."""
    is_attachment = False

    @property
    def source_filename(self):
        return posixpath.join(self.pad.db.to_fs_path(self['_path']),
                              'contents.lr')

    def _iter_dependent_filenames(self):
        yield self.source_filename

    @property
    def url_path(self):
        return Record.url_path.__get__(self).rstrip('/') + '/'

    def is_child_of(self, path):
        this_path = cleanup_path(self['_path']).split('/')
        crumbs = cleanup_path(path).split('/')
        return this_path[:len(crumbs)] == crumbs

    def resolve_url_path(self, url_path):
        if not url_path:
            return self

        for idx in xrange(len(url_path)):
            piece = '/'.join(url_path[:idx + 1])
            child = self.real_children.filter(F._slug == piece).first()
            if child is None:
                attachment = self.attachments.filter(F._slug == piece).first()
                if attachment is None:
                    continue
                node = attachment
            else:
                node = child

            rv = node.resolve_url_path(url_path[idx + 1:])
            if rv is not None:
                return rv

    @property
    def parent(self):
        """The parent of the record."""
        this_path = self._data['_path']
        parent_path = posixpath.dirname(this_path)
        if parent_path != this_path:
            return self.pad.get(parent_path,
                                persist=self.pad.cache.is_persistent(self))

    @property
    def all_children(self):
        """A query over all children that are not hidden."""
        repl_query = self.datamodel.get_child_replacements(self)
        if repl_query is not None:
            return repl_query
        return Query(path=self['_path'], pad=self.pad, alt=self['_alt'])

    @property
    def children(self):
        """Returns a query for all the children of this record.  Optionally
        a child path can be specified in which case the children of a sub
        path are queried.
        """
        return self.all_children.visible_only

    @property
    def real_children(self):
        """A query over all real children of this page.  This includes
        hidden.
        """
        if self.datamodel.child_config.replaced_with is not None:
            return EmptyQuery(path=self['_path'], pad=self.pad,
                              alt=self['_alt'])
        return self.all_children

    def find_page(self, path):
        """Finds a child page."""
        return self.children.get(path)

    @property
    def attachments(self):
        """Returns a query for the attachments of this record."""
        return AttachmentsQuery(path=self['_path'], pad=self.pad,
                                alt=self['_alt'])


class Attachment(Record):
    """This represents a loaded attachment."""
    is_attachment = True

    @property
    def source_filename(self):
        return self.pad.db.to_fs_path(self['_path']) + '.lr'

    @property
    def attachment_filename(self):
        return self.pad.db.to_fs_path(self['_path'])

    @property
    def parent(self):
        """The associated record for this attachment."""
        return self.pad.get(self._data['_attachment_for'],
                            persist=self.pad.cache.is_persistent(self))

    @property
    def record_label(self):
        """The generic record label."""
        rv = self.datamodel.format_record_label(self)
        if rv is not None:
            return rv
        return self['_id']

    def _iter_dependent_filenames(self):
        # We only want to yield the source filename if it actually exists.
        # For attachments it's very likely that this is not the case in
        # case no metadata was defined.
        if os.path.isfile(self.source_filename):
            yield self.source_filename
        yield self.attachment_filename


class Image(Attachment):
    """Specific class for image attachments."""

    def _get_image_info(self):
        rv = getattr(self, '_image_info', None)
        if rv is None:
            with open(self.attachment_filename, 'rb') as f:
                rv = self._image_info = get_image_info(f)
        return rv

    @property
    def exif(self):
        """Provides access to the exif data."""
        rv = getattr(self, '_exif_cache', None)
        if rv is None:
            with open(self.attachment_filename, 'rb') as f:
                rv = self._exif_cache = read_exif(f)
        return rv

    @property
    def width(self):
        """The width of the image if possible to determine."""
        rv = self._get_image_info()['size'][0]
        if rv is not None:
            return rv
        return Undefined('Width of image could not be determined.')

    @property
    def height(self):
        """The height of the image if possible to determine."""
        rv = self._get_image_info()['size'][1]
        if rv is not None:
            return rv
        return Undefined('Height of image could not be determined.')

    @property
    def mode(self):
        """Returns the mode of the image."""
        rv = self._get_image_info()['mode']
        if rv is not None:
            return rv
        return Undefined('The mode of the image could not be determined.')

    @property
    def format(self):
        """Returns the format of the image."""
        rv = self._get_image_info()['format']
        if rv is not None:
            return rv
        return Undefined('The format of the image could not be determined.')

    @property
    def format_description(self):
        """Returns the format of the image."""
        rv = self._get_image_info()['format_description']
        if rv is not None:
            return rv
        return Undefined('The format description of the image '
                         'could not be determined.')

    def thumbnail(self, width, height=None):
        """Utility to create thumbnails."""
        return make_thumbnail(_require_ctx(self),
            self.attachment_filename, self.url_path,
            width=width, height=height)


attachment_classes = {
    'image': Image,
}


class Query(object):
    """Object that helps finding records.  The default configuration
    only finds pages.
    """

    def __init__(self, path, pad, alt=PRIMARY_ALT):
        self.path = path
        self.pad = pad
        self.alt = alt
        self._include_pages = True
        self._include_attachments = False
        self._order_by = None
        self._filters = None
        self._pristine = True
        self._limit = None
        self._offset = None
        self._visible_only = False

    @property
    def self(self):
        """Returns the object this query starts out from."""
        return self.pad.get(self.path, alt=self.alt)

    def _clone(self, mark_dirty=False):
        """Makes a flat copy but keeps the other data on it shared."""
        rv = object.__new__(self.__class__)
        rv.__dict__.update(self.__dict__)
        if mark_dirty:
            rv._pristine = False
        return rv

    def _get(self, id, persist=True):
        """Low level record access."""
        return self.pad.get('%s/%s' % (self.path, id), persist=persist,
                            alt=self.alt)

    def _iterate(self):
        """Low level record iteration."""
        # If we iterate over children we also need to track those
        # dependencies.  There are two ways in which we track them.  The
        # first is through the start record of the query.  If that does
        # not work for whatever reason (because it does not exist for
        # instance).
        self_record = self.pad.get(self.path, alt=self.alt)
        if self_record is not None:
            self.pad.db.track_record_dependency(self_record)

        # We also always want to record the path itself as dependency.
        ctx = get_ctx()
        if ctx is not None:
            ctx.record_dependency(self.pad.db.to_fs_path(self.path))

        for name, is_attachment in self.pad.db.iter_items(
                self.path, alt=self.alt):
            if not ((is_attachment == self._include_attachments) or
                    (not is_attachment == self._include_pages)):
                continue

            record = self._get(name, persist=False)
            if self._visible_only and not record.is_visible:
                continue
            for filter in self._filters or ():
                if not save_eval(filter, record):
                    break
            else:
                yield record

    def filter(self, expr):
        """Filters records by an expression."""
        rv = self._clone(mark_dirty=True)
        rv._filters = list(self._filters or ())
        rv._filters.append(expr)
        return rv

    def get_order_by(self):
        """Returns the order that should be used."""
        if self._order_by is not None:
            return self._order_by
        base_record = self.pad.get(self.path)
        if base_record is not None:
            return base_record.datamodel.child_config.order_by

    @property
    def visible_only(self):
        """Returns all visible pages."""
        rv = self._clone(mark_dirty=True)
        rv._visible_only = True
        return rv

    @property
    def with_attachments(self):
        """Includes attachments as well."""
        rv = self._clone(mark_dirty=True)
        rv._include_attachments = True
        return rv

    def first(self):
        """Loads all matching records as list."""
        return next(iter(self), None)

    def all(self):
        """Loads all matching records as list."""
        return list(self)

    def order_by(self, *fields):
        """Sets the ordering of the query."""
        rv = self._clone()
        rv._order_by = fields or None
        return rv

    def offset(self, offset):
        """Sets the ordering of the query."""
        rv = self._clone(mark_dirty=True)
        rv._offset = offset
        return rv

    def limit(self, limit):
        """Sets the ordering of the query."""
        rv = self._clone(mark_dirty=True)
        rv._limit = limit
        return rv

    def count(self):
        """Counts all matched objects."""
        rv = 0
        for item in self._iterate():
            rv += 1
        return rv

    def get(self, id):
        """Gets something by the local path."""
        # If we're not pristine, we need to query here
        if not self._pristine:
            return self.filter(F._id == id).first()
        # otherwise we can load it directly.
        return self._get(id)

    def __nonzero__(self):
        return self.first() is not None

    def __iter__(self):
        """Iterates over all records matched."""
        iterable = self._iterate()

        order_by = self.get_order_by()
        if order_by:
            iterable = sorted(
                iterable, key=lambda x: x.get_sort_key(order_by))

        if self._offset is not None or self._limit is not None:
            iterable = islice(iterable, self._offset or 0, self._limit)

        for item in iterable:
            yield item

    def __repr__(self):
        return '<%s %r%s>' % (
            self.__class__.__name__,
            self.path,
            self.alt and ' alt=%r' % self.alt or '',
        )


class EmptyQuery(Query):

    def _get(self, id, persist=True):
        pass

    def _iterate(self):
        """Low level record iteration."""
        return iter(())


class AttachmentsQuery(Query):
    """Specialized query class that only finds attachments."""

    def __init__(self, path, pad, alt=PRIMARY_ALT):
        Query.__init__(self, path, pad, alt=PRIMARY_ALT)
        self._include_pages = False
        self._include_attachments = True

    @property
    def images(self):
        """Filters to images."""
        return self.filter(F._attachment_type == 'image')

    @property
    def videos(self):
        """Filters to videos."""
        return self.filter(F._attachment_type == 'video')

    @property
    def audio(self):
        """Filters to audio."""
        return self.filter(F._attachment_type == 'audio')

    @property
    def documents(self):
        """Filters to documents."""
        return self.filter(F._attachment_type == 'document')

    @property
    def text(self):
        """Filters to plain text data."""
        return self.filter(F._attachment_type == 'text')


def _iter_filename_choices(fn_base, alt, config):
    # the order here is important as attachments can exist without a .lr
    # file and as such need to come second or the loading of raw data will
    # implicitly say the record exists.

    if alt is not None and config.is_valid_alternative(alt):
        yield os.path.join(fn_base, 'contents+%s.lr' % alt), False
    yield os.path.join(fn_base, 'contents.lr'), False

    if alt is not None and config.is_valid_alternative(alt):
        yield fn_base + '+%s.lr' % alt, True
    yield fn_base + '.lr', True


def _iter_datamodel_choices(datamodel_name, path, is_attachment=False):
    yield datamodel_name
    if not is_attachment:
        yield posixpath.basename(path).split('.')[0].replace('-', '_').lower()
        yield 'page'
    yield 'none'


class Database(object):

    def __init__(self, env, config=None):
        self.env = env
        if config is None:
            config = env.load_config()
        self.config = config
        self.datamodels = load_datamodels(env)
        self.flowblocks = load_flowblocks(env)

    def to_fs_path(self, path):
        """Convenience function to convert a path into an file system path."""
        return os.path.join(self.env.root_path, 'content', to_os_path(path))

    def load_raw_data(self, path, alt=PRIMARY_ALT, cls=None):
        """Internal helper that loads the raw record data.  This performs
        very little data processing on the data.
        """
        path = cleanup_path(path)
        if cls is None:
            cls = dict

        fn_base = self.to_fs_path(path)

        rv = cls()
        choiceiter = _iter_filename_choices(fn_base, alt, self.config)
        for fs_path, is_attachment in choiceiter:
            try:
                with open(fs_path, 'rb') as f:
                    for key, lines in metaformat.tokenize(f, encoding='utf-8'):
                        rv[key] = u''.join(lines)
            except IOError as e:
                if e.errno not in (errno.ENOTDIR, errno.ENOENT):
                    raise
                if not is_attachment or not os.path.isfile(fs_path[:-3]):
                    continue
                rv = {}
            rv['_path'] = path
            rv['_id'] = posixpath.basename(path)
            rv['_gid'] = hashlib.md5(path.encode('utf-8')).hexdigest()
            rv['_alt'] = alt or PRIMARY_ALT
            if is_attachment:
                rv['_attachment_for'] = posixpath.dirname(path)
            return rv

    def iter_items(self, path, alt=PRIMARY_ALT):
        """Iterates over all items below a path and yields them as
        tuples in the form ``(id, is_attachment)``.
        """
        fn_base = self.to_fs_path(path)

        choiceiter = _iter_filename_choices(fn_base, alt, self.config)

        for fs_path, is_attachment in choiceiter:
            if not os.path.isfile(fs_path):
                continue
            # This path is actually for an attachment, which means that we
            # cannot have any items below it and will just abort with an
            # empty iterator.
            if is_attachment:
                return

            try:
                dir_path = os.path.dirname(fs_path)
                for filename in os.listdir(dir_path):
                    # If we found an .lr file, we just skip it as we do
                    # not want to handle it at this point.  This is done
                    # separately later.
                    if filename.endswith('.lr'):
                        continue

                    # Likewise if we found an interesting source file we
                    # want to ignore it here.
                    if self.env.is_uninteresting_source_name(filename):
                        continue

                    try:
                        filename = filename.decode(fs_enc)
                    except UnicodeError:
                        continue

                    # We found an attachment!
                    if os.path.isfile(os.path.join(dir_path, filename)):
                        yield filename, True

                    # We found a directory, let's make sure it contains a
                    # contents.lr file (or a contents+alt.lr file).
                    else:
                        if (os.path.isfile(os.path.join(
                                dir_path, filename, 'contents.lr')) or
                            (alt is not None and
                             os.path.isfile(os.path.join(
                                 dir_path, filename, 'contents+%s.lr' % alt)))):
                            yield filename, False
            except IOError as e:
                if e.errno != errno.ENOENT:
                    raise

    def list_items(self, path, alt=PRIMARY_ALT):
        """Like :meth:`iter_items` but returns a list."""
        return list(self.iter_items(path, alt=alt))

    def get_datamodel_for_raw_data(self, raw_data, pad=None):
        """Returns the datamodel that should be used for a specific raw
        data.  This might require the discovery of a parent object through
        the pad.
        """
        path = raw_data['_path']
        is_attachment = bool(raw_data.get('_attachment_for'))
        datamodel = (raw_data.get('_model') or '').strip() or None
        return self.get_implied_datamodel(path, is_attachment, pad,
                                          datamodel=datamodel)

    def iter_dependent_models(self, datamodel):
        seen = set()
        def deep_find(datamodel):
            seen.add(datamodel)

            if datamodel.parent is not None and datamodel.parent not in seen:
                deep_find(datamodel.parent)

            for related_dm_name in (datamodel.child_config.model,
                                    datamodel.attachment_config.model):
                dm = self.datamodels.get(related_dm_name)
                if dm is not None and dm not in seen:
                    deep_find(dm)

        deep_find(datamodel)
        seen.discard(datamodel)
        return iter(seen)

    def get_implied_datamodel(self, path, is_attachment=False, pad=None,
                              datamodel=None):
        """Looks up a datamodel based on the information about the parent
        of a model.
        """
        dm_name = datamodel

        # Only look for a datamodel if there was not defined.
        if dm_name is None:
            parent = posixpath.dirname(path)
            dm_name = None

            # If we hit the root, and there is no model defined we need
            # to make sure we do not recurse onto ourselves.
            if parent != path:
                if pad is None:
                    pad = self.new_pad()
                parent_obj = pad.get(parent)
                if parent_obj is not None:
                    if is_attachment:
                        dm_name = parent_obj.datamodel.attachment_config.model
                    else:
                        dm_name = parent_obj.datamodel.child_config.model

        for dm_name in _iter_datamodel_choices(dm_name, path, is_attachment):
            # If that datamodel exists, let's roll with it.
            datamodel = self.datamodels.get(dm_name)
            if datamodel is not None:
                return datamodel

        raise AssertionError("Did not find an appropriate datamodel.  "
                             "That should never happen.")

    def get_attachment_type(self, path):
        """Gets the attachment type for a path."""
        return self.config['ATTACHMENT_TYPES'].get(
            posixpath.splitext(path)[1])

    def track_record_dependency(self, record):
        ctx = get_ctx()
        if ctx is not None:
            for filename in record._iter_dependent_filenames():
                ctx.record_dependency(filename)
            if record.datamodel.filename:
                ctx.record_dependency(record.datamodel.filename)
                for dep_model in self.iter_dependent_models(record.datamodel):
                    if dep_model.filename:
                        ctx.record_dependency(dep_model.filename)
        return record

    def get_default_slug(self, data, pad):
        parent_path = posixpath.dirname(data['_path'])
        parent = None
        if parent_path != data['_path']:
            parent = pad.get(parent_path)
        if parent:
            slug = parent.datamodel.get_default_child_slug(pad, data)
        else:
            slug = ''
        return slug

    def process_data(self, data, datamodel, pad):
        # Automatically fill in slugs
        if is_undefined(data['_slug']):
            data['_slug'] = self.get_default_slug(data, pad)
        else:
            data['_slug'] = data['_slug'].strip('/')

        # For attachments figure out the default attachment type if it's
        # not yet provided.
        if is_undefined(data['_attachment_type']) and \
           data['_attachment_for']:
            data['_attachment_type'] = self.get_attachment_type(data['_path'])

        # Automatically fill in templates
        if is_undefined(data['_template']):
            data['_template'] = datamodel.get_default_template_name()

    def get_record_class(self, datamodel, raw_data):
        """Returns the appropriate record class for a datamodel and raw data."""
        is_attachment = bool(raw_data.get('_attachment_for'))
        if not is_attachment:
            return Page
        attachment_type = raw_data['_attachment_type']
        return attachment_classes.get(attachment_type, Attachment)

    def new_pad(self):
        """Creates a new pad object for this database."""
        return Pad(self)


def _split_alt_from_url(config, clean_path):
    primary = config.primary_alternative

    # The alternative system is not configured, just return
    if primary is None:
        return None, clean_path

    # First try to find alternatives that are identified by a prefix.
    for prefix, alt in config.get_alternative_url_prefixes():
        if clean_path.startswith(prefix):
            return alt, clean_path[len(prefix):].strip('/')

    # Now find alternatives taht are identified by a suffix.
    for suffix, alt in config.get_alternative_url_suffixes():
        if clean_path.endswith(suffix):
            return alt, clean_path[:-len(suffix)].strip('/')

    # If we have a primary alternative without a prefix and suffix, we can
    # return that one.
    if config.primary_alternative_is_rooted:
        return None, clean_path

    return None, None


class Pad(object):

    def __init__(self, db):
        self.db = db
        self.cache = RecordCache(db.config['EPHEMERAL_RECORD_CACHE_SIZE'])

    def resolve_url_path(self, url_path, include_invisible=False,
                         include_assets=True):
        """Given a URL path this will find the correct record which also
        might be an attachment.  If a record cannot be found or is unexposed
        the return value will be `None`.
        """
        clean_path = cleanup_path(url_path).strip('/')

        # Split off the alt and if no alt was found, point it to the
        # primary alternative.
        alt, clean_path = _split_alt_from_url(self.db.config, clean_path)
        if clean_path is None:
            return None

        alt = alt or self.db.config.primary_alternative
        node = self.get_root(alt=alt)

        pieces = clean_path.split('/')
        if pieces == ['']:
            pieces = []

        rv = node.resolve_url_path(pieces)
        if rv is not None and (include_invisible or rv.is_visible):
            return rv

        if include_assets:
            return self.asset_root.resolve_url_path(pieces)

    def get_root(self, alt=PRIMARY_ALT):
        """The root page of the database."""
        return self.get('/', alt=alt, persist=True)

    root = property(get_root)

    @property
    def asset_root(self):
        """The root of the asset tree."""
        return Directory(self, name='',
                         path=os.path.join(self.db.env.root_path, 'assets'))

    def get(self, path, alt=PRIMARY_ALT, persist=True):
        """Loads a record by path."""
        rv = self.cache.get(path, alt)
        if rv is not None:
            return rv

        raw_data = self.db.load_raw_data(path, alt=alt)
        if raw_data is None:
            return

        rv = self.instance_from_data(raw_data)

        if persist:
            self.cache.persist(rv)
        else:
            self.cache.remember(rv)

        return self.db.track_record_dependency(rv)

    def instance_from_data(self, raw_data, datamodel=None):
        """This creates an instance from the given raw data."""
        if datamodel is None:
            datamodel = self.db.get_datamodel_for_raw_data(raw_data, self)
        data = datamodel.process_raw_data(raw_data, self)
        self.db.process_data(data, datamodel, self)
        cls = self.db.get_record_class(datamodel, data)
        return cls(self, data)

    def edit(self, path, is_attachment=None, alt=PRIMARY_ALT, datamodel=None):
        """Edits a record by path."""
        path = cleanup_path(path)
        return make_editor_session(self, path, is_attachment=is_attachment,
                                   alt=alt, datamodel=datamodel)

    def query(self, path=None, alt=PRIMARY_ALT):
        """Queries the database either at root level or below a certain
        path.  This is the recommended way to interact with toplevel data.
        The alternative is to work with the :attr:`root` document.
        """
        return Query(path='/' + (path or '').strip('/'), pad=self, alt=alt)


class RecordCache(object):
    """The record cache holds records eitehr in an persistent or ephemeral
    section which helps the pad not load records it already saw.
    """

    def __init__(self, ephemeral_cache_size=500):
        self.persistent = {}
        self.ephemeral = LRUCache(ephemeral_cache_size)

    def _get_cache_key(self, record_or_path, alt=PRIMARY_ALT):
        if isinstance(record_or_path, basestring):
            path = record_or_path
        else:
            path = record_or_path['_path']
            alt = record_or_path['_alt']
        if alt != PRIMARY_ALT:
            return '%s+%s' % (path, alt)
        return path

    def is_persistent(self, record):
        """Indicates if a record is in the persistent record cache."""
        cache_key = self._get_cache_key(record)
        return cache_key in self.persistent

    def remember(self, record):
        """Remembers the record in the record cache."""
        cache_key = self._get_cache_key(record)
        if cache_key not in self.persistent and cache_key not in self.ephemeral:
            self.ephemeral[cache_key] = record

    def persist(self, record):
        """Persists a record.  This will put it into the persistent cache."""
        cache_key = self._get_cache_key(record)
        self.persistent[cache_key] = record
        try:
            del self.ephemeral[cache_key]
        except KeyError:
            pass

    def persist_if_cached(self, record):
        """If the record is already ephemerally cached, this promotes it to
        the persistent cache section.
        """
        cache_key = self._get_cache_key(record)
        if cache_key in self.ephemeral:
            self.persist(record)

    def get(self, path, alt=PRIMARY_ALT):
        """Looks up a record from the cache."""
        cache_key = self._get_cache_key(path, alt)
        rv = self.persistent.get(cache_key)
        if rv is not None:
            return rv
        rv = self.ephemeral.get(cache_key)
        if rv is not None:
            return rv
